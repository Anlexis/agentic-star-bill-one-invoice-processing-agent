"""AgentCore Platform v1.0 - CMN-C2-275 Bill One Invoice Agent state.

State must be a flat TypedDict — never a Pydantic BaseModel. LangGraph
checkpoints use msgpack serialization, and Pydantic objects (and nested
dict/list containers) are not msgpack-safe. Extend AgentState with
agent-specific fields only, and declare every domain field ``NotRequired[...]``
(a field is absent until its producer node writes it).

``billone_payload`` / ``billone_config`` / ``invoice_summary`` /
``redaction_flags`` are dicts or lists at the point of use but are stored in
State as JSON strings via the ``to_json`` / ``from_json`` helpers below.

Never add credentials, secrets, or Pydantic models here. The Bill One
integration token is read via ``ctx.secrets`` inside CallBillOneApiNode and is
never written to State.
"""

from __future__ import annotations

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a list/dict State value to a compact, msgpack-safe JSON string.

    Returns None for None so the field stays a true Optional[str].
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: Any, default: Any) -> Any:
    """Deserialize a JSON-string State value back to its list/dict form.

    Tolerant by design: None/empty -> default; an already-native list/dict (e.g. a value
    supplied directly in a unit test) passes through unchanged; a malformed string -> default.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class State(AgentState):
    """Bill One Invoice agent state.

    Shared fields (user_input, validated_input, intent, result, status,
    formatted_output, session_id, node_history, error_log, correlation_id,
    trace_id, hitl_*, etc.) are inherited from AgentState and NOT re-declared.
    Only Bill One-workflow fields are added below, all NotRequired.

    All values are JSON/msgpack-serializable primitives — the Bill One
    integration token is NEVER stored here (it is read via ``ctx.secrets``).
    """

    # -- validated caller contract (written by pre_process) -------------------
    # Caller-supplied target hint (invoice number / ID fragment from
    # input_context or the request envelope). Never inferred; resolution to a
    # Bill One invoice number is explicit-only.
    invoice_hint: NotRequired[str]
    # Caller-supplied issuer reference, locked to an inert identifier because it
    # is rendered back into the response.
    issuer_ref: NotRequired[str]
    # Listing scope for the summarize intent; one of the closed set declared in
    # pre_process, or "" for the default.
    listing_scope: NotRequired[str]
    # Caller cap on the summarize listing size; parsed finite and in range.
    max_records: NotRequired[int]

    invoice_id: NotRequired[str]  # resolved Bill One invoice number

    # ValidateInput (deterministic scan)
    # JSON list[str] of patterns redacted from the text before logging
    # (stored as a JSON string; (de)serialize via to_json/from_json).
    redaction_flags: NotRequired[Optional[str]]

    # InferBillOneFields
    issuer_name: NotRequired[str]  # invoice issuer (vendor) display name / record label
    # JSON — assembled Bill One REST API request body (stored as a JSON string,
    # not a native dict; (de)serialize via to_json/from_json).
    billone_payload: NotRequired[Optional[str]]

    # Runtime `billone:` settings forwarded by _parent_config() and injected by
    # the inner graph's _extra_initial_state() (JSON string).
    billone_config: NotRequired[Optional[str]]

    # CallBillOneApi
    record_id: NotRequired[str]  # invoice number / record id returned by Bill One
    record_ref: NotRequired[str]  # human-readable reference (billone://invoices/<number>)
    invoice_status: NotRequired[str]  # processing status of a looked-up invoice
    # JSON — record count + per-status tally for the summarize intent (stored as
    # a JSON string; (de)serialize via to_json/from_json).
    invoice_summary: NotRequired[Optional[str]]

    # Confirm
    confirmation: NotRequired[str]  # human-readable confirmation message
