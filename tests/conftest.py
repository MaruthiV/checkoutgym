import pytest
from fastapi.testclient import TestClient

from checkoutgym.merchant.app import Store, app
from checkoutgym.stripe_leg import FakeStripe


@pytest.fixture
def merchant():
    def make(scenario="S1", stripe=None):
        app.state.store = Store(stripe or FakeStripe())
        app.state.store.reset(scenario, "test")
        return TestClient(app), app.state.store
    return make


def h(key=None, version="2026-04-17", auth=True):
    out = {"API-Version": version} if version else {}
    if auth:
        out["Authorization"] = "Bearer t"
    if key:
        out["Idempotency-Key"] = key
    return out


FD = {"name": "T", "email": "t@example.com", "phone_number": "1", "address": {"name": "T", "line_one": "1", "city": "C", "state": "NC", "country": "US", "postal_code": "27514"}}


def create(client, items, key="k1", extra=None):
    body = {"currency": "usd", "line_items": items, "fulfillment_details": FD, **(extra or {})}
    return client.post("/checkout_sessions", json=body, headers=h(key))


def total(sess):
    return next(t["amount"] for t in sess["totals"] if t["type"] == "total")


def complete(client, sid, token, key):
    return client.post(f"/checkout_sessions/{sid}/complete", headers=h(key), json={
        "buyer": {"first_name": "T", "last_name": "B", "email": "t@example.com"},
        "payment_data": {"handler_id": "card_tokenized", "instrument": {"type": "card", "credential": {"type": "spt", "token": token}}}})
