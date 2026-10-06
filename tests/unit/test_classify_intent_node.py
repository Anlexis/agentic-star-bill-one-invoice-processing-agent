# CMN-C2-275 - Unit tests: ClassifyIntentNode (inner Step 2)
# Intents: lookup_invoice / register_invoice / summarize_invoices (deterministic
# keyword heuristic, v1 - no LLM; unknown falls back to the read-only lookup).
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input gate
# -> execute -> output gate); inner domain node -> caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.classify_intent_node import ClassifyIntentNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.classify_intent_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str) -> dict:
    return {
        "validated_input": text,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "classify-intent-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }


class TestClassifyIntentNode:
    def setup_method(self):
        self.node = ClassifyIntentNode()

    def test_keyword_lookup_invoice(self):
        result = self.node(_state("Look up the invoice status for invoice number INV-3041."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_invoice"

    def test_keyword_register_invoice(self):
        result = self.node(_state("Register a new invoice with number B-778"))
        assert result["intent"] == "register_invoice"

    def test_keyword_summarize_invoices(self):
        result = self.node(_state("Summarize the invoices received this month by vendor"))
        assert result["intent"] == "summarize_invoices"

    def test_write_keyword_wins_over_lookup(self):
        # Priority order is the write first: a "register ... then check it"
        # style request classifies as the write, never the read.
        result = self.node(_state("Register the invoice with number B-778 and check the status"))
        assert result["intent"] == "register_invoice"

    def test_summarize_wins_over_lookup(self):
        # summarize is checked before lookup so an aggregate request never
        # degrades to a single-record lookup.
        result = self.node(_state("Summarize the invoices on file and check the totals"))
        assert result["intent"] == "summarize_invoices"

    def test_no_signal_defaults_to_readonly_lookup(self):
        result = self.node(_state("please handle this for the team"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_invoice"
        # Non-fatal low-confidence note travels in error_log; status stays SUCCESS.
        assert any("defaulted to lookup_invoice" in entry for entry in result.get("error_log", []))

    def test_empty_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_intent_label_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.classify_intent_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state("Look up the invoice status for invoice number INV-3041."))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - the label, never the text.
        assert payloads["classify_intent_complete"]["intent"] == "lookup_invoice"
        assert payloads["classify_intent_complete"]["defaulted"] is False
