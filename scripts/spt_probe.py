import json
import os
import time

import httpx
from dotenv import load_dotenv

HEADERS = ["2026-04-22.preview", "2026-07-29.preview", None]
CARDS = ["pm_card_visa", "pm_card_visa_chargeDeclined", "pm_card_threeDSecure2Required"]


def probe() -> int:
    load_dotenv()
    key = os.environ.get("STRIPE_SK_TEST", "")
    if not key.startswith("sk_test_"):
        print("STRIPE_SK_TEST (sk_test_...) not set in env/.env")
        return 2
    http = httpx.Client(base_url="https://api.stripe.com", auth=(key, ""), timeout=30)
    out = [f"# spt probe {time.strftime('%Y-%m-%d %H:%M')}\n", "test mode only. every response verbatim, secrets none.\n"]

    def log(title, r):
        out.append(f"\n## {title}\n\nhttp {r.status_code}\n\n```json\n{json.dumps(r.json(), indent=1)[:3000]}\n```\n")
        print(f"{title}: http {r.status_code}")
        return r.json()

    def mint(ver, amount, ttl, card="pm_card_visa"):
        h = {"Stripe-Version": ver} if ver else {}
        return http.post("/v1/test_helpers/shared_payment/granted_tokens", headers=h, data={
            "payment_method": card, "usage_limits[currency]": "usd", "usage_limits[max_amount]": amount, "usage_limits[expires_at]": int(time.time() + ttl)})

    def charge(ver, tok, amount):
        h = {"Stripe-Version": ver} if ver else {}
        return http.post("/v1/payment_intents", headers=h, data={"amount": amount, "currency": "usd", "confirm": "true", "payment_method_data[shared_payment_granted_token]": tok})

    working = None
    for ver in HEADERS:
        r = mint(ver, 6000, 600)
        body = log(f"mint with Stripe-Version={ver}", r)
        if r.status_code < 400 and body.get("id"):
            working = ver
            break
    if working is None:
        out.append("\nno header minted a token. fake backend stays the run of record.\n")
        return finish(out, 1)
    out.append(f"\nworking header: `{working}`\n")
    tok = mint(working, 6000, 600).json()["id"]
    log("charge 1 under cap (2000)", charge(working, tok, 2000))
    log("charge 2 same token (2000) - single use or cumulative?", charge(working, tok, 2000))
    log("charge 3 same token over remaining cap (3000)", charge(working, tok, 3000))
    log("token state after charges", http.get(f"/v1/shared_payment/granted_tokens/{tok}", headers={"Stripe-Version": working} if working else {}))
    tok2 = mint(working, 6000, 5).json()["id"]
    time.sleep(8)
    log("charge with expired token", charge(working, tok2, 1000))
    log("expired token state", http.get(f"/v1/shared_payment/granted_tokens/{tok2}", headers={"Stripe-Version": working} if working else {}))
    for card in CARDS[1:]:
        t = mint(working, 6000, 600, card).json().get("id")
        if t:
            log(f"charge on {card}", charge(working, t, 1000))
    return finish(out, 0)


def finish(out, code):
    os.makedirs("docs/notes", exist_ok=True)
    path = f"docs/notes/spt-probe-{time.strftime('%Y%m%d-%H%M')}.md"
    open(path, "w").write("".join(out))
    print("wrote", path)
    return code


if __name__ == "__main__":
    raise SystemExit(probe())
