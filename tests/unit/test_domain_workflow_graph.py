# CMN-C2-275 - Unit tests: inner BillOneWorkflowGraph (BaseGraph) contract.
# The compiled outer path is
# exercised end-to-end by tests/proof_of_boundary/test_pb_invoke_order.py; this
# module unit-checks the inner graph's identity, config forwarding, routing,
# output contract, and a direct inner invoke on the network-free v1 stub.


from langgraph.graph import END

from framework.schemas.agent_status import AgentStatus

from src.graph.domain_workflow_graph import BillOneWorkflowGraph
from src.schemas.state import State, from_json


def _graph(config=None):
    return BillOneWorkflowGraph(config=config or {})


def test_inner_graph_identity():
    g = _graph()
    assert g.name == "billone_invoice_workflow"
    assert g.state_schema is State


def test_extra_initial_state_injects_billone_config_as_json():
    g = _graph({"configurable": {"billone": {"base_url": "https://billone.example.test/v1"}}})
    extra = g._extra_initial_state()
    # forwarded as a JSON string, not a native dict.
    assert isinstance(extra["billone_config"], str)
    assert from_json(extra["billone_config"], {}) == {"base_url": "https://billone.example.test/v1"}


def test_extra_initial_state_empty_without_billone_section():
    assert _graph()._extra_initial_state() == {}


def test_route_error_ends_graph():
    g = _graph()
    assert g.route({"status": AgentStatus.ERROR.value}) == END
    assert g.route({"status": AgentStatus.SUCCESS.value}) == "confirm"


def test_get_output_surfaces_record_fields():
    g = _graph()
    out = g.get_output(
        {
            "result": {"record_id": "INV-3041", "record_ref": "billone://invoices/INV-3041", "confirmation": "ok"},
            "status": AgentStatus.SUCCESS.value,
            "intent": "lookup_invoice",
            "invoice_id": "INV-3041",
            "record_id": "INV-3041",
            "record_ref": "billone://invoices/INV-3041",
            "issuer_name": "Acme",
            "invoice_status": "received",
            "invoice_summary": "",
            "confirmation": "ok",
            "billone_payload": "{}",
            "redaction_flags": "[]",
            "error_log": [],
            "trace_id": "tr",
            "correlation_id": "co",
            "node_history": ["ValidateInputNode", "ConfirmNode"],
        }
    )
    assert out["status"] == AgentStatus.SUCCESS.value
    assert out["intent"] == "lookup_invoice"
    assert out["record_ref"] == "billone://invoices/INV-3041"
    assert out["invoice_status"] == "received"
    assert out["confirmation"] == "ok"
    assert out["output"] == {"record_id": "INV-3041", "record_ref": "billone://invoices/INV-3041", "confirmation": "ok"}


def test_get_output_carries_error_log():
    g = _graph()
    out = g.get_output({"status": AgentStatus.ERROR.value, "error_log": ["boom"], "confirmation": ""})
    assert out["status"] == AgentStatus.ERROR.value
    assert out["error_log"] == ["boom"]


def test_inner_graph_compiles():
    g = _graph()
    g.compile()
    assert g._compiled is not None


def test_inner_invoke_lookup_on_v1_stub():
    """Direct inner invoke (default ANONYMOUS ctx - every inner node is
    ANONYMOUS): validate -> classify -> infer -> call(stub) -> confirm."""
    g = _graph({"configurable": {"billone": {"base_url": "https://api.billone.jp/v1"}}})
    g.compile()
    result = g.invoke(
        user_input="Look up the invoice status for invoice number INV-3041 and report back the record on file."
    )
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["record_id"] == "INV-3041"
    assert result["record_ref"] == "billone://invoices/INV-3041"
    assert result["intent"] == "lookup_invoice"
    assert result["confirmation"]
    history = result.get("node_history", [])
    assert history == [
        "ValidateInputNode",
        "ClassifyIntentNode",
        "InferBillOneFieldsNode",
        "CallBillOneApiNode",
        "ConfirmNode",
    ]


class TestRoutingCallableAnnotations:
    """A routing callable's annotation IS its input schema.

    The graph engine projects away every field the annotation does not declare
    before the callable runs. A path callable annotated with the shared base
    state therefore sees every domain field as absent, so the branch it guards
    can never be taken - while a unit test that calls the method directly, with
    a full dict, keeps passing. The topology here is linear today; this guard
    exists so that stays true if a branch is ever added.
    """

    def test_route_is_annotated_with_this_graphs_own_state(self):
        import typing

        hints = typing.get_type_hints(BillOneWorkflowGraph.route)
        assert hints["state"] is State

    def test_every_conditional_path_callable_is_annotated_with_the_graphs_state(self):
        """Whatever the topology becomes, the annotation rule holds for it."""
        import ast
        import importlib
        import pathlib
        import typing

        module = importlib.import_module(BillOneWorkflowGraph.__module__)
        source = pathlib.Path(module.__file__)
        tree = ast.parse(source.read_text(encoding="utf-8"))
        path_callables = [
            node.args[1].attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "add_conditional_edges"
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Attribute)
        ]
        for name in path_callables:
            hints = typing.get_type_hints(getattr(BillOneWorkflowGraph, name))
            assert hints.get("state") is State, name

    def test_the_domain_fields_a_router_would_read_are_declared_on_the_state(self):
        """The projection only preserves declared fields, so they must be declared."""
        declared: set = set()
        for klass in State.__mro__:
            declared |= set(getattr(klass, "__annotations__", {}))
        for field in ("intent", "invoice_id", "record_id", "listing_scope", "max_records"):
            assert field in declared, field
