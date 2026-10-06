"""AgentCore Platform v1.0 - CMN-C2-275 outer graph (Cat 2).

Cat 2: fixed 5-node backbone (initialize -> pre_process -> main -> post_process ->
finalize). Domain complexity is encapsulated in BillOneWorkflowGraphNode (`main`
slot), which wraps the inner BillOneWorkflowGraph (validate -> classify -> infer ->
call -> confirm). add_edges() is NOT overridden - backbone wiring is the
framework's concern.

Runtime settings live in ``config/config.yaml`` (the manifest ``config/agent.yaml``
is the static registration record and carries no runtime values). The agent is
constructed with that file's contents as its config, and the same mapping is
forwarded to the inner graph — see ``load_runtime_config()`` and
``BillOneWorkflowGraphNode._parent_config()``.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

import yaml

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.post_process_node import ERROR_REASONS, PostProcessNode, _REASON_WORKFLOW_FAILED, error_envelope
from src.schemas.state import State

if TYPE_CHECKING:  # import cycle: the inner graph imports this package at runtime
    from src.graph.domain_workflow_graph import BillOneWorkflowGraph

# Runtime config: src/graph/graph.py -> parents[2] = repo root.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def load_runtime_config(path: Path = _RUNTIME_CONFIG_PATH) -> "dict[str, Any]":
    """Load ``config/config.yaml`` — the agent's runtime parameters.

    This is the mapping the platform passes to the graph constructor. The
    standalone entry point loads it the same way so a value declared once takes
    effect on every path; a config file that is only read on one of them is a
    setting that silently does nothing on the other.

    A missing or unreadable file degrades to ``{}`` — the framework and the Bill
    One client both carry documented defaults — rather than failing start-up on
    a file the deployment may legitimately not have mounted.
    """
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


class BillOneWorkflowGraphNode(GraphNode):
    """Wraps the inner Bill One workflow graph; assigned to the `main` slot.

    No constructor arguments (SDK v1 nodes are no-arg) - configuration reaches
    the subgraph via _parent_config(), which reads the OUTER graph's config.
    """

    # Fail fast: re-raise inner-graph exceptions as SubgraphError (default).
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def __init__(self) -> None:
        # Nodes take no constructor arguments (SDK v1 contract), so the owning
        # graph assigns the runtime config after construction.
        super().__init__()
        self.parent_config: "dict[str, Any]" = {}

    def get_subgraph(self) -> "BillOneWorkflowGraph":
        from src.graph.domain_workflow_graph import BillOneWorkflowGraph

        return BillOneWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        # pre_process serialized the request into validated_input (JSON string);
        # structured params travel as JSON and the first inner node parses them back.
        return str(state.get("validated_input") or state.get("user_input", ""))

    def merge_output(self, state: AgentState, sub_result: "dict[str, Any]") -> "dict[str, Any]":
        # Map only the keys this node changes back into the outer state.
        return {
            "result": sub_result.get("output"),
            "status": sub_result.get("status"),
            "intent": sub_result.get("intent", ""),
            "invoice_id": sub_result.get("invoice_id", ""),
            "record_id": sub_result.get("record_id", ""),
            "record_ref": sub_result.get("record_ref", ""),
            "issuer_name": sub_result.get("issuer_name", ""),
            "invoice_status": sub_result.get("invoice_status", ""),
            "invoice_summary": sub_result.get("invoice_summary", ""),
            "confirmation": sub_result.get("confirmation", ""),
            "billone_payload": sub_result.get("billone_payload", ""),
            "redaction_flags": sub_result.get("redaction_flags", ""),
            "error_log": sub_result.get("error_log", []),
        }

    def _parent_config(self) -> "dict[str, Any]":
        """Forward the runtime config to the inner graph under config["configurable"].

        Forwards the ``billone:`` integration section and the request budget
        (``timeout_s``) from ``config/config.yaml`` — never an empty mapping,
        which is the failure mode that makes every declared setting dead while
        the suite stays green.
        """
        runtime = self.parent_config if isinstance(self.parent_config, dict) else {}
        configurable: "dict[str, Any]" = {}
        billone = runtime.get("billone")
        if isinstance(billone, dict):
            configurable["billone"] = dict(billone)
        if "timeout_s" in runtime:
            configurable["timeout_s"] = runtime["timeout_s"]
        return {"configurable": configurable}


class BillOneInvoiceAgent(AgentBaseGraph):
    """CMN-C2-275 outer graph - Bill One Invoice Agent.

    Backbone: initialize -> pre_process -> main -> post_process -> finalize (fixed).
    Domain logic lives in BillOneWorkflowGraphNode (`main` slot); Bill One
    settings flow from config/config.yaml via _parent_config().
    """

    def __init__(self, config: "dict[str, Any] | None" = None) -> None:
        # Default to the repo's runtime config so a directly-constructed agent
        # behaves like a platform-constructed one. The framework reads
        # max_retry / hitl / memory_enabled straight off this mapping.
        super().__init__(config if config is not None else load_runtime_config())

    @property
    def name(self) -> str:
        return "cmn_c2_275"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        main_node = BillOneWorkflowGraphNode()
        main_node.parent_config = self.config
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = main_node
        self._nodes["post_process"] = PostProcessNode()

    def get_output(self, state: AgentState) -> "dict[str, Any]":
        """The invoke envelope: closed-set labels only on every non-success outcome.

        AgentBaseGraph.get_output() returns ``{output, status, trace_id,
        correlation_id, node_history}`` with ``output = formatted_output or
        result`` and no status check. On the real backbone an inner-workflow
        error is routed straight to ``finalize`` - post_process never runs, so
        no envelope was ever built and ``output`` would fall back to whatever
        the inner graph left in ``result``. This override EXTENDS the base
        envelope (status / trace_id / correlation_id / node_history are kept)
        and closes that fallback:

        * non-success: ``output`` is withheld (``None``) and ``error`` carries
          the same closed-set envelope PostProcessNode builds - its own reason
          when it ran, ``billone_workflow_failed`` otherwise. Never an
          ``error_log`` line, a gate message or an exception's text;
          ``error_log`` stays the internal audit channel and is not projected.
          Anything else found in the ``formatted_output`` slot is not trusted.
        * success: the gated ``formatted_output`` dict, unchanged. A success
          status without a gated dict is not trusted either - ``output`` is
          withheld rather than falling back to the pre-gate ``result``.
        """
        output = cast("dict[str, Any]", super().get_output(state))
        formatted = state.get("formatted_output")
        if state.get("status") != AgentStatus.SUCCESS.value:
            reason = formatted.get("reason") if isinstance(formatted, dict) else None
            if not (isinstance(reason, str) and reason in ERROR_REASONS):
                reason = _REASON_WORKFLOW_FAILED
            output["output"] = None
            output["error"] = error_envelope(reason)
            return output
        if not isinstance(formatted, dict):
            output["output"] = None
        return output

    # add_edges() is NOT overridden - backbone wiring belongs to the framework.
