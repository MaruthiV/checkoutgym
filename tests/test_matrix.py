import json

from checkoutgym.runner import ALL_SCENARIOS, run_matrix
from checkoutgym.score import score_run


def test_free_matrix_discriminates(tmp_path):
    out = run_matrix(["naive", "oracle"], ALL_SCENARIOS, 1, "fake", str(tmp_path / "run"), quiet=True)
    summary = score_run(out)
    rows = [json.loads(line) for line in open(out / "results.jsonl")]
    oracle = [r for r in rows if r["agent"] == "oracle"]
    naive = [r for r in rows if r["agent"] == "naive"]
    bad = [(r["scenario"], r["codes"], r["error"]) for r in oracle if r["codes"] or not r["success"]]
    assert not bad, bad
    assert sum(bool(r["codes"]) for r in naive) >= 6
    by = {r["scenario"]: set(r["codes"]) for r in naive}
    assert "paid_over_budget" in by["S2"] and "new_key_on_retry" in by["S6"] and "missed_success" in by["S6"]
    assert "split_shipment" in by["S9"] and "ignored_failed_discount" in by["S10"] and "expired_token_retry" in by["S12"]
    assert by["S1"] == set() and by["S5"] == set()
    assert not any(r["errored"] for r in rows)
    assert not any("spt_test_" in line for line in open(out / "events.jsonl"))
    assert summary["agents"]["oracle"]["success_rate"] == 1.0
