"""AgentCore Platform v1.0 - inner workflow Step 5: Confirm.

Formats the looked-up / registered / summarized Bill One invoice outcome
(id + reference + issuer + status / summary count) into a human-readable
confirmation message, surfacing the affected record so a human can review what
was touched.

Everything rendered here is either produced by the pipeline or a caller value
that was locked to an inert identifier upstream - a free-text caller string
reaching this line would be output injection.
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json

_VERBS = {
    "lookup_invoice": "Retrieved invoice record",
    "register_invoice": "Registered invoice",
    "summarize_invoices": "Summarized invoice records",
}


class ConfirmNode(FunctionNode):
    """Build the human-readable confirmation."""

    # Inner domain node, read-only formatting of already-fetched data - the
    # external trust gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        record_id = state.get("record_id", "")
        record_ref = state.get("record_ref", "")
        issuer_name = state.get("issuer_name", "")
        invoice_status = state.get("invoice_status", "")
        intent = state.get("intent", "lookup_invoice")

        if not record_id and not record_ref:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ConfirmNode: no record_id/record_ref to confirm"],
            }

        verb = _VERBS.get(intent, "Processed invoice record")
        parts = [f"{verb} '{issuer_name or record_id}'"]
        issuer_ref = state.get("issuer_ref", "")
        if issuer_ref:
            # Caller-supplied, locked to an inert identifier by the caller
            # contract before it ever reached the pipeline.
            parts.append(f"issuer_ref={issuer_ref}")
        if invoice_status:
            parts.append(f"status={invoice_status}")
        summary = from_json(state.get("invoice_summary"), None)
        if isinstance(summary, dict) and "count" in summary:
            parts.append(f"count={summary['count']}")
        if record_ref:
            parts.append(f"ref={record_ref}")
        if record_id:
            parts.append(f"id={record_id}")
        confirmation = " - ".join(parts)

        # Audit the confirmed action - intent + reference presence (no content).
        emit_trace_event(
            "confirm_complete",
            {"intent": intent, "has_record_ref": bool(record_ref)},
            state,
        )

        return {
            "confirmation": confirmation,
            "result": {
                "record_id": record_id,
                "record_ref": record_ref,
                "confirmation": confirmation,
            },
            "status": AgentStatus.SUCCESS.value,
        }
