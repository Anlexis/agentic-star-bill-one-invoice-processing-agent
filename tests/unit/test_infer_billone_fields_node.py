# CMN-C2-275 - Unit tests: InferBillOneFieldsNode (inner Step 3)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input gate
# -> execute -> output gate); inner domain node -> caller_trust_level = TrustLevel.ANONYMOUS.value.
# Positive payloads are PII-free: the framework PII mask rewrites Title-Case
# bigrams in validated_input, so quoted issuer names use a single-word name.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.infer_billone_fields_node import InferBillOneFieldsNode
from src.schemas.state import from_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.infer_billone_fields_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str, intent: str = "lookup_invoice", invoice_hint: str = "", **overrides) -> dict:
    state = {
        "validated_input": text,
        "intent": intent,
        "invoice_hint": invoice_hint,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "infer-fields-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestInferBillOneFieldsNode:
    def setup_method(self):
        self.node = InferBillOneFieldsNode()

    def test_lookup_extracts_number_from_text(self):
        result = self.node(
            _state("Look up the invoice status for invoice number INV-3041 and report back the record on file.")
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["invoice_id"] == "INV-3041"
        # billone_payload is stored as a JSON string, not a native dict.
        assert isinstance(result["billone_payload"], str)
        assert from_json(result["billone_payload"], {}) == {"invoice_number": "INV-3041"}

    def test_number_shaped_hint_used_when_text_has_no_number(self):
        result = self.node(_state("Show the current invoice record on file", invoice_hint="B-778"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["invoice_id"] == "B-778"
        assert from_json(result["billone_payload"], {}) == {"invoice_number": "B-778"}

    def test_register_builds_invoice_data_payload(self):
        # Field values stay lower-case: the framework PII name mask rewrites
        # Title-Case word pairs even ACROSS newlines ("sales\nAmount" is safe;
        # "Cost Center" would be masked before execute() sees the text).
        text = 'Register a new invoice from "Acme"\nDepartment: sales\nAmount due: 4200'
        result = self.node(_state(text, intent="register_invoice", invoice_hint="B-778"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["invoice_id"] == "B-778"
        assert result["issuer_name"] == "Acme"
        payload = from_json(result["billone_payload"], {})
        record = payload["invoice_data"][0]
        assert record["invoice_number"] == "B-778"
        assert record["issuer_name"] == "Acme"
        assert {"name": "Department", "values": ["sales"]} in record["custom_fields"]
        assert {"name": "Amount due", "values": ["4200"]} in record["custom_fields"]

    def test_summarize_builds_scope_payload(self):
        result = self.node(_state("Summarize the invoices received this month", intent="summarize_invoices"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert from_json(result["billone_payload"], {}) == {"scope": "received"}

    def test_unresolved_number_left_empty_never_invented(self):
        result = self.node(_state("Show the invoice record for the flagged vendor"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["invoice_id"] == ""
        assert from_json(result["billone_payload"], {}) == {"invoice_number": ""}

    def test_non_number_shaped_hint_left_unresolved(self):
        result = self.node(_state("Show the invoice record on file", invoice_hint="not a valid number!"))
        assert result["invoice_id"] == ""

    def test_missing_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]


class _FakeLLM:
    """Test double for the LLM gap-filler: complete(messages) -> {"content": ...}."""

    def __init__(self, content: str | None = None, raises: bool = False):
        self.content = content
        self.raises = raises
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        if self.raises:
            raise RuntimeError("simulated LLM API failure")
        return {"content": self.content}


class TestInferBillOneFieldsNodeLLMGapFill:
    """LLM gap-fill: only fills a field the deterministic parse left empty,
    never overrides a regex-confirmed match, and degrades silently on any
    failure - see src/nodes/infer_billone_fields_node.py's module docstring.
    """

    def test_llm_fills_number_and_issuer_when_regex_finds_neither(self):
        fake = _FakeLLM('{"invoice_number": "B-9001", "issuer_name": "Acme"}')
        node = InferBillOneFieldsNode(llm=fake)
        result = node(_state("Show the invoice record for the flagged vendor"))
        assert result["invoice_id"] == "B-9001"
        assert result["issuer_name"] == "Acme"
        assert fake.calls == 1

    def test_llm_response_wrapped_in_markdown_fence_still_parses(self):
        fake = _FakeLLM('Sure, here it is:\n```json\n{"invoice_number": "B-9001", "issuer_name": null}\n```')
        node = InferBillOneFieldsNode(llm=fake)
        result = node(_state("Show the invoice record for the flagged vendor"))
        assert result["invoice_id"] == "B-9001"
        assert result["issuer_name"] == ""

    def test_llm_never_overrides_a_regex_confirmed_number(self):
        # Text already has an explicit number - the LLM (wrongly) proposes a
        # different one; the regex-confirmed value must win.
        fake = _FakeLLM('{"invoice_number": "WRONG-1", "issuer_name": "Acme"}')
        node = InferBillOneFieldsNode(llm=fake)
        result = node(
            _state("Look up the invoice status for invoice number INV-3041 and report back the record on file.")
        )
        assert result["invoice_id"] == "INV-3041"
        # issuer_name WAS empty from regex, so the LLM fill is accepted for it.
        assert result["issuer_name"] == "Acme"

    def test_malformed_json_response_falls_back_to_heuristic(self):
        fake = _FakeLLM("not json at all")
        node = InferBillOneFieldsNode(llm=fake)
        result = node(_state("Show the invoice record for the flagged vendor"))
        assert result["invoice_id"] == ""
        assert result["issuer_name"] == ""

    def test_wrong_shape_response_falls_back_to_heuristic(self):
        # invoice_number is a number, not a string - rejected by the shape check.
        fake = _FakeLLM('{"invoice_number": 9001, "issuer_name": ""}')
        node = InferBillOneFieldsNode(llm=fake)
        result = node(_state("Show the invoice record for the flagged vendor"))
        assert result["invoice_id"] == ""
        assert result["issuer_name"] == ""

    def test_llm_raising_falls_back_to_heuristic(self):
        fake = _FakeLLM(raises=True)
        node = InferBillOneFieldsNode(llm=fake)
        result = node(_state("Show the invoice record for the flagged vendor"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["invoice_id"] == ""
        assert result["issuer_name"] == ""

    def test_no_llm_injected_and_no_secret_bound_falls_back_to_heuristic(self):
        # The real production shape in any environment without a configured
        # key: no llm= injected, and InvocationContext.from_state(state) fails
        # (this fixture carries no session_id/thread_id/trace_id) before any
        # secret is even looked up.
        node = InferBillOneFieldsNode()
        result = node(_state("Show the invoice record for the flagged vendor"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["invoice_id"] == ""
        assert result["issuer_name"] == ""

    def test_summarize_intent_never_calls_the_llm(self):
        fake = _FakeLLM('{"invoice_number": "B-1", "issuer_name": "Acme"}')
        node = InferBillOneFieldsNode(llm=fake)
        node(_state("Summarize the invoices received this month", intent="summarize_invoices"))
        assert fake.calls == 0

    def test_fully_resolved_by_regex_never_calls_the_llm(self):
        fake = _FakeLLM('{"invoice_number": "B-1", "issuer_name": "Acme"}')
        node = InferBillOneFieldsNode(llm=fake)
        text = 'Register a new invoice from "Acme"\nDepartment: sales\nAmount due: 4200'
        node(_state(text, intent="register_invoice", invoice_hint="B-778"))
        assert fake.calls == 0

    def test_empty_input_never_calls_the_llm(self):
        fake = _FakeLLM('{"invoice_number": "B-1", "issuer_name": "Acme"}')
        node = InferBillOneFieldsNode(llm=fake)
        node(_state(""))
        assert fake.calls == 0
