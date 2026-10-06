# CMN-C2-275 - Unit tests: BillOneClient service (Bill One REST API shape)
# Pure service layer (stdlib-only, no framework imports) - plain function tests.

import time

import pytest

from src.services.billone_client import (
    LISTING_HARD_CAP,
    BillOneApiError,
    BillOneClient,
)


def test_find_invoice_success_with_injected_get():
    captured = {}

    def get(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, {"invoices": [{"invoice_number": "INV-3041", "issuer_name": "Acme", "status": "received"}]}

    client = BillOneClient("https://billone.example.test/v1/", get=get)
    resp = client.find_invoice("INV-3041", "tok123")
    assert resp["invoices"][0]["invoice_number"] == "INV-3041"
    assert captured["url"] == "https://billone.example.test/v1/invoices"
    # Bill One REST API auth: the per-call token travels as a Bearer header.
    assert captured["headers"]["Authorization"] == "Bearer tok123"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["body"]["invoice_number"] == "INV-3041"


def test_register_invoice_success_with_injected_post():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"task_id": 42, "invoice_number": "B-778"}

    client = BillOneClient("https://billone.example.test/v1", post=post)
    payload = {"invoice_data": [{"invoice_number": "B-778", "issuer_name": "Acme"}]}
    resp = client.register_invoice(payload, "tok")
    assert resp["invoice_number"] == "B-778"
    assert captured["url"] == "https://billone.example.test/v1/invoices"
    assert captured["body"] == payload


def test_list_invoices_success_with_injected_get():
    captured = {}

    def get(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"invoices": [{"invoice_number": "INV-3041", "status": "received"}]}

    client = BillOneClient("https://billone.example.test/v1", get=get)
    params = {"scope": "received"}
    resp = client.list_invoices(params, "tok")
    assert resp["invoices"][0]["status"] == "received"
    assert captured["body"] == {"scope": "received", "_billone_op": "list"}
    # The caller's params dict is copied, never mutated.
    assert params == {"scope": "received"}


def test_non_2xx_raises_billone_api_error():
    def post(url, headers, body):
        return 400, {"errors": ["invoice_data is malformed"]}

    client = BillOneClient("https://billone.example.test/v1", post=post)
    with pytest.raises(BillOneApiError) as exc:
        client.register_invoice({"invoice_data": [{}]}, "tok")
    assert exc.value.status_code == 400
    assert "invoice_data is malformed" in str(exc.value)


def test_default_stub_transport_lookup_shape():
    # No transport injected -> deterministic, network-free v1 stub.
    client = BillOneClient()
    assert client.uses_stub_transport is True
    resp = client.find_invoice("INV-3041", "tok")
    assert resp.get("_stub") is True
    record = resp["invoices"][0]
    assert record["invoice_number"] == "INV-3041"
    assert record["issuer_name"] == "Issuer INV-3041"
    assert record["status"] == "received"


def test_default_stub_transport_register_echoes_invoice_number():
    client = BillOneClient()
    resp = client.register_invoice({"invoice_data": [{"invoice_number": "B-778", "issuer_name": "Acme"}]}, "tok")
    assert resp.get("_stub") is True
    assert resp["invoice_number"] == "B-778"
    assert isinstance(resp["task_id"], int)


def test_default_stub_transport_list_is_deterministic_set():
    client = BillOneClient()
    resp = client.list_invoices({"scope": "all"}, "tok")
    assert resp.get("_stub") is True
    invoices = resp["invoices"]
    assert len(invoices) == 3
    assert [r["status"] for r in invoices] == ["received", "approved", "paid"]


def test_stub_listing_honours_scope_and_limit():
    """A validated caller filter must change the listing, not decorate it."""
    client = BillOneClient()
    resp = client.list_invoices({"scope": "approved", "limit": 5}, "tok")
    invoices = resp["invoices"]
    assert len(invoices) == 5
    assert {r["status"] for r in invoices} == {"approved"}


def test_stub_listing_clamps_a_hostile_limit():
    """The service layer keeps its own ceiling, independent of upstream bounds."""
    client = BillOneClient()
    assert len(client.list_invoices({"scope": "all", "limit": 10**6}, "tok")["invoices"]) == LISTING_HARD_CAP
    assert len(client.list_invoices({"scope": "all", "limit": 0}, "tok")["invoices"]) == 1
    assert len(client.list_invoices({"scope": "all", "limit": "not-a-number"}, "tok")["invoices"]) == 3


def test_request_budget_overrun_is_an_error_not_a_silent_pass():
    """A transport slower than the budget has already broken the contract."""

    def slow(url, headers, body):
        time.sleep(0.02)
        return 200, {"invoices": []}

    client = BillOneClient(timeout_s=0.001, get=slow)
    with pytest.raises(BillOneApiError) as excinfo:
        client.list_invoices({"scope": "all"}, "tok")
    assert excinfo.value.status_code == 504


def test_request_budget_is_not_tripped_by_a_prompt_transport():
    client = BillOneClient(timeout_s=30.0, get=lambda url, headers, body: (200, {"invoices": []}))
    assert client.list_invoices({"scope": "all"}, "tok") == {"invoices": []}


def test_injected_transport_disables_stub_flag():
    client = BillOneClient(get=lambda url, headers, body: (200, {"invoices": []}))
    assert client.uses_stub_transport is False
