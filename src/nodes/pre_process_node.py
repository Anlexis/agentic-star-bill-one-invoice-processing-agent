"""AgentCore Platform v1.0 - outer pre_process node.

Cat 2 outer backbone. This node owns the CALLER CONTRACT: it is the one place
that sees the raw request and the structured caller channel (``input_context``)
before anything else runs, so it is where hostile input is refused and where
every caller field is bounded.

Order matters and is deliberate:

1. refuse an injection attempt (raw text, then markup-stripped text, then the
   structured channel depth-first including keys) — refusal comes BEFORE any
   sanitization, because the markup strip destroys the evidence;
2. bound every caller field the pipeline consumes — identifiers to an inert
   shape, the record cap to a finite range, the scope to a closed set — and fail
   closed naming the field but never echoing the rejected value;
3. only then sanitize the request text and serialize it for the inner workflow
   graph.

Business validation happens inside the inner graph's ValidateInputNode; this
node does the refusal, the bounds, and the serialization.
"""

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.security import (
    FieldError,
    as_choice,
    as_inert_ref,
    as_record_id,
    parse_finite_int,
    sanitize_query,
    screen_payload,
    screen_text,
)

# The record identifier may arrive under any of these names; first one wins.
_HINT_KEYS = ("invoice_id", "invoice_hint", "invoice_number")

# Listing scopes the summarize intent understands.
SCOPES = ("received", "approved", "paid", "all")

# Bounds on the summarize listing. A caller-supplied cap has to be finite and
# ranged or it is not a cap at all.
MAX_RECORDS_MIN = 1
MAX_RECORDS_MAX = 500
MAX_RECORDS_DEFAULT = 3

# Fields this node consumes from the caller channel. Anything else is ignored
# rather than rejected (the platform may attach its own metadata), and only the
# COUNT of ignored fields is audited — never their names or values.
_CONSUMED_KEYS = frozenset(_HINT_KEYS) | {"issuer_ref", "scope", "max_records"}


class PreProcessNode(FunctionNode):
    """Refuse hostile input, bound the caller fields, serialize for the workflow."""

    # The outer backbone's SINGLE external trust gate. A real caller enters at
    # VERIFIED_EXTERNAL and the inner Bill One call runs under this same
    # (unelevated) context, so the external gate lives HERE, not on the inner API
    # node. An under-trusted (ANONYMOUS) caller is denied at this gate before any
    # call is made.
    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    _AUDIT_EVENT: ClassVar[str] = "pre_process_complete"

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        user_input = state.get("user_input", "")
        raw_context = state.get("input_context", {})  # read-only

        if not user_input or not user_input.strip():
            return self._refuse(["PreProcessNode: user_input is empty or missing"], state)

        if not isinstance(raw_context, dict):
            return self._refuse(["PreProcessNode: input_context must be a mapping"], state)

        # (1) Refusal. The raw text first — the markup strip below would delete a
        # control token and forward the rest as ordinary prose.
        categories = sorted(set(screen_text(user_input) + screen_payload(raw_context)))
        if categories:
            return self._refuse(
                [
                    "PreProcessNode: request refused - caller input matched "
                    f"{len(categories)} disallowed input pattern class(es): {', '.join(categories)}"
                ],
                state,
                refused_categories=categories,
            )

        # (2) Bounds. Every consumed field, or a closed failure naming the field.
        try:
            caller = self._validate_caller_fields(raw_context)
        except FieldError as exc:
            # Closed-set by construction: the field NAME (one of the consumed
            # keys) and the validator's fixed reason, read from the typed
            # attributes - never str(exc), and never the rejected value.
            return self._refuse([f"PreProcessNode: invalid caller field - {exc.field}: {exc.reason}"], state)

        ignored_field_count = sum(1 for key in raw_context if key not in _CONSUMED_KEYS)

        # (3) Reduce and serialize. Structured params travel to the inner graph
        # as JSON; the first inner node parses them back.
        sanitized_input = sanitize_query(user_input.strip())
        validated_input = json.dumps(
            {
                "text": sanitized_input,
                "invoice_hint": caller["invoice_hint"],
                "issuer_ref": caller["issuer_ref"],
                "scope": caller["scope"],
                "max_records": caller["max_records"],
            }
        )

        # Audit the shaped request: presence and counts only, never caller text.
        emit_trace_event(
            self._AUDIT_EVENT,
            {
                "has_invoice_hint": bool(caller["invoice_hint"]),
                "has_issuer_ref": bool(caller["issuer_ref"]),
                "scope": caller["scope"] or "",
                "max_records": caller["max_records"],
                "ignored_context_fields": ignored_field_count,
            },
            state,
        )

        return {
            "validated_input": validated_input,
            "invoice_hint": caller["invoice_hint"],
            "issuer_ref": caller["issuer_ref"],
            "listing_scope": caller["scope"],
            "max_records": caller["max_records"],
            "status": AgentStatus.SUCCESS.value,
        }

    # -- helpers --------------------------------------------------------------

    def _validate_caller_fields(self, context: "dict[str, Any]") -> "dict[str, Any]":
        """Bound every consumed caller field, or raise FieldError naming it."""
        invoice_hint = ""
        for key in _HINT_KEYS:
            if key in context:
                invoice_hint = as_record_id(context[key], key)
                if invoice_hint:
                    break

        issuer_ref = as_inert_ref(context["issuer_ref"], "issuer_ref") if "issuer_ref" in context else ""
        scope = as_choice(context["scope"], "scope", SCOPES) if "scope" in context else ""
        max_records = (
            parse_finite_int(
                context["max_records"],
                "max_records",
                minimum=MAX_RECORDS_MIN,
                maximum=MAX_RECORDS_MAX,
            )
            if context.get("max_records") is not None
            else MAX_RECORDS_DEFAULT
        )
        return {
            "invoice_hint": invoice_hint,
            "issuer_ref": issuer_ref,
            "scope": scope,
            "max_records": max_records,
        }

    def _refuse(
        self, error_log: "list[str]", state: "dict[str, Any]", refused_categories: "list[str] | None" = None
    ) -> "dict[str, Any]":
        """Fail closed, and audit the refusal without carrying the payload."""
        emit_trace_event(
            self._AUDIT_EVENT,
            {
                "refused": True,
                "refused_categories": refused_categories or [],
            },
            state,
        )
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": error_log,
        }
