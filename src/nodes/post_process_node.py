"""AgentCore Platform v1.0 - outer post_process node.

Cat 2 outer backbone: finalize the response after the inner Bill One workflow
graph has run. GraphNode.merge_output() maps the inner result into the outer
state; this node shapes the caller-facing ``formatted_output`` and is the last
place anything can be stopped before it reaches the caller.

The domain output gate is the MODULE-LEVEL ``_security_gate_output()`` below,
called from ``execute()``. It is deliberately NOT an instance method and NOT the
framework ``_extra_security_gate_output`` hook: the framework gate methods are
@final on FunctionNode and the real runtime auto-wraps ``_extra_`` hooks (which
breaks the .invoke() chain), so domain checks live in a module-level helper
invoked inline.

Two properties of the gate are worth stating plainly, because getting either
wrong looks identical to getting it right from the outside:

* It walks NESTED structures. The two richest fields in the response
  (``billone_payload``, ``invoice_summary``) are mappings, so a scan that only
  looked at top-level strings would report zero findings on a payload carrying a
  credential one level down.
* On EVERY error return - a gate violation AND a pre-existing inner-workflow
  error - it CLEARS the output-bearing state, it does not merely raise or
  return an error status. The graph's output builder falls back to
  ``state["result"]`` even when the status is ERROR, so a gate that only flips
  the status still ships the un-gated inner answer inside the error envelope.
  Omitting a field from one envelope is not clearing it: a checkpoint or a
  downstream reader picks it straight back up out of state.

The caller-visible ERROR envelope carries CLOSED-SET labels only. Every
non-success return goes through ``_contain()``, which publishes
``formatted_output = error_envelope(reason)`` - a constant reason code this
module chose, and nothing else. Nothing read from ``error_log``, from the gate's
own violation messages, or from any other node-authored string is projected: an
internal entry can carry an upstream exception message or a third-party response
body, and truncation, path stripping or credential-only redaction of such text
is not a closed set. ``error_log`` itself is untouched - it stays the internal
channel the state reducer appends to and the audit trail reads. The gate's
violations go there, never into the envelope; the entries already present on the
inner-error path are not re-emitted, because the reducer appends and would
duplicate every line.

No error envelope carries Bill One record evidence either. ``record_id`` /
``record_ref`` are this agent's WRITE EVIDENCE - the SUCCESS branch of the gate
below REFUSES an output that lacks them - so returning them under an ERROR
status would tell a caller being informed of failure that an invoice record was
nonetheless touched, and which one.
"""

import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json

# Credential-shaped strings that must never reach the caller (defence in depth -
# the framework's own credential scan also runs on every result).
_CREDENTIAL_LIKE_RE = re.compile(r"eyJ[A-Za-z0-9._-]{10,}|sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._-]{16,}")

# Bound on how deep the gate walks a response structure. The response is built by
# this pipeline, so this is a guard against a pathological nesting bug rather
# than a hostile one - but an unbounded recursive walk is its own hazard.
_MAX_SCAN_DEPTH = 8

# Every state field that can carry released text to the caller. On EVERY error
# return ALL of them are cleared: leaving any one populated re-opens the leak,
# because the output builder reads them regardless of status.
#
# `record_id` / `record_ref` / `invoice_id` are the Bill One write evidence and
# `intent` is the action label - they are cleared for the same reason the error
# envelope omits them: a caller told the operation failed must not be able to
# recover, from a checkpoint or a downstream reader, that a record was touched
# and which one.
_OUTPUT_BEARING_FIELDS = (
    "result",
    "formatted_output",
    "confirmation",
    "billone_payload",
    "invoice_summary",
    "record_id",
    "record_ref",
    "invoice_id",
    "issuer_name",
    "invoice_status",
    "intent",
    "redaction_flags",
)

# ── Caller-visible ERROR envelope ─────────────────────────────────────────────
#
# The closed set of values a non-success response may carry: a constant reason
# code this module chose. The envelope says WHAT happened - never to which
# invoice, and never in a node's own words.
_REASON_WORKFLOW_FAILED = "billone_workflow_failed"  # the inner workflow reported status=error
_REASON_OUTPUT_WITHHELD = "output_withheld_by_gate"  # this node's output gate blocked the response
ERROR_REASONS = frozenset({_REASON_WORKFLOW_FAILED, _REASON_OUTPUT_WITHHELD})


def error_envelope(reason: str) -> "dict[str, Any]":
    """The caller-visible ERROR envelope: a constant reason code and nothing else.

    ``reason`` must be one of ``ERROR_REASONS``; anything else is refused - and
    not echoed back in the exception - so no free text can be smuggled in under
    the one key the caller reads.

    The constant key also keeps the mapping TRUTHY. AgentBaseGraph.get_output()
    selects ``formatted_output or result`` with no status check, so a falsy
    envelope would re-open that projection onto whatever survived in state.
    """
    if reason not in ERROR_REASONS:
        raise ValueError("error envelope reason must be one of ERROR_REASONS")
    return {"reason": reason}


def _cleared_output_state() -> "dict[str, Any]":
    """Blank every output-bearing state field.

    Spread by every error return. ``formatted_output`` is included so the
    mapping is a complete statement of the cleared state; ``_contain()`` then
    overwrites that one key with the closed-set envelope.
    """
    return {field: "" for field in _OUTPUT_BEARING_FIELDS}


def _contain(reason: str, new_errors: "list[str] | None" = None) -> "dict[str, Any]":
    """Fail-closed ERROR result: the closed-set envelope, everything else cleared.

    Returning ERROR is not containment on its own. The graph's output builder
    falls back to ``state["result"]`` when ``formatted_output`` is falsy, and it
    does so on the error path too - so an un-cleared inner answer would ride out
    inside the error envelope, credentials and all. Every output-bearing field
    is cleared, and the replacement envelope is deliberately TRUTHY so that
    fallback never fires.

    ``new_errors`` - this node's own gate violations - go to ``error_log``, the
    internal channel, never into the envelope. Entries already in ``error_log``
    are not re-emitted: the state reducer appends, so they would be duplicated.
    """
    contained: "dict[str, Any]" = {
        **_cleared_output_state(),
        "formatted_output": error_envelope(reason),
        "status": AgentStatus.ERROR.value,
    }
    if new_errors:
        contained["error_log"] = list(new_errors)
    return contained


def _scan_for_credentials(value: Any, path: str = "formatted_output", depth: int = 0) -> "list[str]":
    """Depth-first credential scan over a response value.

    Returns violation descriptions naming the PATH only. The matched text is
    never included: an error message that quotes the credential it found has
    leaked it into the log instead of the response. The descriptions are
    internal (``error_log``); they are never part of the caller-visible envelope.
    """
    if depth > _MAX_SCAN_DEPTH:
        return []
    problems: list[str] = []
    if isinstance(value, str):
        if _CREDENTIAL_LIKE_RE.search(value):
            problems.append(f"output gate: credential-like value at {path}")
    elif isinstance(value, dict):
        for key, sub in value.items():
            problems.extend(_scan_for_credentials(sub, f"{path}[{key!r}]", depth + 1))
    elif isinstance(value, (list, tuple)):
        for index, sub in enumerate(value):
            problems.extend(_scan_for_credentials(sub, f"{path}[{index}]", depth + 1))
    return problems


def _security_gate_output(formatted_output: "dict[str, Any]", is_success: bool) -> "list[str]":
    """Domain output gate (module-level; called from PostProcessNode.execute()).

    Blocks (returns violations for):
      - a SUCCESS response with no record evidence (record_id/record_ref), which
        would misrepresent the Bill One action outcome to the caller;
      - any credential-shaped string anywhere in the caller-facing output,
        including inside the nested payload and summary mappings.

    The violation messages name a field path, never a caller value, and go to
    ``error_log`` only - they are not part of the caller-visible envelope.
    """
    problems: list[str] = []
    if is_success and not (formatted_output.get("record_id") or formatted_output.get("record_ref")):
        problems.append("output gate: SUCCESS output missing record_id/record_ref evidence")
    problems.extend(_scan_for_credentials(formatted_output))
    return problems


class PostProcessNode(FunctionNode):
    """Format the final agent output."""

    # Read-only formatting of the already-produced result - default permissive.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        errored = state.get("status") == AgentStatus.ERROR.value

        # If the inner workflow errored, preserve the error status (do not mask
        # it) - but CONTAIN the failure rather than re-publishing the run: the
        # envelope is the closed-set reason code and nothing else, and the delta
        # clears every output-bearing field so the identifiers cannot be
        # recovered from a checkpoint or by a downstream reader. The entries
        # already in error_log stay internal - they are neither projected nor
        # re-emitted. On the compiled backbone an errored run is routed straight
        # to finalize, so this branch is reached by a direct execute(); it still
        # returns the same envelope as every other error path.
        if errored:
            errors = state.get("error_log", []) or []
            # Outcome signals only - a closed-set reason code and a count. The
            # audit log is not a store for invoice content.
            emit_trace_event(
                "post_process_error_contained",
                {"reason": _REASON_WORKFLOW_FAILED, "errors": len(errors)},
                state,
            )
            return _contain(_REASON_WORKFLOW_FAILED)

        formatted_output = {
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "issuer_name": state.get("issuer_name", ""),
            "invoice_status": state.get("invoice_status", ""),
            "intent": state.get("intent", ""),
            "confirmation": state.get("confirmation", ""),
            "invoice_summary": from_json(state.get("invoice_summary"), {}),
            "billone_payload": from_json(state.get("billone_payload"), {}),
        }

        violations = _security_gate_output(formatted_output, is_success=True)
        if violations:
            # Audit the block - outcome signals only (a reason code and a count).
            emit_trace_event(
                "post_process_complete",
                {"gate_violation": True, "violation_count": len(violations)},
                state,
            )
            return _contain(_REASON_OUTPUT_WITHHELD, violations)

        # Audit the final response shaping - outcome signals only, no payload content.
        emit_trace_event(
            "post_process_complete",
            {
                "intent": state.get("intent", ""),
                "has_record_id": bool(state.get("record_id")),
            },
            state,
        )

        return {
            "formatted_output": formatted_output,
            "status": AgentStatus.SUCCESS.value,
        }
