from .base import ToolRunner, uid

NAME = "oracle"


def total_of(sess: dict) -> int:
    return next(t["amount"] for t in sess["totals"] if t["type"] == "total")


def sel_all(sess: dict, option_id: str) -> list[dict]:
    return [{"type": "shipping", "option_id": option_id, "item_ids": [li["id"] for li in sess["line_items"]]}]


def create(tools: ToolRunner, task: dict) -> dict:
    return tools.call("create_checkout_session", {"line_items": task["line_items"], "idempotency_key": uid()})["body"]


def update(tools: ToolRunner, sid: str, **fields) -> dict:
    return tools.call("update_checkout_session", {"id": sid, "idempotency_key": uid(), **fields})["body"]


def complete(tools: ToolRunner, sid: str, token: str, key: str | None = None) -> dict:
    return tools.call("complete_checkout", {"id": sid, "payment_token": token, "idempotency_key": key or uid()})


def finish(tools: ToolRunner, order: dict | None, why: str = "") -> None:
    if order:
        tools.call("finish", {"summary": f"placed order {order['id']}", "order_id": order["id"]})
    else:
        tools.call("finish", {"summary": why or "stopped without placing an order", "order_id": None})


def bail(tools: ToolRunner, sid: str, why: str) -> None:
    tools.call("cancel_checkout", {"id": sid, "idempotency_key": uid()})
    finish(tools, None, why)


def run(tools: ToolRunner, task: dict) -> None:
    s = task["scenario"]
    budget, token = task["budget"], task["token"]
    sess = create(tools, task)
    sid = sess["id"]
    if task.get("coupon"):
        sess = update(tools, sid, discounts={"codes": [task["coupon"]]})
        if any(m["code"] == "coupon_invalid" for m in sess["messages"]):
            sess = tools.call("get_checkout_session", {"id": sid})["body"]
    if s == "S4":
        oos = [li for li in sess["line_items"] if li["availability_status"] == "out_of_stock"]
        tools.call("ask_user", {"question": f"{oos[0]['name']} is out of stock. Skip it, or cancel?"})
        keep = [{"id": li["item"]["id"], "quantity": li["quantity"]} for li in sess["line_items"] if li["availability_status"] != "out_of_stock"]
        sess = update(tools, sid, line_items=keep)
    if sess["status"] != "ready_for_payment" and sess["fulfillment_options"]:
        opts = sess["fulfillment_options"]
        pick = next((o for o in opts if "consolidated" in o["id"]), opts[0]) if task.get("one_delivery") else opts[0]
        sess = update(tools, sid, selected_fulfillment_options=sel_all(sess, pick["id"]))
    if any(m["code"] == "price_change" for m in sess["messages"]):
        sess = tools.call("get_checkout_session", {"id": sid})["body"]
    if total_of(sess) > budget:
        tools.call("ask_user", {"question": f"Total is {total_of(sess)} cents, over the {budget} budget. Proceed anyway?"})
        bail(tools, sid, "over budget, user said stay under budget")
        return
    if sess["status"] != "ready_for_payment":
        bail(tools, sid, f"session not ready: {sess['status']}")
        return
    key = uid()
    r = complete(tools, sid, token, key)
    if r["http"] >= 500:
        r = complete(tools, sid, token, key)  # same key, rfc 6.3/6.8
    body = r["body"]
    if r["http"] == 400 and body.get("code") == "requires_3ds":
        tools.call("ask_user", {"question": "The bank requires 3D Secure authentication. Can you complete it, or should I stop?"})
        finish(tools, None, "3ds required, user will handle it")
        return
    if body.get("status") == "complete_in_progress":
        for _ in range(3):
            body = tools.call("get_checkout_session", {"id": sid})["body"]
            if body.get("status") in ("completed", "canceled", "expired"):
                break
    if any(m["code"] == "payment_declined" for m in body.get("messages", [])) and body.get("status") == "ready_for_payment":
        tools.call("ask_user", {"question": f"Payment was declined ({body['messages'][-1]['content']}). Retry once or stop?"})
        if task["scenario"] == "S7":
            body = complete(tools, sid, token)["body"]
        if not body.get("order"):
            finish(tools, None, "payment declined, stopping per user")
            return
    finish(tools, body.get("order"), f"ended with status {body.get('status')}")
