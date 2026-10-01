# CheckoutGym MPP reference flow

This is the pinned Stripe MPP baseline for the next CheckoutGym experiment. It accepts one Stripe test-mode SPT payment for a deterministic JSON artifact. The server follows the [Stripe TypeScript sample](https://github.com/stripe-samples/machine-payments/tree/ee4b2cf9e1d4a2a1c5670b266a4cb97a3d9420f8/mpp/server/node-typescript) at commit `ee4b2cf9e1d4a2a1c5670b266a4cb97a3d9420f8`, with its package versions and lockfile. The sample serves both Tempo and SPT; this baseline explicitly excludes Tempo and returns `{ "artifact_id": "fixture-v1", "data": "paid-json-v1" }` after the paid request. That is an application adaptation, not a claim about a Stripe defect.

The scripted buyer uses `mppx/client` and receives one SPT minted by the local test harness. It has no merchant secret key. The server rejects live keys and non-test profile IDs. The harness verifies the receipt reference against Stripe's test-mode PaymentIntent and writes only redacted identifiers and artifact hashes to the run trace.

From the project root:

```sh
uv sync
cd mpp
pnpm install --frozen-lockfile
pnpm run build
cd ..
```

Set `STRIPE_SK_TEST` and `STRIPE_PROFILE_ID` in the gitignored root `.env` using `.env.example` as a guide. The profile must be a `profile_test_...` ID belonging to the test account. Then run:

```sh
.venv/bin/python scripts/mpp_baseline.py
```

The command mints an expiring SPT capped at $0.50, starts the local server, completes one MPP purchase, retrieves the PaymentIntent, and writes `manifest.json` and `trace.json` under a new ignored `results/mpp-baseline-*` directory. It runs entirely in Stripe test mode. A successful payment alone is insufficient: the buyer must receive and validate the JSON artifact and receipt.

The verified October 1 no-fault run is in [results/published/mpp-baseline-2026-10-01](../results/published/mpp-baseline-2026-10-01/README.md). A later [whole-response-loss probe](../results/published/mpp-response-loss-2026-10-01/README.md) uses the modified server, a local TCP-reset proxy, and the same test-mode rail. Run it with `.venv/bin/python scripts/mpp_response_loss.py`. The optional `MPP_ARTIFACT_LOG` is an experiment-only merchant-side append-only result record, written and synced before returning a paid response. The buyer never receives the merchant key.

The [operation/result recipe](RECOVERY.md) supplies the missing application recovery path. Install local Chrome/Chromium or set `MPP_CHROME_BIN`, then run `.venv/bin/python scripts/mpp_recovery.py` for the paired TCP-reset test, buyer/merchant restart, recovered browser screenshot, ownership controls, and a second intentional same-input purchase. The [redacted repair trace](../results/published/mpp-recovery-2026-10-01/README.md) checks the saved result and receipt against Stripe test mode. This is a bounded merchant example; the unknown payment crash window remains unresolved.
