"""AGENTIC STAR Marketplace entrypoint — one-shot Pod process.

Referenced by this repo's Dockerfile as the image `CMD`. Compiles the agent,
provisions its secrets, then hands off to shared.bootstrap.marketplace_app
for the Marketplace lifecycle (identity, input, events, terminal delivery,
exit). Mirrors agentcore's own `agents/base/chat_agent/cli.py` (the pattern
this file was copied from).

`namespace=` here is the Marketplace secret-provisioning namespace — a different
concept from `config/agent.yaml`'s AgentRegistry `namespace:` key that happens to
share its value on most templates. Mirrors src/api/server.py's existing
`secrets_factory(namespace="cmn-c2-275", agent_name="BillOneInvoiceAgent")` call
shape exactly, so both entry points provision secrets under the same path.
"""

from pathlib import Path

from framework.utils.config_loader import load_agent_config
from shared.bootstrap.marketplace_app import run_agent_marketplace
from src.graph.graph import BillOneInvoiceAgent

# Add config overrides here to set values without touching config/config.yaml.
extend_config = {}

if __name__ == "__main__":
    run_agent_marketplace(
        BillOneInvoiceAgent,
        agent_name="BillOneInvoiceAgent",
        namespace="cmn-c2-275",
        config={**load_agent_config(Path(__file__).resolve().parent), **extend_config},
    )
