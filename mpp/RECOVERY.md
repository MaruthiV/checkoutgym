# Merchant recipe: recover a paid result without replaying its credential

The [Stripe MPP guide](https://docs.stripe.com/payments/machine/mpp) shows a 402 challenge, a retry carrying a payment credential, and a paid response with a receipt. For a one-off API call, that is enough when the response arrives. For a paid task whose entire response disappears, the buyer needs an identity for the *original task* and a merchant-authorized way to fetch its result. Stripe PaymentIntent idempotency protects the payment side; [mppx rejects replayed Stripe charges](https://github.com/wevm/mppx/blob/main/src/stripe/server/Charge.ts), so a spent credential is not a result retrieval token.

This repo includes a small [reference merchant](recovery_server.ts), [buyer](recovery_buyer.ts), [one-shot TCP-reset proxy](response_loss_proxy.ts), and [Stripe test-mode runner](../scripts/mpp_recovery.py). The [before/after trace](../results/published/mpp-recovery-2026-10-01/README.md) is the measured outcome. The code is an application adaptation of the pinned Stripe TypeScript sample, not an assertion that Stripe's example promises task recovery.

## Route contract

1. An authenticated buyer sends the intended JSON input to `POST /operations`. The merchant records a fresh UUID, buyer identity, exact input hash, agreed USD 0.50 price, and `created` state **before** returning the operation ID. Two calls with the same input create two intentional operations.
2. The buyer persists that ID and input locally before calling `POST /operations/:id/pay`. The usual mppx 402 → payment credential → paid response flow runs on this route. The merchant records `payment_unknown` before verifying a credential, renders a screenshot of a bundled local HTML page after successful payment, then saves the PaymentIntent reference, original receipt header, and PNG-bearing result as `ready` before returning the paid response. Every transition is appended and synced.
3. If the paid response is lost, the same authenticated buyer sends the original input to `POST /operations/:id/result`. For `ready`, the merchant returns the saved result and original receipt. It requires neither the original credential nor a new SPT. Another buyer gets 404; modified input gets 409; another route gets 404. Calling `pay` again after `ready` gets 409, avoiding a fresh charge for the same operation.
4. `payment_unknown` returns HTTP 202. The buyer should preserve the operation and spending commitment and report unresolved payment, not authorize another purchase from a transport error alone. This example intentionally does not turn an unknown payment into a new 402.

The test fixture uses separate random 256-bit buyer keys passed through `X-Buyer-Key`; the intruder process receives only its own key, even though it knows the operation ID and input. Use your real merchant authentication in production. The operation ID, challenge ID, and PaymentIntent ID are identifiers, not authorization secrets. Use a transactional durable store rather than the sample's local JSONL when multiple server workers or hosts serve requests. The local log cannot make Stripe confirmation and merchant persistence atomic. The Chrome renderer uses `--no-sandbox` only on the bundled local page; do not reuse it for untrusted URLs without browser isolation.

## Reproduce and check

Follow [setup](README.md). Install Chrome or Chromium locally; the runner detects common binaries, or set `MPP_CHROME_BIN` to its executable path. Then run:

```sh
.venv/bin/python scripts/mpp_response_loss.py
.venv/bin/python scripts/mpp_recovery.py
```

The first command records the unmodified route's paid-but-undelivered outcome. The second verifies recovery under the same complete paid-response reset, restarts merchant and buyer before lookup, tries wrong-buyer/body/route access, repeats recovery, and makes one separate same-input purchase. Check both published traces before adopting the recipe; each command uses Stripe **test mode only**. The second command intentionally produces two 50-cent test payments.

The example does not cover every ambiguous payment state. If the server crashes after Stripe succeeds but before saving `ready`, the existing operation remains `payment_unknown`; the client gets an accurate pending outcome but cannot retrieve a result yet. The [local-only pending-state control](../results/published/mpp-recovery-2026-10-01/unknown-control.json) verifies that result lookup and repayment both return 202 for a synthetic unresolved operation, with no Stripe call. A production implementation needs authoritative reconciliation and a policy for worker failure/refunds. Do not infer payment failure from a missing local record, and do not give the buyer a merchant Stripe key. This boundary is why the recipe promises recovery only after the result has been saved, exactly where the published fault is injected.
