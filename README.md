# Bill One Invoice Processing Agent

AI agent for processing invoices in Sansan Bill One, built with Agentic Star.

> **Category**: Cat 2 (domain-specific pipeline agent for the CMN industry)
> **Industry**: Common
> **Template ID**: CMN-C2-275

## Overview

Invoice-operations agent for the Bill One received-invoice service. It takes a request in ordinary language — "look up invoice INV-3041", "register the invoice from Acme", "summarize this month's received invoices" — classifies it into a lookup, a registration or a summary, extracts the invoice number, issuer and any "Key: value" invoice fields from the text, assembles the matching Bill One REST request, calls the service and returns a confirmation naming the record it touched.

Two behaviours are deliberate rather than incidental. An invoice number is only ever taken from an explicit mention or a caller-supplied hint — never guessed — because acting on the wrong invoice is the expensive failure; an unresolved number ends the request in an error instead. And a request the classifier is unsure about falls back to the read-only lookup, so ambiguity can never turn into a write.

Structured caller data (an invoice hint, a listing scope, a record cap, an issuer reference) travels in `input_context` and is validated against explicit bounds before any of it reaches the pipeline.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails during
graph compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and operational documentation
```

See `docs/` for the design specification and the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
