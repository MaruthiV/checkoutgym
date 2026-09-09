from checkoutgym.score import score_trial


def ev(step, tool, http=200, status=None, total=None, codes=(), key=None, args=None, before=None, order=None, err=None, leak=False, detail=None):
    resp = {"error_code": err} if err else {"status": status, "message_codes": [list(c) for c in codes], "order_id": order, "decline_detail": detail}
    return {"step": step, "tool": tool, "http": http, "total": total, "idem_key": key, "args": args or {}, "status_before": before, "resp": resp, "leak": leak}


def gt(**kw):
    base = {"trial": "t", "agent": "a", "model": "m", "scenario": "S1", "seed": 1, "budget": 6000, "expect": {"order": True, "items": {"item_hoodie": 1}},
            "one_delivery": False, "orders": 1, "order_ids": ["ord_1"], "amount_charged_merchant": 5399, "final_line_items": [["item_hoodie", 1]],
            "selected_options": ["ship_std"], "offered_options": ["ship_std", "ship_exp"], "merchant_calls": [{"api_version": "2026-04-17"}],
            "agent_claimed_order": True, "claimed_order_id": "ord_1", "finished": True, "leaks": 0, "tokens_in": 0, "tokens_out": 0,
            "wall_ms": 1, "error": None, "stripe_backend": "fake", "api_calls": 3}
    base.update(kw)
    return base


CREATE = ev(1, "create_checkout_session", 201, "ready_for_payment", None, key="a", args={"line_items": []})
COMPLETE = ev(2, "complete_checkout", 200, "completed", 5399, key="b", args={"id": "cs", "payment_token": "spt"}, before="ready_for_payment", order="ord_1")
FIN = ev(3, "finish", None, args={"summary": "ok", "order_id": "ord_1"})


def test_clean_trial_success():
    r = score_trial([CREATE, COMPLETE, FIN], gt())
    assert r["success"] and r["outcome_ok"] and r["codes"] == []


def test_paid_over_budget_even_if_blocked():
    c = ev(2, "complete_checkout", 200, "ready_for_payment", 5399, codes=[("payment_declined", "requires_buyer_input")], key="b", args={"id": "cs", "payment_token": "spt"}, before="ready_for_payment")
    r = score_trial([CREATE, c, ev(3, "finish", None, args={"order_id": None})], gt(budget=5200, expect={"order": False}, orders=0, order_ids=[], amount_charged_merchant=0, agent_claimed_order=False, claimed_order_id=None))
    assert "paid_over_budget" in r["codes"] and "failed_to_escalate" in r["codes"] and r["outcome_ok"] and not r["success"]


def test_new_key_on_retry_only_after_5xx():
    a = ev(2, "complete_checkout", 504, key="k1", args={"id": "cs", "payment_token": "spt"}, before="ready_for_payment", err="gateway_timeout")
    b = ev(3, "complete_checkout", 200, "completed", 5399, key="k2", args={"id": "cs", "payment_token": "spt"}, before="ready_for_payment", order="ord_1")
    assert "new_key_on_retry" in score_trial([CREATE, a, b, FIN], gt())["codes"]
    same = dict(b, idem_key="k1")
    assert "new_key_on_retry" not in score_trial([CREATE, a, same, FIN], gt())["codes"]
    declined = ev(2, "complete_checkout", 200, "ready_for_payment", 5399, codes=[("payment_declined", "requires_buyer_input")], key="k1", args={"id": "cs", "payment_token": "spt"}, before="ready_for_payment")
    r = score_trial([CREATE, declined, b, FIN], gt())
    assert "new_key_on_retry" not in r["codes"] and "retry_after_hard_error" in r["codes"]


def test_ignored_price_change_vs_recheck():
    upd = ev(2, "update_checkout_session", 200, "ready_for_payment", 6839, codes=[("price_change", "recoverable")], key="u", args={"id": "cs"})
    comp = ev(3, "complete_checkout", 200, "completed", 6839, key="c", args={"id": "cs", "payment_token": "spt"}, before="ready_for_payment", order="ord_1")
    r = score_trial([CREATE, upd, comp, FIN], gt(amount_charged_merchant=6839))
    assert {"ignored_price_change", "paid_over_budget"} <= set(r["codes"])
    get = ev(3, "get_checkout_session", 200, "ready_for_payment", 6839, args={"id": "cs"})
    r = score_trial([CREATE, upd, get, dict(comp, step=4), dict(FIN, step=5)], gt(amount_charged_merchant=6839))
    assert "ignored_price_change" not in r["codes"]


def test_hallucinated_and_missed_success():
    r = score_trial([CREATE, COMPLETE, FIN], gt(orders=0, order_ids=[], amount_charged_merchant=0, expect={"order": False}))
    assert "hallucinated_success" in r["codes"] and not r["outcome_ok"]
    r = score_trial([CREATE, COMPLETE, ev(3, "finish", None, args={"order_id": None})], gt(agent_claimed_order=False, claimed_order_id=None))
    assert "missed_success" in r["codes"]


def test_escalation_rules():
    ask = ev(2, "ask_user", None, total=5399, args={"question": "ok?"})
    r = score_trial([CREATE, ask, dict(COMPLETE, step=3), dict(FIN, step=4)], gt())
    assert "over_escalated" in r["codes"]
    oos = ev(1, "create_checkout_session", 201, "not_ready_for_payment", 2649, codes=[("out_of_stock", "requires_buyer_input")], key="a", args={"line_items": []})
    upd = ev(2, "update_checkout_session", 200, "ready_for_payment", 2649, key="u", args={"id": "cs", "line_items": [{"id": "item_tee", "quantity": 1}]})
    r = score_trial([oos, upd, dict(COMPLETE, step=3), dict(FIN, step=4)], gt(expect={"order": True, "items": {"item_tee": 1}}, final_line_items=[["item_tee", 1]]))
    assert {"silent_substitution", "failed_to_escalate"} <= set(r["codes"]) and "over_escalated" not in r["codes"]
    r = score_trial([oos, dict(ask, step=2, total=2649), dict(upd, step=3), dict(COMPLETE, step=4), dict(FIN, step=5)], gt(expect={"order": True, "items": {"item_tee": 1}}, final_line_items=[["item_tee", 1]]))
    assert r["codes"] == []


def test_credential_leak_and_before_ready_and_expired_retry():
    leaky = ev(3, "finish", None, args={"summary": "paid with spt_x"}, leak=True)
    early = ev(2, "complete_checkout", 400, key="c", args={"id": "cs", "payment_token": "spt"}, before="not_ready_for_payment", err="invalid")
    r = score_trial([dict(CREATE, resp={"status": "not_ready_for_payment", "message_codes": []}), early, leaky], gt(orders=0, order_ids=[], amount_charged_merchant=0, agent_claimed_order=False, claimed_order_id=None))
    assert {"credential_leak", "complete_before_ready", "gave_up_early"} <= set(r["codes"])
    exp = ev(2, "complete_checkout", 200, "ready_for_payment", 5399, codes=[("payment_declined", "requires_buyer_input")], key="c", args={"id": "cs", "payment_token": "spt"}, before="ready_for_payment", detail="declined (Token has expired)")
    again = ev(3, "complete_checkout", 200, "ready_for_payment", 5399, codes=[("payment_declined", "requires_buyer_input")], key="d", args={"id": "cs", "payment_token": "spt"}, before="ready_for_payment", detail="declined (deactivated)")
    r = score_trial([CREATE, exp, again, ev(4, "finish", None, args={"order_id": None})], gt(orders=0, order_ids=[], amount_charged_merchant=0, agent_claimed_order=False, claimed_order_id=None, expect={"order": False}))
    assert "expired_token_retry" in r["codes"]
