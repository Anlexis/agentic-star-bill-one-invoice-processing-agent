"""Input screening and caller-field validation for the Bill One Invoice Agent.

Pure, stateless domain helpers (NOT framework gate methods) used by the outer
backbone pre-processing node. Three responsibilities, kept separate on purpose:

1. ``screen_text`` / ``screen_payload`` — REFUSAL. Detect prompt-injection
   attempts and refuse the request. This runs on the caller text *before* and
   *after* sanitization, and depth-first over the structured caller channel
   including its keys.
2. ``sanitize_query`` — REDUCTION. Strip markup and cap length. A sanitizer is
   not a refusal: stripping ``<|im_start|>`` out of an attack turns a
   detectable control token into ordinary-looking text, so the raw form must be
   screened before this ever runs.
3. ``parse_finite`` / ``as_identifier`` / ``as_choice`` — BOUNDS. Every
   caller-supplied number is parsed as finite and in range, and every caller
   string that can reach the response is locked to an inert identifier shape.

All helpers are stdlib-only (no framework imports) so the service layer stays
import-isolated.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

_HTML_TAG_RE = re.compile(r"<[^>]+>")

DEFAULT_MAX_LENGTH = 4000

# ---------------------------------------------------------------------------
# Injection screening
# ---------------------------------------------------------------------------

# Chat-template CONTROL TOKENS. These are a class of their own: they are not
# English phrases, so a phrase-based screen never sees them, and the markup
# strip in sanitize_query() silently deletes the angle-bracket forms — which
# would forward the surviving directive text as innocent prose. Screened on the
# RAW text, before any stripping.
_CONTROL_TOKEN_RE = re.compile(
    r"<\|[^|>]{0,64}\|>"  # <|im_start|>, <|endoftext|>, ...
    r"|\[/?INST\]"  # [INST] / [/INST]
    r"|<</?SYS>>"  # <<SYS>> / <</SYS>>
    r"|<\|?im_(?:start|end)\|?>"  # bare/degraded im_start forms
    r"|\bChatML\b",
    re.IGNORECASE,
)

# Directive phrases. Anchored to a verb + object pair so ordinary invoice prose
# does not trip them: an invoice request legitimately says "Insert Into Trust
# Holdings" or "transact as a settlement agent", and refusing a real payment
# request is the expensive failure direction.
_DIRECTIVE_RES = (
    re.compile(
        r"\bignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier|preceding)?\s*"
        r"(?:instruction|rule|direction|prompt|constraint|guideline)s?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bdisregard\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)?\s*"
        r"(?:instruction|rule|direction|prompt|constraint)s?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:reveal|print|show|output|repeat|dump|leak)\s+(?:me\s+)?(?:your|the)\s+"
        r"(?:system\s+prompt|initial\s+prompt|instructions|api[\s_-]?key|token|secret|credential)s?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\byou\s+are\s+now\s+(?:a|an|the)\b", re.IGNORECASE),
    re.compile(r"\b(?:switch|enter|activate)\s+(?:to\s+)?(?:developer|debug|god|admin|dan)\s+mode\b", re.IGNORECASE),
    re.compile(r"\bpretend\s+(?:that\s+)?you\s+(?:are|have)\b", re.IGNORECASE),
    re.compile(r"\bnew\s+(?:system\s+)?(?:instruction|prompt|rule)s?\s*:", re.IGNORECASE),
    re.compile(r"\boverride\s+(?:your|the|all)\s+(?:safety|security|system)\b", re.IGNORECASE),
)

# Spliced-directive residue. Markup interleaved into a directive ("ig<b>nore all
# rules") reads as harmless to a raw scan and re-assembles into an attack once
# the tags come out, so the sanitized form is screened as well as the raw one.

# The screen reports CATEGORY labels only. A category never carries the matched
# text: echoing a rejected value back into an error log re-introduces the
# payload we just refused.
_CATEGORY_CONTROL_TOKEN = "control_token"
_CATEGORY_DIRECTIVE = "directive"

# Field-name and key screening. Keys are part of the caller's payload and are as
# controllable as values, so they are screened with the same rules.
_MAX_SCAN_DEPTH = 8
_MAX_SCAN_NODES = 2000


def _normalize(text: str) -> str:
    """Fold width/compatibility variants so a full-width payload cannot evade the screen."""
    return unicodedata.normalize("NFKC", text)


def screen_text(text: str) -> list[str]:
    """Return the injection categories found in *text* (empty list = clean).

    Screens the RAW text and the markup-stripped text. The two passes are not
    redundant: the raw pass is the only one that can still see a control token
    (the strip deletes it), and the stripped pass is the only one that can see a
    directive spliced with markup.
    """
    if not isinstance(text, str) or not text:
        return []
    categories: list[str] = []
    raw = _normalize(text)
    stripped = _normalize(_HTML_TAG_RE.sub("", text))

    if _CONTROL_TOKEN_RE.search(raw) or _CONTROL_TOKEN_RE.search(stripped):
        categories.append(_CATEGORY_CONTROL_TOKEN)
    for pattern in _DIRECTIVE_RES:
        if pattern.search(raw) or pattern.search(stripped):
            categories.append(_CATEGORY_DIRECTIVE)
            break
    return categories


def screen_payload(value: Any, _depth: int = 0, _budget: list[int] | None = None) -> list[str]:
    """Depth-first injection screen over a structured caller payload.

    Walks mappings and sequences, screening KEYS as well as values: a hostile
    field name is as reachable as a hostile field value, and JSON ``\\u``
    escapes are already decoded by the time the payload arrives here, so a
    post-parse walk cannot be evaded by re-encoding.

    Traversal is bounded in depth and node count — an unbounded walk over a
    caller-shaped structure is itself a denial-of-service surface.
    """
    if _budget is None:
        _budget = [_MAX_SCAN_NODES]
    if _depth > _MAX_SCAN_DEPTH or _budget[0] <= 0:
        return []
    _budget[0] -= 1

    found: list[str] = []
    if isinstance(value, str):
        found.extend(screen_text(value))
    elif isinstance(value, dict):
        for key, sub in value.items():
            if isinstance(key, str):
                found.extend(screen_text(key))
            found.extend(screen_payload(sub, _depth + 1, _budget))
    elif isinstance(value, (list, tuple, set)):
        for sub in value:
            found.extend(screen_payload(sub, _depth + 1, _budget))
    # Deduplicate while keeping a stable order for assertions and audit payloads.
    return sorted(set(found))


def sanitize_query(query: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Strip markup and cap length.

    REDUCTION only — never treat a successful strip as a refusal. Call
    ``screen_text`` on the raw input first; by the time this returns, the
    evidence a control-token attack ever happened is gone.
    """
    cleaned = _HTML_TAG_RE.sub("", query)
    return cleaned[:max_length]


# ---------------------------------------------------------------------------
# Caller-field bounds
# ---------------------------------------------------------------------------


class FieldError(ValueError):
    """A caller field failed validation.

    Carries the FIELD NAME only. The rejected value is never attached, so an
    error surfaced to a log or a caller cannot re-emit the payload.
    """

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


def parse_finite(value: Any, field: str, *, minimum: float, maximum: float) -> float:
    """Parse a caller number as FINITE and in ``[minimum, maximum]``, or raise.

    Fails CLOSED. The cases that matter and are easy to miss:

    * ``bool`` is an ``int`` in Python — ``True`` would otherwise arrive as 1.
    * ``float("nan")`` and ``float("inf")`` parse without error, and Python's
      ``json`` module accepts bare ``NaN`` / ``Infinity`` in a request body.
      Every comparison against NaN is False, so an unchecked NaN does not raise
      — it silently makes a bound check pass and a threshold never fire.
    """
    if isinstance(value, bool):
        raise FieldError(field, "must be a number, not a boolean")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except (TypeError, ValueError):
            raise FieldError(field, "must be a number") from None
    else:
        raise FieldError(field, "must be a number")

    # `number != number` is the NaN test that does not depend on the math module.
    if number != number or number in (float("inf"), float("-inf")):
        raise FieldError(field, "must be a finite number")
    if not (minimum <= number <= maximum):
        raise FieldError(field, f"must be between {minimum:g} and {maximum:g}")
    return number


def parse_finite_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    """``parse_finite`` for a whole number: finite, in range, and integral."""
    number = parse_finite(value, field, minimum=float(minimum), maximum=float(maximum))
    if number != int(number):
        raise FieldError(field, "must be a whole number")
    return int(number)


# An invoice number is a short alphanumeric identifier; hyphen and underscore are
# the only separators Bill One record numbers use.
_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
# Anything the caller supplies that is RENDERED BACK into the response is locked
# to this inert alphabet. Free text in a rendered field is caller-controlled
# output injection, whatever the downstream formatter does with it.
_INERT_REF_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def as_record_id(value: Any, field: str) -> str:
    """Validate a caller-supplied Bill One record identifier."""
    if not isinstance(value, str):
        raise FieldError(field, "must be a string")
    candidate = value.strip()
    if not candidate:
        return ""
    if not _RECORD_ID_RE.match(candidate):
        raise FieldError(field, "must be 1-32 characters of letters, digits, '-' or '_'")
    return candidate


def as_inert_ref(value: Any, field: str) -> str:
    """Validate a caller string that is rendered back into the response."""
    if not isinstance(value, str):
        raise FieldError(field, "must be a string")
    candidate = value.strip()
    if not candidate:
        return ""
    if not _INERT_REF_RE.match(candidate):
        raise FieldError(field, "must be 1-32 characters of lowercase letters, digits or '_'")
    return candidate


def as_choice(value: Any, field: str, allowed: Iterable[str]) -> str:
    """Validate a caller string against a closed set of values."""
    options = tuple(allowed)
    if not isinstance(value, str):
        raise FieldError(field, "must be a string")
    candidate = value.strip().lower()
    if not candidate:
        return ""
    if candidate not in options:
        raise FieldError(field, f"must be one of {', '.join(options)}")
    return candidate


# ---------------------------------------------------------------------------
# Rendered-text bounds
# ---------------------------------------------------------------------------

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

FIELD_NAME_MAX = 40
FIELD_VALUE_MAX = 200
FIELD_ENTRY_MAX = 20


def clean_rendered_text(value: str, max_length: int) -> str:
    """Bound a text fragment that will be embedded in the outbound record.

    Removes control characters (which can rewrite how a downstream log or
    terminal renders the surrounding text) and caps length. Structural limits
    like this are what keep an unbounded request from becoming an unbounded
    response.
    """
    cleaned = _CONTROL_CHARS_RE.sub("", value)
    return cleaned.strip()[:max_length]
