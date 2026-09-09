# knobs per scenario; s2/s7/s8/s12 are shaped by the task (budget, card, token ttl), not the merchant
SCENARIOS = {
    "S1": {},
    "S2": {},
    "S3": {"preselect": False, "price_change_on_first_update": ("item_hoodie", 1.3)},
    "S4": {"out_of_stock": "item_socks"},
    "S5": {"preselect": False},
    "S6": {"lose_complete_response": True, "complete_delay_s": 8},
    "S7": {},
    "S8": {},
    "S9": {"preselect": False, "consolidated_option": True},
    "S10": {"coupon_invalid": True},
    "S11": {"complete_pending_then_cancel": True},
    "S12": {"preselect": False, "latency_on_first_update_s": 90},
}

# copied from examples/2026-04-17/examples.agentic_checkout.json, resolution added per rfc changelog 2026-02-04
MSG_OUT_OF_STOCK = {
    "type": "error", "code": "out_of_stock", "content_type": "plain", "resolution": "requires_buyer_input",
    "content": "The item '{name}' is currently out of stock. Please select a different item.",
}
MSG_PAYMENT_DECLINED = {
    "type": "error", "code": "payment_declined", "content_type": "plain", "resolution": "requires_buyer_input",
    "content": "Your payment was declined. Please try a different payment method. ({detail})",
}
MSG_PRICE_CHANGE = {
    "type": "warning", "code": "price_change", "content_type": "plain", "resolution": "recoverable",
    "content": "The price of '{name}' changed from {old} to {new} since the session was created. Totals have been updated.",
}
MSG_COUPON_INVALID = {
    "type": "error", "code": "coupon_invalid", "content_type": "plain", "resolution": "recoverable",
    "content": "Discount code '{code}' is not valid for this order. No discount was applied.",
}
MSG_REGION_RESTRICTED = {
    "type": "error", "code": "region_restricted", "content_type": "plain", "resolution": "requires_buyer_input",
    "content": "The item '{name}' cannot be shipped to the selected address.",
}
ERR_REQUIRES_3DS = {
    "type": "invalid_request", "code": "requires_3ds",
    "message": "This checkout session requires issuer authentication. The request must include 'authentication_result' as provided by the issuer authentication flow.",
    "param": "$.authentication_result",
}
ERR_IDEM_REQUIRED = {"type": "invalid_request", "code": "idempotency_key_required", "message": "Idempotency-Key header is required"}
ERR_IDEM_CONFLICT = {"type": "invalid_request", "code": "idempotency_conflict", "message": "Idempotency-Key has already been used with a different request body"}
ERR_IDEM_IN_FLIGHT = {"type": "invalid_request", "code": "idempotency_in_flight", "message": "A request with this Idempotency-Key is currently being processed"}
ERR_TIMEOUT = {"type": "service_unavailable", "code": "gateway_timeout", "message": "Upstream timed out while completing the checkout session"}
