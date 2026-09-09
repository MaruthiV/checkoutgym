import hashlib
import json
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..stripe_leg import FakeStripe
from .catalog import COUPONS, ITEMS, SHIPPING, SHIPPING_S9
from .scenarios import (
    ERR_IDEM_CONFLICT,
    ERR_IDEM_IN_FLIGHT,
    ERR_IDEM_REQUIRED,
    ERR_REQUIRES_3DS,
    ERR_TIMEOUT,
    MSG_COUPON_INVALID,
    MSG_OUT_OF_STOCK,
    MSG_PAYMENT_DECLINED,
    MSG_PRICE_CHANGE,
    MSG_REGION_RESTRICTED,
    SCENARIOS,
)

API_VERSION = "2026-04-17"
TERMINAL = {"completed", "canceled", "expired"}
HANDLER = {"id": "card_tokenized", "name": "dev.acp.tokenized.card", "display_name": "Credit Card", "version": "2026-01-22",
           "spec": "https://acp.dev/handlers/tokenized.card", "requires_delegate_payment": True, "requires_pci_compliance": False,
           "psp": "stripe", "config": {"psp": "stripe", "accepted_brands": ["visa", "mastercard"], "supports_3ds": True, "environment": "test"}}
LINKS = [{"type": "terms_of_use", "url": "https://merchant.test/legal/terms"}]


class Store:
    def __init__(self, stripe=None):
        self.stripe = stripe or FakeStripe()
        self.reset("S1", "adhoc")

    def reset(self, scenario: str, trial: str) -> None:
        self.scenario = scenario
        self.knobs = SCENARIOS[scenario]
        self.trial = trial
        self.sessions: dict[str, dict] = {}
        self.orders: list[dict] = []
        self.calls: list[dict] = []
        self.charges: list[dict] = []
        self.idem: dict[tuple, dict] = {}
        self.recovery: dict[str, dict] = {}
        self.updates_seen = 0
        self.lost_once = False


app = FastAPI(title="checkoutgym mock merchant")
app.state.store = Store()


def store_of(request: Request) -> Store:
    return request.app.state.store


def err(status: int, body: dict, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=headers or {})


def public(obj):
    if isinstance(obj, dict):
        return {k: public(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [public(v) for v in obj]
    return obj


def guard(request: Request, store: Store, body: dict | None) -> JSONResponse | None:
    store.calls.append({
        "ts": time.time(), "method": request.method, "path": request.url.path,
        "api_version": request.headers.get("api-version"), "idempotency_key": request.headers.get("idempotency-key"),
        "authorized": bool(request.headers.get("authorization")),
    })
    if not request.headers.get("authorization"):
        return err(401, {"type": "invalid_request", "code": "missing", "message": "Authorization header is required"})
    ver = request.headers.get("api-version")
    if ver is None:
        return err(400, {"type": "invalid_request", "code": "missing", "message": f"API-Version header is required (expected {API_VERSION})"})
    if ver != API_VERSION:
        return err(400, {"type": "invalid_request", "code": "invalid", "message": f"Unsupported API-Version {ver!r}; expected {API_VERSION}"})
    return None


def idem_begin(store: Store, key: str | None, path: str, body: dict) -> tuple[JSONResponse | None, str]:
    if not key:
        return err(400, ERR_IDEM_REQUIRED), ""
    fp = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    rec = store.idem.get((path, key))
    if rec is not None:
        if rec["in_flight"]:
            return err(409, ERR_IDEM_IN_FLIGHT, {"Retry-After": "1"}), fp
        if rec["fp"] != fp:
            return err(422, ERR_IDEM_CONFLICT), fp
        return JSONResponse(rec["body"], status_code=rec["status"], headers={"Idempotent-Replayed": "true", "Idempotency-Key": key}), fp
    store.idem[(path, key)] = {"fp": fp, "in_flight": True, "status": None, "body": None}
    return None, fp


def idem_finish(store: Store, key: str, path: str, resp: JSONResponse) -> JSONResponse:
    if resp.status_code >= 500:
        store.idem.pop((path, key), None)  # rfc 6.5, never cache a 5xx
    else:
        store.idem[(path, key)].update(in_flight=False, status=resp.status_code, body=json.loads(resp.body))
    resp.headers["Idempotency-Key"] = key
    return resp


def money(t: str, amount: int, text: str) -> dict:
    return {"type": t, "display_text": text, "amount": amount}


def build_line_items(store: Store, items: list[dict], country: str) -> tuple[list[dict], JSONResponse | None]:
    out = []
    for i, li in enumerate(items or []):
        iid, qty = li.get("id"), int(li.get("quantity", 1))
        cat = ITEMS.get(iid)
        if cat is None:
            return [], err(400, {"type": "invalid_request", "code": "invalid", "message": f"Unknown item {iid!r}", "param": f"$.line_items[{i}].id"})
        if qty < 1:
            return [], err(400, {"type": "invalid_request", "code": "invalid", "message": "quantity must be >= 1", "param": f"$.line_items[{i}].quantity"})
        avail = "out_of_stock" if store.knobs.get("out_of_stock") == iid else "in_stock"
        out.append({"id": f"li_{i + 1}", "item": {"id": iid}, "quantity": qty, "name": cat["name"], "unit_amount": cat["unit_amount"],
                    "availability_status": avail, "_restricted": bool(cat.get("region_restricted")) and country != "US", "totals": []})
    return out, None


def options_for(store: Store, sess: dict) -> list[dict]:
    src = SHIPPING_S9 if store.knobs.get("consolidated_option") else SHIPPING
    opts = []
    for o in src:
        opts.append({"type": "shipping", "id": o["id"], "title": o["title"], "description": o["description"], "carrier": o["carrier"],
                     "earliest_delivery_time": "2026-09-15T00:00:00Z", "latest_delivery_time": "2026-09-17T00:00:00Z",
                     "totals": [money("total", o["amount"], "Shipping")], "_amount": o["amount"], "_per_item": bool(o.get("per_item"))})
    return opts


def recompute(store: Store, sess: dict) -> None:
    base = 0
    for li in sess["line_items"]:
        line = li["unit_amount"] * li["quantity"]
        base += line
        li["totals"] = [money("items_base_amount", line, "Base Amount"), money("subtotal", line, "Subtotal"), money("total", line, "Total")]
    discount = 0
    code = sess.get("_coupon")
    if code and code in COUPONS and not store.knobs.get("coupon_invalid"):
        discount = base * COUPONS[code]["percent"] // 100
    subtotal = base - discount
    ship = 0
    by_id = {o["id"]: o for o in sess["fulfillment_options"]}
    for sel in sess["selected_fulfillment_options"]:
        o = by_id.get(sel["option_id"])
        if o:
            ship += o["_amount"] * (len(sel["item_ids"]) if o["_per_item"] else 1)
    total = subtotal + ship
    totals = [money("items_base_amount", base, "Item(s) total")]
    if discount:
        totals.append(money("discount", -discount, "Discount"))
    totals += [money("subtotal", subtotal, "Subtotal"), money("tax", 0, "Tax"), money("fulfillment", ship, "Fulfillment"), money("total", total, "Total")]
    sess["totals"] = totals
    msgs = []
    for li in sess["line_items"]:
        if li["availability_status"] == "out_of_stock":
            m = dict(MSG_OUT_OF_STOCK)
            m["content"] = m["content"].format(name=li["name"])
            m["param"] = f"$.line_items[{sess['line_items'].index(li)}].item.id"
            msgs.append(m)
        if li.get("_restricted"):
            m = dict(MSG_REGION_RESTRICTED)
            m["content"] = m["content"].format(name=li["name"])
            msgs.append(m)
    if code and (code not in COUPONS or store.knobs.get("coupon_invalid")):
        m = dict(MSG_COUPON_INVALID)
        m["content"] = m["content"].format(code=code)
        msgs.append(m)
    sess["messages"] = msgs + sess.get("_extra_msgs", [])
    if sess["status"] in TERMINAL or sess["status"] == "complete_in_progress":
        return
    blocked = any(li["availability_status"] == "out_of_stock" or li.get("_restricted") for li in sess["line_items"])
    covered = {i for sel in sess["selected_fulfillment_options"] for i in sel["item_ids"]}
    all_items = {li["id"] for li in sess["line_items"]}
    sess["status"] = "ready_for_payment" if (not blocked and all_items and all_items <= covered) else "not_ready_for_payment"


def apply_selection(sess: dict, selections: list[dict]) -> JSONResponse | None:
    by_id = {o["id"] for o in sess["fulfillment_options"]}
    li_ids = {li["id"] for li in sess["line_items"]}
    item_to_li = {li["item"]["id"]: li["id"] for li in sess["line_items"]}
    out = []
    for i, sel in enumerate(selections or []):
        oid = sel.get("option_id")
        if oid not in by_id:
            return err(400, {"type": "invalid_request", "code": "invalid", "message": f"Unknown fulfillment option {oid!r}", "param": f"$.selected_fulfillment_options[{i}].option_id"})
        ids = sel.get("item_ids") or list(li_ids)
        ids = [item_to_li.get(x, x) for x in ids]
        bad = [x for x in ids if x not in li_ids]
        if bad:
            return err(400, {"type": "invalid_request", "code": "invalid", "message": f"Unknown item ids {bad}", "param": f"$.selected_fulfillment_options[{i}].item_ids"})
        out.append({"type": sel.get("type", "shipping"), "option_id": oid, "item_ids": ids})
    sess["selected_fulfillment_options"] = out
    return None


def flip_if_pending(sess: dict) -> None:
    if sess.get("_pending_cancel") and sess["status"] == "complete_in_progress":
        sess["status"] = "canceled"
        sess["_pending_cancel"] = False
        sess.setdefault("_extra_msgs", []).append({"type": "error", "code": "payment_declined", "content_type": "plain",
                                                   "content": "The order was rejected by a post-authorization fraud review and has been canceled. No charge was made."})
        sess["messages"] = sess["messages"] + sess["_extra_msgs"][-1:]


@app.get("/.well-known/acp.json")
def discovery(request: Request):
    return {"protocol": {"name": "acp", "version": API_VERSION, "supported_versions": [API_VERSION]},
            "api_base_url": str(request.base_url).rstrip("/"), "transports": ["rest"], "capabilities": {"services": ["checkout"]}}


@app.post("/checkout_sessions")
async def create_session(request: Request):
    store = store_of(request)
    body = await request.json()
    if (bad := guard(request, store, body)):
        return bad
    key = request.headers.get("idempotency-key")
    replay, _ = idem_begin(store, key, request.url.path, body)
    if replay is not None:
        return replay
    fd = body.get("fulfillment_details") or {}
    country = ((fd.get("address") or {}).get("country")) or "US"
    items, bad = build_line_items(store, body.get("line_items"), country)
    if bad is None and not items:
        bad = err(400, {"type": "invalid_request", "code": "missing", "message": "line_items is required", "param": "$.line_items"})
    if bad is not None:
        return idem_finish(store, key, request.url.path, bad)
    sid = "cs_" + uuid.uuid4().hex[:12]
    sess = {"id": sid, "protocol": {"version": API_VERSION}, "capabilities": {"payment": {"handlers": [HANDLER]}},
            "status": "not_ready_for_payment", "currency": "usd", "line_items": items, "fulfillment_details": fd,
            "fulfillment_options": [], "selected_fulfillment_options": [], "totals": [], "messages": [], "links": LINKS, "order": None,
            "_coupon": None, "_extra_msgs": []}
    sess["fulfillment_options"] = options_for(store, sess)
    if store.knobs.get("preselect", True):
        first = sess["fulfillment_options"][0]["id"]
        sess["selected_fulfillment_options"] = [{"type": "shipping", "option_id": first, "item_ids": [li["id"] for li in items]}]
    codes = ((body.get("discounts") or {}).get("codes")) or []
    if codes:
        sess["_coupon"] = codes[0]
    recompute(store, sess)
    store.sessions[sid] = sess
    return idem_finish(store, key, request.url.path, JSONResponse(public(sess), status_code=201))


@app.get("/checkout_sessions/{sid}")
def get_session(sid: str, request: Request):
    store = store_of(request)
    if (bad := guard(request, store, None)):
        return bad
    sess = store.sessions.get(sid)
    if sess is None:
        return err(404, {"type": "invalid_request", "code": "invalid", "message": f"No checkout session {sid!r}"})
    flip_if_pending(sess)
    return public(sess)


@app.post("/checkout_sessions/{sid}")
async def update_session(sid: str, request: Request):
    store = store_of(request)
    body = await request.json()
    if (bad := guard(request, store, body)):
        return bad
    key = request.headers.get("idempotency-key")
    replay, _ = idem_begin(store, key, request.url.path, body)
    if replay is not None:
        return replay
    sess = store.sessions.get(sid)
    if sess is None:
        return idem_finish(store, key, request.url.path, err(404, {"type": "invalid_request", "code": "invalid", "message": f"No checkout session {sid!r}"}))
    if sess["status"] in TERMINAL or sess["status"] == "complete_in_progress":
        return idem_finish(store, key, request.url.path, err(400, {"type": "invalid_request", "code": "invalid", "message": f"Checkout session is {sess['status']} and cannot be updated"}))
    store.updates_seen += 1
    if store.updates_seen == 1:
        if (lat := store.knobs.get("latency_on_first_update_s")):
            store.stripe.sleep(lat)
        if (pc := store.knobs.get("price_change_on_first_update")):
            iid, factor = pc
            for li in sess["line_items"]:
                if li["item"]["id"] == iid:
                    old = li["unit_amount"]
                    li["unit_amount"] = int(old * factor)
                    m = dict(MSG_PRICE_CHANGE)
                    m["content"] = m["content"].format(name=li["name"], old=old, new=li["unit_amount"])
                    sess["_extra_msgs"].append(m)
    if "line_items" in body:
        country = ((sess.get("fulfillment_details") or {}).get("address") or {}).get("country") or "US"
        items, bad = build_line_items(store, body["line_items"], country)
        if bad is None and not items:
            bad = err(400, {"type": "invalid_request", "code": "invalid", "message": "line_items cannot be empty", "param": "$.line_items"})
        if bad is not None:
            return idem_finish(store, key, request.url.path, bad)
        if store.knobs.get("price_change_on_first_update"):
            bumped = {li["item"]["id"]: li["unit_amount"] for li in sess["line_items"]}
            for li in items:
                li["unit_amount"] = bumped.get(li["item"]["id"], li["unit_amount"])
        sess["line_items"] = items
        keep = {li["id"] for li in items}
        for sel in sess["selected_fulfillment_options"]:
            sel["item_ids"] = [x for x in sel["item_ids"] if x in keep]
    if "fulfillment_details" in body and body["fulfillment_details"] is not None:
        sess["fulfillment_details"] = body["fulfillment_details"]
    if "selected_fulfillment_options" in body and (bad := apply_selection(sess, body["selected_fulfillment_options"])) is not None:
        return idem_finish(store, key, request.url.path, bad)
    if "discounts" in body:
        codes = ((body.get("discounts") or {}).get("codes")) or []
        sess["_coupon"] = codes[0] if codes else None
    recompute(store, sess)
    return idem_finish(store, key, request.url.path, JSONResponse(public(sess), status_code=200))


@app.post("/checkout_sessions/{sid}/complete")
async def complete_session(sid: str, request: Request):
    store = store_of(request)
    body = await request.json()
    if (bad := guard(request, store, body)):
        return bad
    key = request.headers.get("idempotency-key")
    replay, _ = idem_begin(store, key, request.url.path, body)
    if replay is not None:
        return replay
    path = request.url.path
    sess = store.sessions.get(sid)
    if sess is None:
        return idem_finish(store, key, path, err(404, {"type": "invalid_request", "code": "invalid", "message": f"No checkout session {sid!r}"}))
    if key in store.recovery:
        # rfc 6.8 recovery point: the earlier attempt did the work, the response got lost
        return idem_finish(store, key, path, JSONResponse(store.recovery[key], status_code=200))
    if sess["status"] != "ready_for_payment":
        return idem_finish(store, key, path, err(400, {"type": "invalid_request", "code": "invalid", "message": f"Checkout session is not ready for payment (status: {sess['status']})", "param": "$.status"}))
    token = (((body.get("payment_data") or {}).get("instrument") or {}).get("credential") or {}).get("token")
    if not token:
        return idem_finish(store, key, path, err(400, {"type": "invalid_request", "code": "missing", "message": "payment_data.instrument.credential.token is required", "param": "$.payment_data.instrument.credential.token"}))
    if body.get("buyer"):
        sess["buyer"] = body["buyer"]
    total = next(t["amount"] for t in sess["totals"] if t["type"] == "total")
    if store.knobs.get("complete_pending_then_cancel"):
        sess["status"] = "complete_in_progress"
        sess["_pending_cancel"] = True
        store.charges.append({"token": token, "amount": total, "ok": False, "code": "deferred_fraud_review", "pi": None})
        return idem_finish(store, key, path, JSONResponse(public(sess), status_code=200))
    res = store.stripe.charge(token, total, sess["currency"])
    store.charges.append({"token": token, "amount": total, "ok": res.get("ok", False), "code": res.get("code"), "pi": res.get("id")})
    if not res.get("ok"):
        if res.get("code") == "requires_action":
            return idem_finish(store, key, path, err(400, ERR_REQUIRES_3DS))
        m = dict(MSG_PAYMENT_DECLINED)
        m["content"] = m["content"].format(detail=res.get("message") or res.get("code"))
        sess["_extra_msgs"].append(m)
        recompute(store, sess)
        return idem_finish(store, key, path, JSONResponse(public(sess), status_code=200))
    order = {"id": "ord_" + uuid.uuid4().hex[:10], "checkout_session_id": sid, "order_number": f"#{1000 + len(store.orders) + 1}",
             "permalink_url": f"https://merchant.test/orders/{sid}", "confirmation": {"confirmation_number": f"CNF-{1000 + len(store.orders) + 1}", "confirmation_email_sent": True},
             "amount": total, "payment_intent": res.get("id")}
    store.orders.append(order)
    done = dict(public(sess), status="completed", order=order)
    if store.knobs.get("lose_complete_response") and not store.lost_once:
        # order + charge happened, status write lost, client sees a timeout
        store.lost_once = True
        store.recovery[key] = done
        store.stripe.sleep(store.knobs.get("complete_delay_s", 0))
        return idem_finish(store, key, path, err(504, ERR_TIMEOUT))
    sess["status"] = "completed"
    sess["order"] = order
    return idem_finish(store, key, path, JSONResponse(public(sess), status_code=200))


@app.post("/checkout_sessions/{sid}/cancel")
async def cancel_session(sid: str, request: Request):
    store = store_of(request)
    body = await request.json() if int(request.headers.get("content-length") or 0) else {}
    if (bad := guard(request, store, body)):
        return bad
    key = request.headers.get("idempotency-key")
    replay, _ = idem_begin(store, key, request.url.path, body)
    if replay is not None:
        return replay
    sess = store.sessions.get(sid)
    if sess is None:
        return idem_finish(store, key, request.url.path, err(404, {"type": "invalid_request", "code": "invalid", "message": f"No checkout session {sid!r}"}))
    if sess["status"] in TERMINAL:
        return idem_finish(store, key, request.url.path, err(400, {"type": "invalid_request", "code": "invalid", "message": f"Checkout session is already {sess['status']}"}))
    sess["status"] = "canceled"
    return idem_finish(store, key, request.url.path, JSONResponse(public(sess), status_code=200))


@app.post("/_control/reset")
async def control_reset(request: Request):
    body = await request.json()
    store_of(request).reset(body["scenario"], body.get("trial", "adhoc"))
    return {"ok": True}


@app.get("/_control/state")
def control_state(request: Request):
    store = store_of(request)
    for s in store.sessions.values():
        flip_if_pending(s)
    return {"scenario": store.scenario, "trial": store.trial, "stripe_backend": store.stripe.name,
            "sessions": {k: public(v) for k, v in store.sessions.items()}, "orders": store.orders,
            "charges": store.charges, "calls": store.calls}
