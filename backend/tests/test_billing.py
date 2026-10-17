"""Billed costs stay scoped to this device's API key and do not invent a balance."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from sashmo import billing
from sashmo.state import StateStore


def test_sum_costs_rejects_unknown_currency_and_bad_values():
    page = {"data": [{"results": [
        {"amount": {"value": 0.013, "currency": "usd"}},
        {"amount": {"value": 0.02, "currency": "usd"}},
    ]}]}
    assert billing.sum_costs(page) == Decimal("0.033")
    with pytest.raises(ValueError):
        billing.sum_costs({"data": [{"results": [{"amount": {"value": 2, "currency": "gbp"}}]}]})
    with pytest.raises(ValueError):
        billing.sum_costs({"data": [{"results": [{"amount": {"value": "NaN", "currency": "usd"}}]}]})


def test_fetch_costs_filters_key_and_follows_pages(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.params["api_key_ids[]"] == "key_sashmo"
        assert request.headers["Authorization"] == "Bearer admin-test"
        if "page" not in request.url.params:
            return httpx.Response(200, json={"data": [{"results": [
                {"amount": {"value": 1.25, "currency": "usd"}}]}],
                "has_more": True, "next_page": "next"})
        return httpx.Response(200, json={"data": [{"results": [
            {"amount": {"value": 0.50, "currency": "usd"}}]}], "has_more": False})

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(billing.httpx, "AsyncClient", lambda **kw: original_client(transport=transport, **kw))
    amount, _ = asyncio.run(billing.fetch_month_cost("key_sashmo", "admin-test",
                                                   datetime(2026, 10, 17, tzinfo=timezone.utc)))
    assert amount == Decimal("1.75")
    assert len(seen) == 2


def test_billing_pulse_distinguishes_spend_and_budget():
    store = StateStore()
    store.update_billing(Decimal("1.23"), Decimal("10"),
                         datetime(2026, 10, 17, tzinfo=timezone.utc), None)
    assert store.billing_pulse()["spent_usd"] == "1.23"
    assert store.billing_pulse()["budget_left_usd"] == "8.77"
    store.update_billing(None, Decimal("10"), None, "Unavailable")
    assert store.billing_pulse()["budget_left_usd"] is None
