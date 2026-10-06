"""AgentCore Platform v1.0

Standalone HTTP entry point for the agent. Entry points are adapters only — no
business logic here. For platform-level routing, the gateway calls
agent.invoke() directly.
"""

import json
import os
import secrets
from typing import Any, Dict
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.graph import BillOneInvoiceAgent
from src.graph.graph import load_runtime_config

app = FastAPI(title="Agent")

# The agent is constructed with the runtime config so a value declared in
# config/config.yaml takes effect here exactly as it does on the platform.
agent = BillOneInvoiceAgent(config=load_runtime_config())
agent.compile()
agent.provision_secrets(secrets_factory(namespace="cmn-c2-275", agent_name="BillOneInvoiceAgent"))

# Ceiling on the structured caller channel. The platform adapter caps request
# bodies at roughly this size; the entry point enforces its own bound so a
# direct caller cannot hand the pipeline an unbounded structure.
MAX_INPUT_CONTEXT_BYTES = 256 * 1024


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Structured caller data. The fields the pipeline consumes, and the bounds
    # each one is held to, are declared by the pre-processing node — this
    # adapter only enforces the size ceiling and passes the mapping through.
    input_context: Dict[str, Any] = Field(default_factory=dict)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> "dict[str, Any]":
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone caller auth: when INVOKE_AUTH_TOKEN is set on the server
    # environment, callers that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a Bearer token and run at VERIFIED_EXTERNAL.
    # Middleware-established trust is never demoted. This adapter is the
    # entry-point auth boundary — a deployment-level caller credential, not an
    # agent secret, so ctx.secrets does not apply (no InvocationContext exists
    # before auth).
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL

    input_context = req.input_context or {}
    if _encoded_size(input_context) > MAX_INPUT_CONTEXT_BYTES:
        # Name the limit, never the payload.
        raise HTTPException(
            status_code=413,
            detail=f"input_context exceeds the {MAX_INPUT_CONTEXT_BYTES} byte limit.",
        )

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        result: "dict[str, Any]" = agent.invoke(req.input, ctx=ctx, input_context=input_context)
        return result


def _encoded_size(payload: "dict[str, Any]") -> int:
    """Serialized size of the caller channel, used for the entry-point cap.

    A structure that cannot be serialized at all is treated as over-limit: the
    pipeline could not have carried it anyway, and refusing is the safe
    direction.
    """
    try:
        return len(json.dumps(payload, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return MAX_INPUT_CONTEXT_BYTES + 1


@app.get("/health")
def health() -> "dict[str, str]":
    return {"status": "ok", "agent": "BillOneInvoiceAgent"}
