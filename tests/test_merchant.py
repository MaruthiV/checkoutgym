from conftest import complete, create, h, total

HOODIE = [{"id": "item_hoodie", "quantity": 1}]
T2 = [{"id": "item_tee", "quantity": 1}, {"id": "item_socks", "quantity": 1}]
T3 = [{"id": "item_tee", "quantity": 1}, {"id": "item_beanie", "quantity": 1}, {"id": "item_socks", "quantity": 1}]


def test_create_shape_and_preselect(merchant):
    c, _ = merchant("S1")
    r = create(c, HOODIE)
    assert r.status_code == 201 and r.headers["Idempotency-Key"] == "k1"
    s = r.json()
    assert s["status"] == "ready_for_payment" and s["protocol"]["version"] == "2026-04-17"
    assert total(s) == 5399 and s["selected_fulfillment_options"][0]["option_id"] == "ship_std"
    assert not any(k.startswith("_") for k in s["fulfillment_options"][0])


def test_headers_enforced(merchant):
    c, _ = merchant()
    assert create(c, HOODIE, key=None).json()["code"] == "idempotency_key_required"
    assert c.post("/checkout_sessions", json={"line_items": HOODIE}, headers=h("k", version=None)).status_code == 400
    assert c.post("/checkout_sessions", json={"line_items": HOODIE}, headers=h("k", version="2026-01-16")).json()["code"] == "invalid"
    assert c.post("/checkout_sessions", json={"line_items": HOODIE}, headers=h("k", auth=False)).status_code == 401


def test_idempotency_replay_conflict_inflight(merchant):
    c, store = merchant()
    a = create(c, HOODIE, key="same")
    b = create(c, HOODIE, key="same")
    assert b.status_code == 201 and b.json()["id"] == a.json()["id"] and b.headers["Idempotent-Replayed"] == "true"
    assert create(c, T2, key="same").status_code == 422
    store.idem[("/checkout_sessions", "busy")] = {"fp": "x", "in_flight": True, "status": None, "body": None}
    r = create(c, HOODIE, key="busy")
    assert r.status_code == 409 and r.headers["Retry-After"] == "1"


def test_state_machine_s5(merchant):
    c, store = merchant("S5")
    s = create(c, HOODIE).json()
    assert s["status"] == "not_ready_for_payment"
    tok = store.stripe.mint(6000)["id"]
    r = complete(c, s["id"], tok, "c1")
    assert r.status_code == 400 and "not ready" in r.json()["message"]
    s = c.post(f"/checkout_sessions/{s['id']}", headers=h("u1"), json={"selected_fulfillment_options": [{"type": "shipping", "option_id": "ship_std", "item_ids": ["li_1"]}]}).json()
    assert s["status"] == "ready_for_payment"
    r = complete(c, s["id"], tok, "c2")
    assert r.status_code == 200 and r.json()["status"] == "completed" and r.json()["order"]["id"].startswith("ord_")
    assert len(store.orders) == 1 and store.charges[-1]["ok"]
    assert complete(c, s["id"], tok, "c3").status_code == 400
    assert c.post(f"/checkout_sessions/{s['id']}/cancel", headers=h("x1"), json={}).status_code == 400


def test_s6_lost_response_recovery(merchant):
    c, store = merchant("S6")
    s = create(c, T2).json()
    tok = store.stripe.mint(6000)["id"]
    r = complete(c, s["id"], tok, "k")
    assert r.status_code == 504 and r.json()["code"] == "gateway_timeout"
    assert len(store.orders) == 1 and (f"/checkout_sessions/{s['id']}/complete", "k") not in store.idem
    assert c.get(f"/checkout_sessions/{s['id']}", headers=h()).json()["status"] == "ready_for_payment"
    r = complete(c, s["id"], tok, "k")
    assert r.status_code == 200 and r.json()["status"] == "completed" and len(store.orders) == 1
    r = complete(c, s["id"], tok, "k2")
    assert r.status_code == 200 and r.json()["messages"][-1]["code"] == "payment_declined" and len(store.orders) == 1
    assert store.charges[-1]["code"] == "shared_payment_token_deactivated"


def test_s6_double_charge_when_cap_allows(merchant):
    # counterfactual: a reusable token with headroom lets the #402 double charge through
    from checkoutgym.stripe_leg import FakeStripe
    c, store = merchant("S6", stripe=FakeStripe(single_use=False))
    s = create(c, T2).json()
    tok = store.stripe.mint(20000)["id"]
    complete(c, s["id"], tok, "k")
    r = complete(c, s["id"], tok, "k2")
    assert r.status_code == 200 and len(store.orders) == 2 and store.stripe.get_token(tok)["usage_details"]["amount_captured"]["value"] == 2 * 5397


def test_s3_price_change(merchant):
    c, _ = merchant("S3")
    s = create(c, HOODIE).json()
    assert s["status"] == "not_ready_for_payment"
    s = c.post(f"/checkout_sessions/{s['id']}", headers=h("u"), json={"selected_fulfillment_options": [{"type": "shipping", "option_id": "ship_std"}]}).json()
    assert [m["code"] for m in s["messages"]] == ["price_change"] and s["messages"][0]["resolution"] == "recoverable"
    assert total(s) == 6240 + 599


def test_s4_out_of_stock(merchant):
    c, _ = merchant("S4")
    s = create(c, T3).json()
    assert s["status"] == "not_ready_for_payment"
    m = s["messages"][0]
    assert m["code"] == "out_of_stock" and m["resolution"] == "requires_buyer_input" and m["param"] == "$.line_items[2].item.id"
    s = c.post(f"/checkout_sessions/{s['id']}", headers=h("u"), json={"line_items": T3[:2]}).json()
    assert s["status"] == "ready_for_payment" and total(s) == 2050 + 599


def test_s9_consolidated(merchant):
    c, _ = merchant("S9")
    s = create(c, T3).json()
    assert [o["id"] for o in s["fulfillment_options"]] == ["ship_std", "ship_consolidated"]
    a = c.post(f"/checkout_sessions/{s['id']}", headers=h("u1"), json={"selected_fulfillment_options": [{"type": "shipping", "option_id": "ship_std"}]}).json()
    assert total(a) == 5648 + 3 * 599
    b = c.post(f"/checkout_sessions/{s['id']}", headers=h("u2"), json={"selected_fulfillment_options": [{"type": "shipping", "option_id": "ship_consolidated"}]}).json()
    assert total(b) == 5648 + 1999


def test_s10_coupon_invalid_and_valid_elsewhere(merchant):
    c, _ = merchant("S10")
    s = create(c, T2).json()
    s = c.post(f"/checkout_sessions/{s['id']}", headers=h("u"), json={"discounts": {"codes": ["SAVE10"]}}).json()
    assert s["messages"][0]["code"] == "coupon_invalid" and total(s) == 5397
    c, _ = merchant("S1")
    s = create(c, T2).json()
    s = c.post(f"/checkout_sessions/{s['id']}", headers=h("u"), json={"discounts": {"codes": ["SAVE10"]}}).json()
    assert total(s) == 4798 - 479 + 599 and any(t["type"] == "discount" for t in s["totals"])


def test_s11_pending_then_canceled(merchant):
    c, store = merchant("S11")
    s = create(c, HOODIE).json()
    tok = store.stripe.mint(6000)["id"]
    r = complete(c, s["id"], tok, "k")
    assert r.status_code == 200 and r.json()["status"] == "complete_in_progress" and r.json()["order"] is None
    assert not store.orders and not any(ch["ok"] for ch in store.charges)
    g = c.get(f"/checkout_sessions/{s['id']}", headers=h()).json()
    assert g["status"] == "canceled" and g["messages"][-1]["code"] == "payment_declined"


def test_s12_expired_token(merchant):
    c, store = merchant("S12")
    s = create(c, HOODIE).json()
    tok = store.stripe.mint(6000, expires_in=60)["id"]
    s = c.post(f"/checkout_sessions/{s['id']}", headers=h("u"), json={"selected_fulfillment_options": [{"type": "shipping", "option_id": "ship_std"}]}).json()
    r = complete(c, s["id"], tok, "k")
    assert r.status_code == 200 and "expired" in r.json()["messages"][-1]["content"].lower()
    assert store.stripe.get_token(tok)["deactivated_reason"] == "expired"


def test_declined_and_3ds_cards(merchant):
    c, store = merchant("S7")
    s = create(c, HOODIE).json()
    tok = store.stripe.mint(6000, card="pm_card_visa_chargeDeclined")["id"]
    r = complete(c, s["id"], tok, "k")
    assert r.json()["messages"][-1]["code"] == "payment_declined" and r.json()["status"] == "ready_for_payment"
    tok = store.stripe.mint(6000, card="pm_card_threeDSecure2Required")["id"]
    r = complete(c, s["id"], tok, "k2")
    assert r.status_code == 400 and r.json()["code"] == "requires_3ds" and r.json()["param"] == "$.authentication_result"


def test_cancel_discovery_unknown_item(merchant):
    c, _ = merchant()
    s = create(c, HOODIE).json()
    assert c.post(f"/checkout_sessions/{s['id']}/cancel", headers=h("x"), json={}).json()["status"] == "canceled"
    d = c.get("/.well-known/acp.json").json()
    assert d["protocol"]["version"] == "2026-04-17" and "checkout" in d["capabilities"]["services"]
    r = create(c, [{"id": "item_nope", "quantity": 1}], key="bad")
    assert r.status_code == 400 and r.json()["param"] == "$.line_items[0].id"
