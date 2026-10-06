"""AgentCore Platform v1.0 - inner Bill One workflow graph (Cat 2 domain workflow).

Instantiated by BillOneWorkflowGraphNode.get_subgraph() in graph.py. Inherits
BaseGraph directly for a fully custom linear topology:

    START -> validate_input -> classify_intent -> infer_billone_fields
          -> call_billone_api -> confirm -> END

Config (forwarded from the outer graph via _parent_config(), under
config["configurable"]):
    billone    - integration settings (base_url, ...) from config/config.yaml;
                 injected into State as the JSON `billone_config` field via
                 _extra_initial_state() so the no-arg nodes can read it
    timeout_s  - per-call request budget, carried alongside the integration
                 settings and enforced by the client

Nodes are registered WITHOUT constructor arguments (SDK v1 nodes are no-arg;
ctor args raise TypeError at graph build).
"""

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus
from src.nodes.validate_input_node import ValidateInputNode
from src.nodes.classify_intent_node import ClassifyIntentNode
from src.nodes.infer_billone_fields_node import InferBillOneFieldsNode
from src.nodes.call_billone_api_node import CallBillOneApiNode
from src.nodes.confirm_node import ConfirmNode
from src.schemas.state import State, to_json


class BillOneWorkflowGraph(BaseGraph):
    """Inner graph: NL -> validate -> classify -> infer -> call -> confirm."""

    @property
    def name(self) -> str:
        return "billone_invoice_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # No mandatory config: the billone section is optional (the client
        # falls back to the documented default base_url and the network-free
        # stub transport), and a missing or unusable setting is handled at
        # CallBillOneApiNode.execute() as a graceful status=error rather than
        # a compile-time crash.
        pass

    def register_nodes(self) -> None:
        # No super() - BaseGraph.register_nodes() is abstract. Do NOT register
        # initialize / finalize (outer backbone concern). All nodes are no-arg.
        self._nodes["validate_input"] = ValidateInputNode()
        self._nodes["classify_intent"] = ClassifyIntentNode()
        self._nodes["infer_billone_fields"] = InferBillOneFieldsNode()
        self._nodes["call_billone_api"] = CallBillOneApiNode()
        self._nodes["confirm"] = ConfirmNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "validate_input")
        self._sg.add_edge("validate_input", "classify_intent")
        self._sg.add_edge("classify_intent", "infer_billone_fields")
        self._sg.add_edge("infer_billone_fields", "call_billone_api")
        self._sg.add_edge("call_billone_api", "confirm")
        self._sg.add_edge("confirm", END)

    def route(self, state: State) -> str:
        """Required by the BaseGraph contract; the topology above is linear.

        The annotation is this graph's OWN State, not the shared base state, and
        that is load-bearing rather than cosmetic: a routing callable's
        annotation IS its input schema, and any field the annotation does not
        declare is projected away before the callable sees it. Annotating a
        route with the base state makes every domain field read as absent, so
        the branch it guards can never be taken while unit tests that call the
        method directly still pass.
        """
        return END if state.get("status") == AgentStatus.ERROR.value else "confirm"

    def _extra_initial_state(self) -> "dict[str, Any]":
        # Forward the integration settings (arriving under config["configurable"]
        # from _parent_config()) into State as a JSON string so the no-arg
        # CallBillOneApiNode can read them via state.get("billone_config").
        configurable = self.config.get("configurable") or {}
        settings = dict(configurable.get("billone") or {})
        if "timeout_s" in configurable:
            settings["timeout_s"] = configurable["timeout_s"]
        if not settings:
            return {}
        return {"billone_config": to_json(settings)}

    def get_output(self, state: State) -> "dict[str, Any]":
        return {
            "output": state.get("result") or state.get("confirmation"),
            "status": state.get("status"),
            "intent": state.get("intent", ""),
            "invoice_id": state.get("invoice_id", ""),
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "issuer_name": state.get("issuer_name", ""),
            "invoice_status": state.get("invoice_status", ""),
            "invoice_summary": state.get("invoice_summary", ""),
            "confirmation": state.get("confirmation", ""),
            "billone_payload": state.get("billone_payload", ""),
            "redaction_flags": state.get("redaction_flags", ""),
            "error_log": state.get("error_log", []),
            "trace_id": state.get("trace_id", ""),
            "correlation_id": state.get("correlation_id", ""),
            "node_history": state.get("node_history", []),
        }
