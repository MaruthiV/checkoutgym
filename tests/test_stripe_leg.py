from checkoutgym.stripe_leg import CARD_3DS, CARD_DECLINE, FakeStripe


def test_cumulative_cap_and_expiry():
    s = FakeStripe()
    t = s.mint(1000, expires_in=60)["id"]
    assert s.charge(t, 600)["ok"] and s.charge(t, 300)["ok"]
    assert s.charge(t, 200)["code"] == "amount_exceeds_limit"
    s.sleep(61)
    r = s.charge(t, 50)
    assert r["code"] == "shared_payment_token_expired" and s.get_token(t)["deactivated_reason"] == "expired"
    assert s.charge(t, 50)["code"] == "shared_payment_token_deactivated"
    assert len(s.intents_for(t)) == 2


def test_single_use_mode_and_cards():
    s = FakeStripe(single_use=True)
    t = s.mint(5000)["id"]
    assert s.charge(t, 100)["ok"] and s.charge(t, 100)["code"] == "shared_payment_token_deactivated"
    assert s.charge(s.mint(5000, card=CARD_DECLINE)["id"], 100)["code"] == "card_declined"
    assert s.charge(s.mint(5000, card=CARD_3DS)["id"], 100)["code"] == "requires_action"
    assert s.charge("spt_nope", 1)["code"] == "resource_missing"
