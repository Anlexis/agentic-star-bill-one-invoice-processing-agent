"""AgentCore Platform v1.0 - inner workflow Step 3: InferBillOneFields.

Extracts the invoice number, issuer (vendor) name, and "Key: value" invoice
fields from the (redacted) request and assembles a validated Bill One REST API
request body for the classified intent. The invoice number is taken only from
an explicit number in the text or the caller-supplied invoice hint - an
unresolved number is left empty rather than invented, because acting on the
wrong invoice record is the expensive failure; the executor surfaces the miss
as status=error.

Everything that ends up inside the assembled request body is bounded here:
field names and values are stripped of control characters and capped in length,
and the number of custom fields is capped. An unbounded request must not be
able to produce an unbounded record.

The regex/line-structure extraction above is the node's deterministic baseline
- the template runs and tests without a language model. When invoice_number
and/or issuer_name come back empty (not "wrong" - just unresolved by the
regex heuristics), an LLM gap-filler (below) gets one attempt to supply them
from the same text. It only ever FILLS an empty field; it never overrides a
regex-confirmed match, preserving the "never invented" invariant above. Any
failure - missing/unbound secret, LLM API error, a malformed or wrong-shape
response - degrades silently back to the deterministic result. See
``_llm_extract``.

Deliberately NOT in ``requires.secrets`` (see ``config/agent.yaml`` +
``tests/unit/test_config.py``): this is an optional enhancement, and per-namespace
secret provisioning is not fleet-wide guaranteed, so declaring the three Azure
OpenAI keys there would refuse to compile the agent wherever they are absent.
"""

import re

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.services.llm.azure_openai_client import AzureOpenAIClient
from shared.utils.audit_logger import emit_trace_event
from shared.utils.llm_json import extract_json_object

from src.schemas.state import to_json
from src.services.security import (
    FIELD_ENTRY_MAX,
    FIELD_NAME_MAX,
    FIELD_VALUE_MAX,
    clean_rendered_text,
)

# Gap-filler prompt: only the two fields the deterministic parse can miss.
# "or null" on both keys, and told explicitly not to invent, so a text with no
# clear signal comes back {"invoice_number": null, "issuer_name": null} rather
# than a guess this node would then have no way to distinguish from a real one.
_LLM_SYSTEM_PROMPT = """\
You extract two fields from an invoice-related request, when clearly stated:
- invoice_number: a short alphanumeric invoice/document identifier, or null
- issuer_name: the vendor/issuer company name, or null

Respond with a JSON object containing only these two keys. Never invent a
value that is not directly supported by the text - use null instead.
"""

# A Bill One invoice number: short alphanumeric identifier (no spaces).
_NUMBER_SHAPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")
# Explicit invoice-number mention in the request text, EN or JA
# ("invoice number INV-3041" / "invoice no: 4471" / "請求書番号 4471").
_NUMBER_IN_TEXT_RE = re.compile(
    r"(?:invoice|bill|document)\s+(?:number|no\.?|id|code)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})"
    r"|(?:請求書番号|請求書コード|伝票番号)\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})",
    re.IGNORECASE,
)
# Quoted issuer name: from "Foo" / issued by "Foo". Curly quotes as \u escapes so
# the source stays pure ASCII (push-safe).
_ISSUER_QUOTED_RE = re.compile(r'(?:from|issued by|billed by|vendor)\s+["“]([^"”\n]+)["”]', re.IGNORECASE)
# "Key: value" invoice-field lines (ASCII or full-width colon). CJK ranges:
# hiragana/katakana + CJK unified ideographs, as \u escapes (push-safe).
_KV_RE = re.compile(r"^\s*([A-Za-z぀-ヿ一-鿿][\w \-぀-ヿ一-鿿]{0,40})[:：]\s*(.+?)\s*$")
# Keys that are the number/issuer themselves, not custom invoice fields.
_NUMBER_KEYS = ("invoice number", "invoice no", "invoice code", "number", "code", "id")
_ISSUER_KEYS = ("issuer", "issuer name", "vendor", "supplier", "company", "from")

# Listing scope used when the caller did not name one.
_DEFAULT_SCOPE = "received"
# Cap on the rendered issuer label.
_ISSUER_MAX = 100


class InferBillOneFieldsNode(FunctionNode):
    """Extract entities and assemble the Bill One REST API request body."""

    # Inner domain node - derives fields from already-validated text; the
    # external trust gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def __init__(self, llm: Any = None) -> None:
        # ``llm`` is a unit-test seam only (see tests/unit/test_infer_billone_fields_node.py's
        # FakeLLM) - production construction (domain_workflow_graph.py) always passes none, since
        # a real caller-independent client built here would be cached on this node instance and
        # shared across every invocation the registry's node cache serves.
        super().__init__()
        self._llm = llm

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        text = state.get("validated_input", "") or ""
        intent = state.get("intent", "lookup_invoice") or "lookup_invoice"
        invoice_hint = state.get("invoice_hint", "") or ""

        if not text.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InferBillOneFieldsNode: missing validated_input"],
            }

        fields = self._parse_fields(text)
        invoice_id = self._resolve_number(text, invoice_hint, fields)
        issuer_name = self._resolve_issuer(text, fields)

        # LLM gap-fill: only when the deterministic parse left something empty,
        # and only for intents that use invoice_id/issuer_name at all
        # (summarize_invoices's payload uses neither - nothing to fill).
        llm_filled = False
        if intent != "summarize_invoices" and (not invoice_id or not issuer_name):
            llm_fields = self._llm_extract(text, state)
            if llm_fields:
                if not invoice_id and llm_fields.get("invoice_id"):
                    invoice_id = llm_fields["invoice_id"]
                    llm_filled = True
                if not issuer_name and llm_fields.get("issuer_name"):
                    issuer_name = llm_fields["issuer_name"]
                    llm_filled = True

        if intent == "register_invoice":
            payload = self._build_invoice_data(invoice_id, issuer_name, fields)
        elif intent == "summarize_invoices":
            # The caller-validated listing scope and cap travel with the request
            # so the listing the pipeline aggregates is the one that was asked
            # for, not a fixed default.
            payload = {"scope": state.get("listing_scope") or _DEFAULT_SCOPE}
            max_records = state.get("max_records")
            if isinstance(max_records, int) and not isinstance(max_records, bool):
                payload["limit"] = max_records
        else:  # lookup_invoice (read-only default)
            payload = {"invoice_number": invoice_id}

        # Audit the assembled payload shape - field signals only, not content.
        emit_trace_event(
            "infer_billone_fields_complete",
            {"intent": intent, "has_invoice_id": bool(invoice_id), "n_fields": len(fields), "llm_filled": llm_filled},
            state,
        )

        return {
            "invoice_id": invoice_id,
            "issuer_name": issuer_name,
            "billone_payload": to_json(payload),
            "status": AgentStatus.SUCCESS.value,
        }

    # -- LLM gap-fill -----------------------------------------------------------

    def _llm_extract(self, text: str, state: "dict[str, Any]") -> "dict[str, str] | None":
        """Best-effort LLM fill for whichever of invoice_number/issuer_name is empty.

        Broad by design: a missing/unbound secret (``InvocationContext.from_state``
        indexes lifecycle fields a bare/test state may not carry, or the secret
        itself is unprovisioned), an LLM API failure, or a malformed/wrong-shape
        response must all degrade the same way - ``None``, so the caller keeps the
        deterministic result unchanged. Never raises.
        """
        try:
            llm = self._llm
            if llm is None:
                ctx = InvocationContext.from_state(state)
                llm = AzureOpenAIClient(
                    {
                        "api_key": ctx.secrets.require("AZURE_OPENAI_API_KEY"),
                        "azure_endpoint": ctx.secrets.require("AZURE_OPENAI_ENDPOINT"),
                        "azure_deployment": ctx.secrets.require("AZURE_OPENAI_DEPLOYMENT"),
                    }
                )
            response = llm.complete(
                [
                    {"role": "system", "content": _LLM_SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ]
            )
            parsed = extract_json_object(response.get("content", ""))
        except Exception:
            return None

        result: "dict[str, str]" = {}
        number = parsed.get("invoice_number")
        if isinstance(number, str) and _NUMBER_SHAPE_RE.match(number.strip()):
            result["invoice_id"] = number.strip()
        issuer = parsed.get("issuer_name")
        if isinstance(issuer, str) and issuer.strip():
            result["issuer_name"] = clean_rendered_text(issuer, _ISSUER_MAX)
        return result or None

    # -- extraction -----------------------------------------------------------

    def _resolve_number(self, text: str, invoice_hint: str, fields: "list[tuple[str, str]]") -> str:
        """Explicit number only: text mention > number-shaped hint > 'Number:' field. Never invented."""
        m = _NUMBER_IN_TEXT_RE.search(text)
        if m:
            return m.group(1) or m.group(2) or ""
        hint = invoice_hint.strip()
        if hint and _NUMBER_SHAPE_RE.match(hint):
            return hint
        for key, value in fields:
            if key.strip().lower() in _NUMBER_KEYS and _NUMBER_SHAPE_RE.match(value.strip()):
                return value.strip()
        return ""  # unresolved - left empty, never invented

    def _resolve_issuer(self, text: str, fields: "list[tuple[str, str]]") -> str:
        m = _ISSUER_QUOTED_RE.search(text)
        if m:
            return clean_rendered_text(m.group(1), _ISSUER_MAX)
        for key, value in fields:
            if key.strip().lower() in _ISSUER_KEYS:
                return clean_rendered_text(value, _ISSUER_MAX)
        return ""

    def _parse_fields(self, text: str) -> "list[tuple[str, str]]":
        """Return the [(key, value), ...] invoice fields parsed from the request lines."""
        fields: list[tuple[str, str]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = _KV_RE.match(stripped)
            if m:
                fields.append((m.group(1).strip(), m.group(2).strip()))
        return fields

    # -- payload assembly (Bill One invoice_data shape) -------------------------

    def _build_invoice_data(
        self, invoice_id: str, issuer_name: str, fields: "list[tuple[str, str]]"
    ) -> "dict[str, Any]":
        record: "dict[str, Any]" = {"invoice_number": invoice_id}
        if issuer_name:
            record["issuer_name"] = issuer_name
        custom_fields: "list[dict[str, Any]]" = []
        for key, value in fields:
            if key.strip().lower() in _NUMBER_KEYS + _ISSUER_KEYS:
                continue
            if len(custom_fields) >= FIELD_ENTRY_MAX:
                break
            name = clean_rendered_text(key, FIELD_NAME_MAX)
            cleaned = clean_rendered_text(value, FIELD_VALUE_MAX)
            if not name:
                continue
            custom_fields.append({"name": name, "values": [cleaned]})
        if custom_fields:
            record["custom_fields"] = custom_fields
        return {"invoice_data": [record]}
