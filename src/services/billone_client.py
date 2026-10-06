"""AgentCore Platform v1.0 - Bill One REST API client.

Service layer: a thin wrapper around the Bill One (Sansan invoice SaaS) REST
API invoice endpoints. Contains NO business logic, NO routing, and NO
credentials - the integration token is passed in per call by the node (which
reads it via ctx.secrets). This module imports no framework/SDK internals -
pure stdlib (import-isolation, PB-4).

LIMITATION (deliberate, documented):
    The DEFAULT transport is a deterministic, NETWORK-FREE stub. It returns the
    documented Bill One response shapes (an ``invoices`` list for lookups and
    listings; the task/receipt shape with a synthetic ``invoice_number`` echo
    for register, derived from the request) so the pipeline is runnable and
    testable without a live Bill One tenant or the ``requests`` package - it
    does NOT perform a live Bill One call. Never fake a live call; document the
    limitation instead.

    To perform real Bill One calls, inject live transports (requests-based
    ``post`` / ``get``) at construction time; the method contracts and payload
    shapes follow the Bill One invoice API conventions, so no business-logic
    change is needed to go live. A live transport also requires a real
    integration token (see CallBillOneApiNode - the stub runs without one
    because no request ever leaves the process).
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable

# A transport callable: (url, headers, json_body) -> (status_code, response_dict)
Transport = Callable[[str, "dict[str, Any]", "dict[str, Any]"], "tuple[int, dict[str, Any]]"]

_BASE_URL = "https://api.billone.jp/v1"

# Deterministic status cycle used by the network-free list stub (documented
# Bill One invoice processing states).
_STUB_STATUSES = ("received", "approved", "paid")

# Per-call request budget when the runtime config declares none.
DEFAULT_TIMEOUT_S = 30.0
# Ceiling on how many records a single listing call may return, whatever the
# caller asked for. The caller-side cap is validated upstream; this is the
# service layer refusing to be told to do something unbounded.
LISTING_HARD_CAP = 500
_STUB_LISTING_DEFAULT = 3


class BillOneApiError(Exception):
    """Raised when a Bill One REST API call fails or overruns its budget."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Bill One API error {status_code}: {message}")


class BillOneClient:
    """Bill One REST API invoice client.

    Args:
        base_url: Bill One API base URL (default https://api.billone.jp/v1).
        timeout_s: per-call request budget in seconds. A transport that returns
            later than this is reported as a failure rather than accepted
            silently - a backend slow enough to blow the budget has already
            broken the caller's contract, whether or not it eventually answers.
        post/get: optional injected transports (tests or a live client).
            When none is injected, a deterministic NETWORK-FREE stub is used
            (see the module docstring - it returns the documented shape without
            a live Bill One call).
    """

    def __init__(
        self,
        base_url: str = _BASE_URL,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        post: Transport | None = None,
        get: Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self.timeout_s = float(timeout_s)
        self._post = post
        self._get = get

    # -- transport mode --------------------------------------------------------

    @property
    def uses_stub_transport(self) -> bool:
        """True when NO live transport is injected (the network-free v1 default)."""
        return self._post is None and self._get is None

    # -- auth ----------------------------------------------------------------

    def _headers(self, api_token: str) -> "dict[str, str]":
        """Build the Bill One REST API auth headers.

        api_token is supplied per-call by the node (from ctx.secrets); it is
        never persisted on the instance or logged.
        """
        return {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_token,
        }

    # -- v1 deterministic stub transport (default; NO network) ----------------

    def _stub_transport(
        self, url: str, headers: "dict[str, Any]", json_body: "dict[str, Any]"
    ) -> "tuple[int, dict[str, Any]]":
        """Deterministic, network-free v1 stub - returns the documented Bill One shape.

        NOT a live call. Synthetic ids are derived from the request so the
        response is stable and inspectable. See the module docstring for the v1
        limitation and how to inject live transports.
        """
        seed = url + "|" + json.dumps(json_body, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        op = json_body.get("_billone_op")
        if op == "lookup":
            number = str(json_body.get("invoice_number", "")) or f"inv-{digest[:8]}"
            # Documented GET /invoices shape: {"invoices": [...]}
            return 200, {
                "invoices": [
                    {
                        "invoice_number": number,
                        "issuer_name": f"Issuer {number}",
                        "status": "received",
                        "custom_fields": [],
                    }
                ],
                "_stub": True,  # marks the network-free v1 stub response
            }
        if op == "list":
            # Documented GET /invoices listing shape: a deterministic set so
            # summarize is stable and inspectable without a live tenant. The
            # requested scope and record cap are honoured, so a caller-supplied
            # filter produces a genuinely different aggregate rather than the
            # same fixed answer every time.
            limit = _coerce_limit(json_body.get("limit"))
            scope = str(json_body.get("scope", "") or "all").lower()
            invoices: "list[dict[str, Any]]" = []
            index = 0
            while len(invoices) < limit and index < limit * len(_STUB_STATUSES):
                status_label = _STUB_STATUSES[index % len(_STUB_STATUSES)]
                if scope in ("", "all") or status_label == scope:
                    invoices.append(
                        {
                            "invoice_number": f"inv-{digest[:8]}-{index}",
                            "issuer_name": f"Issuer {index}",
                            "status": status_label,
                        }
                    )
                index += 1
            return 200, {"invoices": invoices, "_stub": True}
        # POST /invoices (register) - documented task-receipt shape, plus a
        # synthetic invoice_number echo so the caller can reference the affected
        # record without a follow-up lookup.
        invoice_data = json_body.get("invoice_data") or [{}]
        first = invoice_data[0] if isinstance(invoice_data, list) and invoice_data else {}
        number = str(first.get("invoice_number", "")) or f"inv-{digest[:8]}"
        return 200, {
            "task_id": int(digest[:6], 16),
            "invoice_number": number,
            "_stub": True,  # marks the network-free v1 stub response
        }

    def _resolve(self, injected: Transport | None) -> Transport:
        return injected or self._stub_transport

    def _call(
        self, transport: Transport, url: str, headers: "dict[str, Any]", body: "dict[str, Any]"
    ) -> "tuple[int, dict[str, Any]]":
        """Run a transport call under the configured request budget."""
        started = time.monotonic()
        status, response = transport(url, headers, body)
        elapsed = time.monotonic() - started
        if self.timeout_s > 0 and elapsed > self.timeout_s:
            raise BillOneApiError(504, f"request budget of {self.timeout_s:g}s exceeded ({elapsed:.1f}s)")
        return status, response

    # -- public API ---------------------------------------------------------

    def find_invoice(self, invoice_number: str, api_token: str) -> "dict[str, Any]":
        """GET /invoices - look up a received-invoice record by invoice number.

        The live Bill One endpoint returns the matching invoice records; a live
        ``get`` transport adapter is expected to filter to ``invoice_number``
        (the stub returns the matching record directly). Returns the parsed
        response dict (containing ``invoices``). Raises BillOneApiError on
        non-2xx.
        """
        url = f"{self._base_url}/invoices"
        transport = self._resolve(self._get)
        status, body = self._call(
            transport,
            url,
            self._headers(api_token),
            {"_billone_op": "lookup", "invoice_number": invoice_number},
        )
        if not (200 <= status < 300):
            raise BillOneApiError(status, _err_message(body))
        return body

    def register_invoice(self, payload: "dict[str, Any]", api_token: str) -> "dict[str, Any]":
        """POST /invoices - register a newly received invoice record.

        ``payload`` is the documented ``{"invoice_data": [...]}`` request body.
        Returns the parsed response dict (task receipt). Raises BillOneApiError
        on a non-2xx status.
        """
        url = f"{self._base_url}/invoices"
        transport = self._resolve(self._post)
        status, body = self._call(transport, url, self._headers(api_token), payload)
        if not (200 <= status < 300):
            raise BillOneApiError(status, _err_message(body))
        return body

    def list_invoices(self, params: "dict[str, Any]", api_token: str) -> "dict[str, Any]":
        """GET /invoices (listing) - fetch invoice records for summarization.

        ``params`` is the documented filter dict (e.g.
        ``{"scope": "received", "limit": 10}``). Returns the parsed response
        dict (containing ``invoices``). Raises BillOneApiError on a non-2xx
        status or a call that overruns the request budget.
        """
        url = f"{self._base_url}/invoices"
        transport = self._resolve(self._get)
        body_params = dict(params or {})
        body_params["_billone_op"] = "list"
        status, body = self._call(transport, url, self._headers(api_token), body_params)
        if not (200 <= status < 300):
            raise BillOneApiError(status, _err_message(body))
        return body


def _err_message(body: Any) -> str:
    """Extract a human-readable error message from a Bill One error body."""
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(e) for e in errors)
        msg = body.get("message")
        if msg:
            return str(msg)
    return str(body)


def _coerce_limit(value: Any) -> int:
    """Clamp a listing size to the service hard cap.

    The caller-facing bound is enforced upstream; this is the service layer's
    own floor and ceiling, so a direct caller of the client cannot ask it for an
    unbounded listing either.
    """
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return _STUB_LISTING_DEFAULT
    if limit < 1:
        return 1
    return min(limit, LISTING_HARD_CAP)
