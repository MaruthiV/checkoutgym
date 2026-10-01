# October 1 scoring correction

This directory re-scores the original September 8 CheckoutGym run. It contains **no new agent trials or Stripe payments**. The original directory, `../matrix-real-3models-2026-09-08/`, remains unchanged, including the scores used in the September 9 email.

## Why the score changed

The old `ignored_failed_discount` rule marked the next `complete_checkout` as a failure whenever the merchant returned `coupon_invalid`. In S10 the buyer said to apply SAVE10 **if it works** and set a $55 hard budget. The merchant's update response rejected the coupon and returned the unchanged $53.97 total. All six model S10 traces completed under budget and reported that the coupon was not applied. The naive script also completed under budget in both repetitions. Requiring a separate GET before payment was not justified by the task or the update response.

Scoring version `2026-10-01-coupon-v2` removes that predicate. An attempt to complete above the budget still gets `paid_over_budget`, including when Stripe blocks the charge. The price-change rule and all other predicates are unchanged.

| Agent | Removed S10 codes | Original clean | Corrected clean | Original failures / 100 | Corrected failures / 100 |
|---|---:|---:|---:|---:|---:|
| Haiku 4.5 via Claude Code | 2 | 0/24 | 0/24 | 150.0 | 141.7 |
| Sonnet 5 via Claude Code | 2 | 0/24 | 0/24 | 125.0 | 116.7 |
| Opus 5 via Claude Code | 2 | 18/24 | 20/24 | 25.0 | 16.7 |
| Naive script | 2 | 4/24 | 6/24 | 191.7 | 183.3 |
| Oracle script | 0 | 24/24 | 24/24 | 0.0 | 0.0 |

The eight removed codes are the **only differences** between the 120 original and corrected trial rows. Right-outcome counts are unchanged. The old summary's S10 `protocol_traps` entry was an artifact of this rule; the corrected summary has no protocol traps.

## Provenance and reproduction

- Original run ID: `20260908-230245-ff19`; original run header recorded Git revision `50e240c`, five arms, 12 scenarios, two repetitions, and Stripe `real(test-mode)`.
- Original and copied `events.jsonl` SHA-256: `340414909618be64e0e5d196e7326e1e04ec8d119e7611c1af1868e5fc574612`. The byte-identical events are redacted and stored alongside the corrected results.
- The scorer writes `scoring_version` to `summary.json`. `results.jsonl`, `summary.json`, `chart.svg/png`, and `failure_map.svg/png` were generated with that version; the chart's display date reflects generation time.
- Regression checks: a rejected optional coupon with a $53.97 total and $55 budget is clean; the same purchase attempt with a $52 budget still triggers `paid_over_budget`.

To reproduce from the project root, copy the original event log to a fresh directory, then run:

```sh
uv run checkoutgym score results/published/matrix-real-3models-2026-09-08-rescored-2026-10-01
uv run checkoutgym chart results/published/matrix-real-3models-2026-09-08-rescored-2026-10-01
```

These commands overwrite only the corrected results in this directory. They do not rerun agents or call Stripe. Compare by trial ID with the original `results.jsonl` to verify that only the eight S10 code sets changed.
