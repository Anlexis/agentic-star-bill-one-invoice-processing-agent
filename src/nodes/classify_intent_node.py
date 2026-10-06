"""AgentCore Platform v1.0 - inner workflow Step 2: ClassifyIntent.

Classifies the (redacted) request into one of lookup_invoice / register_invoice
/ summarize_invoices using a deterministic keyword heuristic, so the template
is testable and runnable without a language model. Low-confidence or unknown
input falls back to the read-only "lookup_invoice" default with a note -
never a write.
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_VALID_INTENTS = ("lookup_invoice", "register_invoice", "summarize_invoices")

# Deterministic keyword signals (checked in priority order, the write first so
# a "register the invoice then show it" style request classifies as the write;
# summarize before lookup so an aggregate request never degrades to a single
# lookup).
_KEYWORDS = (
    (
        "register_invoice",
        (
            "register",
            "submit",
            "upload",
            "add an invoice",
            "add a new",
            "new invoice",
            "received a new",
            "book the invoice",
            "登録",
            "追加",
            "新規",
            "計上",
        ),
    ),
    (
        "summarize_invoices",
        (
            "summarize",
            "summary",
            "aggregate",
            "how many",
            "total amount",
            "overview",
            "breakdown",
            "tally",
            "集計",
            "サマリ",
            "概要",
            "一覧",
            "まとめ",
        ),
    ),
    (
        "lookup_invoice",
        (
            "look up",
            "lookup",
            "find",
            "show",
            "get",
            "fetch",
            "retrieve",
            "search",
            "status",
            "check",
            "what is",
            "照会",
            "検索",
            "参照",
            "確認",
            "ステータス",
        ),
    ),
)


class ClassifyIntentNode(FunctionNode):
    """Classify the request into a Bill One invoice operation intent."""

    # Inner domain node, read-only classification of already-redacted text -
    # the external trust gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        text = state.get("validated_input", "") or ""
        if not text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClassifyIntentNode: missing validated_input"],
            }

        intent = self._classify_via_keywords(text)

        note: list[str] = []
        if intent not in _VALID_INTENTS:
            note = ["ClassifyIntentNode: low-confidence classification, " "defaulted to lookup_invoice (read-only)"]
            intent = "lookup_invoice"

        # Audit the classification decision - intent label only, never the text.
        emit_trace_event(
            "classify_intent_complete",
            {"intent": intent, "defaulted": bool(note)},
            state,
        )

        result: "dict[str, Any]" = {"intent": intent, "status": AgentStatus.SUCCESS.value}
        if note:
            result["error_log"] = note  # non-fatal note; status stays SUCCESS
        return result

    # -- classification -------------------------------------------------------

    def _classify_via_keywords(self, text: str) -> str:
        low = text.lower()
        for intent, words in _KEYWORDS:
            if any(w in low for w in words):
                return intent
        # No signal at all: fall through to the read-only default via the
        # _VALID_INTENTS guard in execute() (returns a sentinel outside the set).
        return "unknown"
