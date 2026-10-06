# CMN-C2-275 - Unit tests: manifest + runtime-config sanity.
#
# Two files, two jobs, and the split matters: config/agent.yaml is the static
# registration record read at ROOT level (no `agent:` block), and
# config/config.yaml holds the runtime parameters handed to the graph
# constructor. A test that reads a runtime value out of the manifest would pass
# while the value reached nothing.

import pathlib

import pytest

try:
    import yaml  # pyyaml (transitive dep of the framework wheel)

    _YAML_ERROR = None
except Exception as exc:  # pragma: no cover
    yaml = None
    _YAML_ERROR = exc

_ROOT = pathlib.Path(__file__).parents[2]
_MANIFEST_PATH = _ROOT / "config" / "agent.yaml"
_RUNTIME_PATH = _ROOT / "config" / "config.yaml"

pytestmark = pytest.mark.skipif(_YAML_ERROR is not None, reason=f"pyyaml unavailable: {_YAML_ERROR}")


def _manifest():
    return yaml.safe_load(_MANIFEST_PATH.read_text())


def _runtime():
    return yaml.safe_load(_RUNTIME_PATH.read_text())


def test_manifest_identity():
    data = _manifest()
    assert data["id"] == "CMN-C2-275"
    assert data["category"] == "Cat 2"
    assert data["industry"] == "CMN"
    assert data["namespace"] == "cmn"
    assert data["base_type"] == "ToolCallingAgent"
    assert data["enabled"] is True


def test_manifest_is_flat_not_nested():
    """The registry reads every key at root level; an `agent:` block hides them all."""
    assert "agent" not in _manifest()


def test_manifest_entry_point():
    assert _manifest()["class"] == "src.graph.graph.BillOneInvoiceAgent"


def test_manifest_security():
    data = _manifest()
    # Agent-level entry trust, enforced by the outer backbone pre_process gate
    # (VERIFIED_EXTERNAL); inner domain nodes stay ANONYMOUS.
    assert data["required_trust_level"] == "VERIFIED_EXTERNAL"


def test_manifest_declares_no_unprovisioned_compile_gates():
    """requires.* is a COMPILE gate, not documentation.

    The integration token is read with ctx.secrets.get(), never .require(), and
    the pipeline runs without it on the network-free transport. Declaring it
    here would refuse to compile the agent wherever it is not provisioned.

    InferBillOneFieldsNode's LLM gap-filler (Step 8e) DOES construct a real model
    client (AzureOpenAIClient) and DOES read three Azure OpenAI secrets via
    ctx.secrets.require() - but requires.secrets stays [] regardless: that
    enhancement is optional and per-namespace secret provisioning is not
    fleet-wide guaranteed, so declaring them would refuse to compile this agent
    wherever they are absent, exactly the failure a graceful-degrade design must
    not have. requires.extras DOES list "openai" - unlike a secret, that
    package's presence is fleet-wide guaranteed (every CI/deploy path installs
    the shared AGENTCORE_WHEEL_SPEC, which always includes it), so declaring it
    can never fail the compile-time find_spec check.
    """
    requires = _manifest()["requires"]
    assert requires["secrets"] == []
    assert requires["extras"] == ["openai"]
    assert _manifest()["generation_mode"] == "deterministic"


def test_runtime_config_carries_the_declared_parameters():
    runtime = _runtime()
    assert isinstance(runtime["max_retry"], int)
    assert isinstance(runtime["timeout_s"], (int, float))
    # The integration section travels here, not in the manifest.
    assert runtime["billone"]["base_url"] == "https://api.billone.jp/v1"


def test_runtime_config_reaches_the_agent_and_the_inner_graph():
    """A declared value must arrive where it is read - end to end, not by inspection."""
    from src.graph.graph import BillOneInvoiceAgent, load_runtime_config

    runtime = load_runtime_config()
    assert runtime["billone"]["base_url"] == "https://api.billone.jp/v1"

    agent = BillOneInvoiceAgent()
    assert agent.config["max_retry"] == _runtime()["max_retry"]

    agent.register_nodes()
    forwarded = agent._nodes["main"]._parent_config()["configurable"]
    assert forwarded["billone"]["base_url"] == "https://api.billone.jp/v1"
    assert forwarded["timeout_s"] == _runtime()["timeout_s"]
