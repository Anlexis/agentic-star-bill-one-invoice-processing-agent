"""AgentCore Platform v1.0 - inner workflow Step 1: ValidateInput.

Two jobs, in this order:

1. **Refuse** an injection attempt. The outer pre-processing node already
   screens the caller contract, but this node is the inner graph's own entry
   point and a guarantee that only holds where an upstream gate happens to be
   active is not a guarantee. The screen runs here too, on the text this node
   actually received, and refuses rather than sanitizing: a strip that removes
   a control token forwards the surviving directive as ordinary prose.
2. **Flag and redact** email addresses and access-token-like strings before
   anything is logged. An invoice request legitimately names a vendor and an
   invoice number, so this half is redaction for safe logging, not a reject.

The only other deterministic auto-reject is the empty / non-request guard.
"""

import json
import re

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json
from src.services.security import screen_text

# Email addresses and bearer/JWT/API-token-like strings that might appear in a
# pasted request. Flagged and redacted before logging.
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_TOKEN_RE = re.compile(r"\b(?:eyJ[A-Za-z0-9_-]{6,}|secret_[A-Za-z0-9]{6,}|sk-[A-Za-z0-9]{6,})\b")
_REDACTION = "[REDACTED]"

# Minimum signal that the text is a real request rather than noise.
_MIN_LEN = 3


class ValidateInputNode(FunctionNode):
    """Refuse hostile input, then flag-and-redact the inbound invoice request."""

    # Inner domain node - the external trust gate lives on the outer backbone
    # pre_process (VERIFIED_EXTERNAL); the caller context is forwarded unchanged.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        raw = state.get("validated_input") or state.get("user_input") or ""

        # The outer graph serialized the request into a JSON string; accept both
        # the serialized shape and a bare string for direct unit testing.
        text = raw
        invoice_hint = state.get("invoice_hint", "")
        issuer_ref = state.get("issuer_ref", "")
        listing_scope = state.get("listing_scope", "")
        max_records = state.get("max_records")
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                obj = json.loads(raw)
                text = obj.get("text", "")
                invoice_hint = obj.get("invoice_hint", invoice_hint)
                issuer_ref = obj.get("issuer_ref", issuer_ref)
                listing_scope = obj.get("scope", listing_scope)
                max_records = obj.get("max_records", max_records)
            except (ValueError, TypeError):
                text = raw

        if not isinstance(text, str) or len(text.strip()) < _MIN_LEN:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ValidateInputNode: empty or non-request input"],
            }

        # Refusal before redaction. The categories are reported; the matched text
        # never is, so a refusal cannot re-emit the payload it just rejected.
        categories = screen_text(text)
        if categories:
            emit_trace_event(
                "validate_input_complete",
                {"refused": True, "refused_categories": categories},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    "ValidateInputNode: request refused - text matched "
                    f"{len(categories)} disallowed input pattern class(es): {', '.join(categories)}"
                ],
            }

        # Deterministic flag-and-redact (before any logging). Local list per
        # invocation - never a module-global (no cross-invoke leak).
        flags: list[str] = []
        redacted = text
        if _EMAIL_RE.search(redacted):
            flags.append("email")
            redacted = _EMAIL_RE.sub(_REDACTION, redacted)
        if _TOKEN_RE.search(redacted):
            flags.append("token")
            redacted = _TOKEN_RE.sub(_REDACTION, redacted)

        # Audit the scan outcome - redaction flags only, never the inbound text.
        emit_trace_event(
            "validate_input_complete",
            {"has_invoice_hint": bool(invoice_hint), "redaction_flags": flags},
            state,
        )

        result = {
            "validated_input": redacted.strip(),
            "invoice_hint": invoice_hint,
            "issuer_ref": issuer_ref,
            "listing_scope": listing_scope,
            "redaction_flags": to_json(flags),
            "status": AgentStatus.SUCCESS.value,
        }
        if isinstance(max_records, int) and not isinstance(max_records, bool):
            result["max_records"] = max_records
        return result
