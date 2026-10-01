# MPP paid response loss: test-mode reproduction

On 2026-10-01, a scripted mppx 0.9.2 buyer paid a Stripe SPT-backed endpoint for a JSON result. The merchant saved the result and its PaymentIntent reference; a local proxy then reset the buyer's TCP connection before forwarding any paid-response headers. The buyer saw `TypeError: fetch failed`, with neither artifact nor receipt. The original credential returned HTTP 402 on retry. This is a missing **merchant application recovery path** in this small integration, not evidence of an mppx replay bug or a claim about Stripe's production systems.

| Check | Observed |
|---|---|
| No-fault control on the modified server | One 402 challenge, one SPT use, HTTP 200, validated JSON artifact and receipt; succeeded 50-cent test-mode PaymentIntent. |
| Paid request with whole-response loss | Merchant artifact file was flushed with `fsync` before its 200 response; proxy read that response and its receipt, then reset the downstream connection before sending headers. |
| Buyer-visible outcome | `TypeError: fetch failed`; no paid artifact or receipt delivered. |
| Same credential retried twice | HTTP 402 both times; no artifact or receipt. |
| Same credential, changed JSON body or alternate paid route | HTTP 402; no artifact or receipt. |
| Stripe ground truth | Saved PaymentIntent and Stripe lookup hashes match; status `succeeded`, USD 0.50, test mode, `machine_payment=true`. One matching MPP PaymentIntent appeared in the complete run-window list. |

The [manifest](manifest.json) pins the sample SHA, SDK and tool versions, source hashes, and fault transport. The [redacted trace](trace.json) records the client error, fault barrier, saved result, payment proof, and retry controls. Original SPT, payment credential, merchant key, and plaintext PaymentIntent ID are absent from these published files. The ignored local run retains the private payment reference for verification.

Reproduce from the project root after following [MPP setup](../../../mpp/README.md):

```sh
.venv/bin/python scripts/mpp_baseline.py
.venv/bin/python scripts/mpp_response_loss.py
```

Each command mints one expiring, USD 0.50-capped **test-mode** SPT and makes one successful test payment. The first is the no-fault control; the second produces `results/mpp-response-loss-*` with a manifest, trace, and private artifact/fault logs. The buyer subprocess gets only the SPT. The merchant key stays with the local server and verification harness.

The fault proxy is an experiment component. It waits for the entire upstream 200 body, while the server writes its result record before returning that response. It then destroys the downstream connection without forwarding the response headers or body. This tests delivery loss after local persistence; it does **not** test server crash between Stripe success and persistence, production network incidence, client restart, or a different authenticated caller. Body/route retries establish that this credential did not expose another result; they do not replace ownership tests for a future recovery endpoint.

Stripe's [MPP guide](https://docs.stripe.com/payments/machine/mpp) and [TypeScript sample](https://github.com/stripe-samples/machine-payments/tree/ee4b2cf9e1d4a2a1c5670b266a4cb97a3d9420f8/mpp/server/node-typescript) show charge, challenge, receipt, and content delivery, without a paid-result recovery operation. mppx intentionally [rejects a replayed Stripe PaymentIntent](https://github.com/wevm/mppx/blob/main/src/stripe/server/Charge.ts). The useful next contribution is a narrow reference integration: assign an operation and recovery authorization before payment, durably link it to the caller/request/payment/result, and let that buyer retrieve the original result without presenting the spent credential again. Similar recovery patterns exist elsewhere; this experiment does not claim invention of the pattern.
