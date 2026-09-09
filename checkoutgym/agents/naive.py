from .base import ToolRunner, uid

NAME = "naive"


def sel_all(sess: dict, option_id: str) -> list[dict]:
    return [{"type": "shipping", "option_id": option_id, "item_ids": [li["id"] for li in sess["line_items"]]}]


def run(tools: ToolRunner, task: dict) -> None:
    r = tools.call("create_checkout_session", {"line_items": task["line_items"], "idempotency_key": uid()})
    sess = r["body"]
    if r["http"] >= 400:
        tools.call("finish", {"summary": f"could not create session: {sess.get('message')}", "order_id": None})
        return
    if task.get("coupon"):
        r = tools.call("update_checkout_session", {"id": sess["id"], "discounts": {"codes": [task["coupon"]]}, "idempotency_key": uid()})
        sess = r["body"] if r["http"] < 400 else sess
    if sess.get("status") != "ready_for_payment" and sess.get("fulfillment_options"):
        first = sess["fulfillment_options"][0]["id"]
        r = tools.call("update_checkout_session", {"id": sess["id"], "selected_fulfillment_options": sel_all(sess, first), "idempotency_key": uid()})
        sess = r["body"] if r["http"] < 400 else sess
    r = tools.call("complete_checkout", {"id": sess["id"], "payment_token": task["token"], "idempotency_key": uid()})
    if r["http"] >= 400 or (r["body"].get("status") != "completed"):
        # the #402 pattern: retry as a brand new request
        r = tools.call("complete_checkout", {"id": sess["id"], "payment_token": task["token"], "idempotency_key": uid()})
    body = r["body"]
    order = (body.get("order") or {}).get("id") if isinstance(body, dict) else None
    if order:
        tools.call("finish", {"summary": f"placed order {order}", "order_id": order})
    else:
        tools.call("finish", {"summary": f"could not complete checkout (last http {r['http']}, status {body.get('status') if isinstance(body, dict) else None})", "order_id": None})
