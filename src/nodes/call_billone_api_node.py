"""AgentCore Platform v1.0 - inner workflow Step 4: CallBillOneApi (tool side-effect).

Performs the lookup/register/summarize call against the Bill One REST API
invoice endpoints via src/services/billone_client.py.

Security posture:
  Trust: required_trust_level = ANONYMOUS. The single external trust gate lives
       on the OUTER backbone pre_process (VERIFIED_EXTERNAL), not on this inner
       node. GraphNode.execute() passes the caller's InvocationContext into the
       inner subgraph UNCHANGED (no trust elevation), so a real external caller
       runs this call under its own VERIFIED_EXTERNAL context; declaring
       INTERNAL here would deny that already-gated external caller before the
       call ever runs. The node therefore stays ANONYMOUS.
  Credentials: the integration token is read via
       ctx.secrets.get("BILLONE_TOKEN") (InvocationContext.from_state(state)) -
       never os.environ, never stored in state. While the deterministic
       NETWORK-FREE stub transport is active a missing token is tolerated (a
       sentinel placeholder is used - it is never sent anywhere because no
       request leaves the process); with a LIVE transport injected, a missing
       token is a hard status=error - a real API is never called
       unauthenticated.
  Audit: emit_trace_event() is called on the success path - a side-effect
       against an external invoice system; HTTP 4xx/5xx surfaces as
       status=error + error_log (no silent pass).
  Error reasons: every error_log entry this node writes carries CLOSED-SET
       labels only - the HTTP status, the exception type, a fixed phrase - and
       never an invoice number, an issuer name, or an upstream response body.
       error_log is the INTERNAL channel (the audit trail reads it; post_process
       never projects it into the caller-visible envelope) and it is kept
       closed-set anyway: an upstream body is unbounded third-party text that
       can quote the very record it refused, and a log entry must not be the
       place that text is stored.

Configuration: this node takes NO constructor arguments (SDK v1 nodes are
no-arg). Bill One settings (base_url, timeout_s) arrive as the JSON
`billone_config` state field - injected by the inner graph's
_extra_initial_state() from the runtime section forwarded by
BillOneWorkflowGraphNode._parent_config() - or via the optional
`config["configurable"]["billone"]` argument for direct invocation. The client
is constructed locally per call (no module-global mutation).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json, to_json
from src.services.billone_client import DEFAULT_TIMEOUT_S, BillOneApiError, BillOneClient

_SECRET_KEY = "BILLONE_TOKEN"
# Placeholder handed to the network-free stub transport when no secret is
# provisioned. Never sent over any network (the stub performs no I/O) and never
# written to state or logs.
_STUB_PLACEHOLDER = "stub-transport-no-credential"


class CallBillOneApiNode(FunctionNode):
    """Look up / register / summarize Bill One invoice records via the REST API."""

    # The external trust gate is enforced UPSTREAM on the outer backbone
    # pre_process (VERIFIED_EXTERNAL). This inner node runs under the caller's
    # UNELEVATED context (GraphNode does not elevate trust for the subgraph), so
    # it must stay ANONYMOUS - declaring INTERNAL would deny a real external
    # caller before the call runs.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]", config: "dict[str, Any] | None" = None) -> "dict[str, Any]":
        payload = from_json(state.get("billone_payload"), None)
        if not payload:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallBillOneApiNode: missing billone_payload"],
            }

        intent = state.get("intent", "lookup_invoice") or "lookup_invoice"

        # Settings: manifest section from state (graph-injected), overridable via
        # an explicit config["configurable"]["billone"] for direct invocation.
        # Merged into a LOCAL dict - module globals are never mutated.
        settings = dict(from_json(state.get("billone_config"), {}) or {})
        override = ((config or {}).get("configurable") or {}).get("billone") or {}
        settings.update(override)

        # Client built locally per call; with no injected transport it uses the
        # deterministic NETWORK-FREE stub (documented limitation, see the design
        # notes). The request budget comes from the runtime config; a value that
        # is not a usable positive number falls back to the documented default
        # rather than disabling the budget.
        base_url = str(settings.get("base_url", "") or "").strip()
        timeout_s = _positive_float(settings.get("timeout_s"), DEFAULT_TIMEOUT_S)
        client = (
            BillOneClient(base_url=base_url, timeout_s=timeout_s) if base_url else BillOneClient(timeout_s=timeout_s)
        )

        # Token from the bound secret provider - never os.environ / state.
        ctx = InvocationContext.from_state(state)
        api_token = ctx.secrets.get(_SECRET_KEY)
        if api_token is None:
            if client.uses_stub_transport:
                # v1 stub limitation: no request leaves the process, so run with
                # a non-credential placeholder (see module docstring).
                api_token = _STUB_PLACEHOLDER
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        f"CallBillOneApiNode: secret {_SECRET_KEY} unavailable - "
                        "refusing to call a live transport unauthenticated"
                    ],
                }

        invoice_id = state.get("invoice_id", "") or str(payload.get("invoice_number", "") or "")
        invoice_status = ""
        invoice_summary: "dict[str, Any] | None" = None

        try:
            if intent == "lookup_invoice":
                if not invoice_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallBillOneApiNode: unresolved invoice number - cannot look up invoice"],
                    }
                resp = client.find_invoice(invoice_id, api_token) or {}
                invoices = resp.get("invoices") or []
                if not invoices:
                    # Closed-set reason, no invoice number. error_log is the
                    # internal audit channel - never projected to the caller -
                    # and a log entry is still not the place to store the
                    # record evidence the error envelope deliberately omits.
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallBillOneApiNode: no invoice record found for the requested number"],
                    }
                record = invoices[0]
                record_id = str(record.get("invoice_number", "")) or invoice_id
                record_ref = f"billone://invoices/{record_id}"
                issuer_name = state.get("issuer_name", "") or str(record.get("issuer_name", ""))
                invoice_status = str(record.get("status", ""))
            elif intent == "register_invoice":
                resp = client.register_invoice(payload, api_token) or {}
                record_id = str(resp.get("invoice_number", "")) or invoice_id
                record_ref = f"billone://invoices/{record_id}" if record_id else ""
                issuer_name = state.get("issuer_name", "")
            elif intent == "summarize_invoices":
                resp = client.list_invoices(dict(payload), api_token) or {}
                invoices = resp.get("invoices") or []
                by_status: "dict[str, int]" = {}
                for record in invoices:
                    key = str(record.get("status", "unknown")) or "unknown"
                    by_status[key] = by_status.get(key, 0) + 1
                invoice_summary = {"count": len(invoices), "by_status": by_status}
                record_id = str(invoices[0].get("invoice_number", "")) if invoices else ""
                record_ref = "billone://invoices"
                issuer_name = state.get("issuer_name", "")
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"CallBillOneApiNode: unknown intent '{intent}'"],
                }
        except BillOneApiError as exc:
            # HTTP status only. A live tenant's error body is unbounded
            # third-party text that can quote the very record it refused
            # (invoice number, issuer name, AP contact address). error_log is
            # internal, but a log entry is not the place to store that text:
            # the closed-set signal is recorded and the upstream body is not.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallBillOneApiNode: Bill One API error {exc.status_code}"],
            }
        except Exception as exc:  # transport failure - no silent pass
            # Exception CLASS only, for the same reason: a transport error string
            # can carry the request URL and the invoice number inside it.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallBillOneApiNode: Bill One call failed ({type(exc).__name__})"],
            }

        # Audit the tool side-effect - intent + presence signals only, never
        # invoice content or credentials.
        emit_trace_event(
            "call_billone_api_complete",
            {
                "intent": intent,
                "has_record_id": bool(record_id),
                "stub_transport": client.uses_stub_transport,
            },
            state,
        )

        result = {
            "record_id": record_id,
            "record_ref": record_ref,
            "invoice_id": invoice_id or record_id,
            "issuer_name": issuer_name,
            "invoice_status": invoice_status,
            "status": AgentStatus.SUCCESS.value,
        }
        if invoice_summary is not None:
            result["invoice_summary"] = to_json(invoice_summary)
        return result


def _positive_float(value: object, default: float) -> float:
    """Read a positive, finite number from the runtime config, or fall back.

    A budget of NaN would compare False against every elapsed time and quietly
    disable itself, which is the failure direction that matters: the setting
    would look configured and enforce nothing.
    """
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")) or number <= 0:
        return default
    return number
