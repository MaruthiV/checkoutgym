import os
import time
import uuid
from dataclasses import dataclass, field

import httpx

CARD_OK = "pm_card_visa"
CARD_DECLINE = "pm_card_visa_chargeDeclined"
CARD_3DS = "pm_card_threeDSecure2Required"


@dataclass
class FakeStripe:
    single_use: bool = False
    auto_mint: bool = False
    clock_offset: float = 0.0
    tokens: dict = field(default_factory=dict)
    intents: list = field(default_factory=list)

    @property
    def name(self) -> str:
        if self.auto_mint:
            return "fake(auto_mint)"
        return "fake(single_use)" if self.single_use else "fake(cumulative_cap)"

    def now(self) -> float:
        return time.time() + self.clock_offset

    def sleep(self, seconds: float) -> None:
        self.clock_offset += seconds

    def mint(self, max_amount: int, expires_in: int = 600, card: str = CARD_OK, currency: str = "usd") -> dict:
        tok = {
            "id": "spt_test_" + uuid.uuid4().hex[:20],
            "object": "shared_payment.granted_token",
            "usage_limits": {"currency": currency, "max_amount": max_amount, "expires_at": int(self.now() + expires_in)},
            "usage_details": {"amount_captured": {"value": 0, "currency": currency}},
            "deactivated_at": None,
            "deactivated_reason": None,
            "card": card,
        }
        self.tokens[tok["id"]] = tok
        return tok

    def get_token(self, token_id: str) -> dict:
        tok = self.tokens.get(token_id)
        if tok is None:
            return {"error": {"code": "resource_missing", "message": f"No such token: {token_id}"}}
        return {k: v for k, v in tok.items() if k != "card"}

    def charge(self, token_id: str, amount: int, currency: str = "usd") -> dict:
        tok = self.tokens.get(token_id)
        if tok is None and self.auto_mint and str(token_id).startswith("spt_"):
            # validator mode only, accept any spt_ string the conformance suite invents
            tok = self.mint(10**8, 3600)
            self.tokens[token_id] = self.tokens.pop(tok["id"])
            tok["id"] = token_id
        if tok is None:
            return _fail("resource_missing", f"No such shared payment token: {token_id}")
        if tok["deactivated_reason"]:
            return _fail("shared_payment_token_deactivated", f"Token deactivated: {tok['deactivated_reason']}")
        if self.now() > tok["usage_limits"]["expires_at"]:
            _deactivate(tok, "expired", self.now())
            return _fail("shared_payment_token_expired", "Token has expired")
        if currency != tok["usage_limits"]["currency"]:
            return _fail("currency_mismatch", "Token currency does not match")
        captured = tok["usage_details"]["amount_captured"]["value"]
        if captured + amount > tok["usage_limits"]["max_amount"]:
            return _fail("amount_exceeds_limit", f"Amount {amount} exceeds remaining limit {tok['usage_limits']['max_amount'] - captured}")
        if tok["card"] == CARD_DECLINE:
            pi = self._intent(tok, amount, currency, "canceled", "card_declined")
            return {"ok": False, "code": "card_declined", "message": "Your card was declined.", "id": pi["id"]}
        if tok["card"] == CARD_3DS:
            pi = self._intent(tok, amount, currency, "requires_action", None)
            return {"ok": False, "code": "requires_action", "message": "3D Secure authentication required", "id": pi["id"]}
        pi = self._intent(tok, amount, currency, "succeeded", None)
        tok["usage_details"]["amount_captured"]["value"] = captured + amount
        if self.single_use:
            _deactivate(tok, "consumed", self.now())
        return {"ok": True, "id": pi["id"], "amount": amount}

    def intents_for(self, token_id: str) -> list[dict]:
        return [pi for pi in self.intents if pi["token"] == token_id]

    def _intent(self, tok: dict, amount: int, currency: str, status: str, decline: str | None) -> dict:
        pi = {"id": "pi_test_" + uuid.uuid4().hex[:16], "amount": amount, "currency": currency,
              "status": status, "decline_code": decline, "token": tok["id"], "created": int(self.now())}
        self.intents.append(pi)
        return pi


def _fail(code: str, message: str) -> dict:
    return {"ok": False, "code": code, "message": message}


def _deactivate(tok: dict, reason: str, now: float) -> None:
    tok["deactivated_reason"] = reason
    tok["deactivated_at"] = int(now)


class RealStripe:
    name = "real(test-mode)"

    def __init__(self, secret_key: str | None = None, preview_version: str | None = None):
        self.key = secret_key or os.environ.get("STRIPE_SK_TEST", "")
        if not self.key.startswith("sk_test_"):
            raise RuntimeError("RealStripe needs a test-mode key in STRIPE_SK_TEST")
        # docs show 2026-04-22.preview, mppx sends 2026-07-29.preview, override via env
        self.version = preview_version or os.environ.get("STRIPE_PREVIEW_VERSION", "2026-04-22.preview")
        self.http = httpx.Client(base_url="https://api.stripe.com", auth=(self.key, ""), timeout=30)
        self.intents: list[dict] = []

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def _headers(self) -> dict:
        return {"Stripe-Version": self.version} if self.version else {}

    def mint(self, max_amount: int, expires_in: int = 600, card: str = CARD_OK, currency: str = "usd") -> dict:
        r = self.http.post("/v1/test_helpers/shared_payment/granted_tokens", headers=self._headers(), data={
            "payment_method": card,
            "usage_limits[currency]": currency,
            "usage_limits[max_amount]": max_amount,
            "usage_limits[expires_at]": int(time.time() + expires_in),
        })
        body = r.json()
        if r.status_code >= 400:
            raise RuntimeError(f"mint failed {r.status_code}: {body}")
        return body

    def get_token(self, token_id: str) -> dict:
        return self.http.get(f"/v1/shared_payment/granted_tokens/{token_id}", headers=self._headers()).json()

    def charge(self, token_id: str, amount: int, currency: str = "usd") -> dict:
        r = self.http.post("/v1/payment_intents", headers=self._headers(), data={
            "amount": amount, "currency": currency, "confirm": "true",
            "payment_method_data[shared_payment_granted_token]": token_id,
        })
        body = r.json()
        rec = {"token": token_id, "amount": amount, "http": r.status_code, "id": body.get("id"),
               "status": body.get("status"), "error": body.get("error")}
        self.intents.append(rec)
        if r.status_code >= 400:
            err = body.get("error", {})
            return {"ok": False, "code": err.get("decline_code") or err.get("code", "unknown"), "message": err.get("message", ""), "raw": body}
        if body.get("status") == "requires_action":
            return {"ok": False, "code": "requires_action", "message": "3D Secure authentication required", "id": body["id"]}
        if body.get("status") != "succeeded":
            return {"ok": False, "code": body.get("status", "unknown"), "message": "PaymentIntent not succeeded", "id": body.get("id")}
        return {"ok": True, "id": body["id"], "amount": amount}

    def intents_for(self, token_id: str) -> list[dict]:
        return [pi for pi in self.intents if pi["token"] == token_id]


def make_backend(kind: str) -> FakeStripe | RealStripe:
    if kind == "fake":
        return FakeStripe()
    if kind == "fake-single-use":
        return FakeStripe(single_use=True)
    if kind == "real":
        return RealStripe()
    raise ValueError(f"unknown stripe backend {kind}")
