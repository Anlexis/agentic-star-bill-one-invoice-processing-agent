# CMN-C2-275 - Unit tests: ConfirmNode (inner Step 5)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input gate
# -> execute -> output gate); inner domain node -> caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.confirm_node import ConfirmNode
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.confirm_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "record_id": "INV-3041",
        "record_ref": "billone://invoices/INV-3041",
        "issuer_name": "Acme",
        "invoice_status": "received",
        "intent": "lookup_invoice",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "confirm-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestConfirmNode:
    def setup_method(self):
        self.node = ConfirmNode()

    def test_lookup_confirmation(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Retrieved invoice record" in result["confirmation"]
        assert "Acme" in result["confirmation"]
        assert "status=received" in result["confirmation"]
        assert "ref=billone://invoices/INV-3041" in result["confirmation"]
        assert "id=INV-3041" in result["confirmation"]
        assert result["result"]["record_id"] == "INV-3041"
        assert result["result"]["record_ref"] == "billone://invoices/INV-3041"

    def test_register_verb(self):
        result = self.node(
            _state(
                intent="register_invoice", record_id="B-778", record_ref="billone://invoices/B-778", invoice_status=""
            )
        )
        assert "Registered invoice" in result["confirmation"]

    def test_summarize_includes_count(self):
        result = self.node(
            _state(
                intent="summarize_invoices",
                record_id="",
                record_ref="billone://invoices",
                issuer_name="",
                invoice_status="",
                invoice_summary=to_json({"count": 3, "by_status": {"received": 1, "approved": 1, "paid": 1}}),
            )
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Summarized invoice records" in result["confirmation"]
        assert "count=3" in result["confirmation"]

    def test_unknown_intent_uses_generic_verb(self):
        result = self.node(_state(intent="mystery"))
        assert "Processed invoice record" in result["confirmation"]

    def test_id_only_no_ref(self):
        result = self.node(_state(record_ref=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "id=INV-3041" in result["confirmation"]
        assert "ref=" not in result["confirmation"]

    def test_falls_back_to_record_id_when_name_missing(self):
        result = self.node(_state(issuer_name=""))
        assert "'INV-3041'" in result["confirmation"]

    def test_missing_record_evidence_errors(self):
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]
