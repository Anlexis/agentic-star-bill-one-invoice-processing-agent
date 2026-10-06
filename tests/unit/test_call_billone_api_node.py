# CMN-C2-275 - Unit tests: CallBillOneApiNode (inner Step 4, tool side-effect)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input gate
# -> execute -> output gate); inner domain node -> caller_trust_level = TrustLevel.ANONYMOUS.value.
# The ONE documented exception: the config-override call passes a 2nd (config)
# argument, which __call__ cannot forward - that single test stays a DIRECT
# execute(state, config=...) call (ANONYMOUS node, trust gate unaffected).
#
# The node builds its client locally (SDK v1 nodes are no-arg), so error-path
# transports are exercised by monkeypatching the module's BillOneClient symbol
# (our own module attribute - never a sys.modules stub of shared.*).

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

import src.nodes.call_billone_api_node as call_node_module
from src.nodes.call_billone_api_node import CallBillOneApiNode, _positive_float
from src.services.billone_client import BillOneApiError, BillOneClient
from src.schemas.state import from_json, to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.call_billone_api_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "billone_payload": to_json({"invoice_number": "INV-3041"}),
        "intent": "lookup_invoice",
        "invoice_id": "INV-3041",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "call-billone-test",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class _FakeErrorClient:
    """Stands in for BillOneClient: lookup raises the documented API error."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def find_invoice(self, invoice_number, api_token):
        raise BillOneApiError(403, "forbidden by integration permissions")


class _FakeEmptyClient:
    """Stands in for BillOneClient: lookup returns no matching invoice record."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def find_invoice(self, invoice_number, api_token):
        return {"invoices": []}


class _FakeLiveClient:
    """Stands in for BillOneClient with a LIVE (non-stub) transport."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = False

    def find_invoice(self, invoice_number, api_token):
        _FakeLiveClient.captured = {"invoice_number": invoice_number, "api_token": api_token}
        return {
            "invoices": [
                {"invoice_number": invoice_number, "issuer_name": f"Issuer {invoice_number}", "status": "received"}
            ]
        }


class TestCallBillOneApiNode:
    def setup_method(self):
        self.node = CallBillOneApiNode()

    def test_lookup_success_via_default_v1_stub(self):
        # Default transport = deterministic, network-free v1 stub; no secret
        # provider bound -> the node runs on the documented stub placeholder.
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "INV-3041"
        assert result["record_ref"] == "billone://invoices/INV-3041"
        assert result["invoice_id"] == "INV-3041"
        assert result["issuer_name"] == "Issuer INV-3041"
        assert result["invoice_status"] == "received"

    def test_register_success_via_default_v1_stub(self):
        state = _state(
            intent="register_invoice",
            invoice_id="B-778",
            billone_payload=to_json({"invoice_data": [{"invoice_number": "B-778", "issuer_name": "Acme"}]}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "B-778"
        assert result["record_ref"] == "billone://invoices/B-778"

    def test_summarize_success_via_default_stub(self):
        state = _state(
            intent="summarize_invoices",
            invoice_id="",
            billone_payload=to_json({"scope": "all"}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_ref"] == "billone://invoices"
        # invoice_summary is a JSON string; the unfiltered stub listing is a
        # deterministic 3-record set cycling received/approved/paid.
        summary = from_json(result["invoice_summary"], {})
        assert summary["count"] == 3
        assert summary["by_status"] == {"received": 1, "approved": 1, "paid": 1}

    def test_summarize_honours_the_validated_caller_scope_and_cap(self):
        """The caller's validated scope/cap reach the listing and change the aggregate."""
        state = _state(
            intent="summarize_invoices",
            invoice_id="",
            billone_payload=to_json({"scope": "approved", "limit": 6}),
        )
        result = self.node(state)
        summary = from_json(result["invoice_summary"], {})
        assert summary["count"] == 6
        assert summary["by_status"] == {"approved": 6}

    def test_request_budget_from_runtime_config_reaches_the_client(self):
        """timeout_s declared in the runtime config must govern the real call."""
        state = _state(billone_config=to_json({"base_url": "https://billone.example.test/v1", "timeout_s": 0.001}))
        captured = {}

        class _SlowClient(BillOneClient):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                captured["timeout_s"] = self.timeout_s

        monkey = _SlowClient
        original = call_node_module.BillOneClient
        call_node_module.BillOneClient = monkey
        try:
            self.node(state)
        finally:
            call_node_module.BillOneClient = original
        assert captured["timeout_s"] == 0.001

    def test_non_finite_request_budget_falls_back_instead_of_disabling_itself(self):
        """NaN compares False against every elapsed time - a budget of NaN is no budget."""
        for hostile in ("NaN", float("nan"), float("inf"), -1, 0, "abc", True, None):
            assert _positive_float(hostile, 30.0) == 30.0
        assert _positive_float(12.5, 30.0) == 12.5

    def test_billone_config_state_field_sets_base_url(self):
        # The inner graph injects the manifest `billone:` section as the JSON
        # billone_config state field; the stub transport still serves the call.
        state = _state(billone_config=to_json({"base_url": "https://billone.example.test/v1"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_ref"] == "billone://invoices/INV-3041"

    def test_config_override_direct_execute_call(self):
        # Documented canon exception: execute(state, config=...) takes a 2nd
        # argument that __call__ cannot forward, so this ONE test calls execute
        # directly (ANONYMOUS node - the trust gate is not the subject here).
        config = {"configurable": {"billone": {"base_url": "https://billone.example.test/v1"}}}
        result = self.node.execute(_state(), config=config)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "INV-3041"

    def test_missing_payload_errors(self):
        result = self.node(_state(billone_payload=None))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_lookup_with_unresolved_number_errors(self):
        state = _state(invoice_id="", billone_payload=to_json({"invoice_number": ""}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved invoice number" in entry for entry in result["error_log"])

    def test_lookup_with_no_matching_record_errors(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_billone_api_node.BillOneClient", _FakeEmptyClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("no invoice record found" in entry for entry in result["error_log"])

    def test_unknown_intent_errors(self):
        result = self.node(_state(intent="delete_invoice"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unknown intent" in entry for entry in result["error_log"])

    def test_api_error_surfaces_status_error(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_billone_api_node.BillOneClient", _FakeErrorClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("403" in entry for entry in result["error_log"])

    def test_live_transport_without_secret_refuses_call(self, monkeypatch):
        # with a LIVE transport a missing BILLONE_TOKEN is a hard error -
        # a real API is never called unauthenticated.
        monkeypatch.setattr("src.nodes.call_billone_api_node.BillOneClient", _FakeLiveClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unauthenticated" in entry for entry in result["error_log"])

    def test_live_transport_reads_token_from_ctx_secrets(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_billone_api_node.BillOneClient", _FakeLiveClient)
        _FakeLiveClient.captured = {}
        with bound_secrets(InMemoryProvider({"BILLONE_TOKEN": "mock-token-for-testing"})):
            result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert _FakeLiveClient.captured["api_token"] == "mock-token-for-testing"
        assert _FakeLiveClient.captured["invoice_number"] == "INV-3041"

    def test_audit_emits_side_effect_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.call_billone_api_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        payload = payloads["call_billone_api_complete"]
        assert payload["intent"] == "lookup_invoice"
        assert payload["has_record_id"] is True
        assert payload["stub_transport"] is True


class TestErrorReasonsAreClosedSet:
    """Every error_log entry this node writes is a closed-set label.

    molt source review (wave-8 batch, 2026-09-04) - the second leak channel.
    `error_log` is the internal channel - post_process never projects it into
    the caller-visible envelope - and it is kept closed-set anyway: a reason
    that interpolates the invoice number stores the record evidence in a log
    the envelope was built to omit, and a reason that echoes the upstream body
    is worse still: a live tenant's error text is unbounded third-party
    content, and a log entry must not be the place it is stored.

    The signal must still travel - the record TYPE, the HTTP STATUS, the
    exception TYPE. It is the interpolated value that is removed, not the
    diagnosis.
    """

    INVOICE_ID = "INV-3041"

    def setup_method(self):
        self.node = CallBillOneApiNode()

    def test_no_matching_record_reason_omits_the_invoice_number(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_billone_api_node.BillOneClient", _FakeEmptyClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        reasons = " ".join(result["error_log"])
        assert "no invoice record found" in reasons, "the diagnosis must survive"
        assert self.INVOICE_ID not in reasons, f"reason interpolated the invoice number: {reasons}"

    def test_api_error_reason_carries_the_status_not_the_upstream_body(self, monkeypatch):
        class _LeakyErrorClient:
            """A tenant whose 403 body quotes the record it refused."""

            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def find_invoice(self, invoice_number, api_token):
                raise BillOneApiError(403, f"denied for Acme Trading K.K. <ap@acme.example> on {invoice_number}")

        monkeypatch.setattr("src.nodes.call_billone_api_node.BillOneClient", _LeakyErrorClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        reasons = " ".join(result["error_log"])
        assert "403" in reasons, "the closed-set signal must still travel"
        assert "Acme Trading" not in reasons
        assert "ap@acme.example" not in reasons
        assert self.INVOICE_ID not in reasons

    def test_transport_failure_reason_carries_the_exception_type_only(self, monkeypatch):
        class _BoomClient:
            """A transport error string carrying the URL and the record id."""

            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def find_invoice(self, invoice_number, api_token):
                raise RuntimeError(f"POST https://billone.example/v1/invoices/{invoice_number} refused")

        monkeypatch.setattr("src.nodes.call_billone_api_node.BillOneClient", _BoomClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        reasons = " ".join(result["error_log"])
        assert "RuntimeError" in reasons, "the exception TYPE is the signal that must travel"
        assert self.INVOICE_ID not in reasons
        assert "https://" not in reasons

    def test_control_the_clean_call_still_returns_record_evidence(self):
        """CONTROL. Without it the assertions above pass vacuously - a node
        that errored on everything, or wrote no reasons at all, would satisfy
        them."""
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == self.INVOICE_ID
        assert result["record_ref"] == f"billone://invoices/{self.INVOICE_ID}"
