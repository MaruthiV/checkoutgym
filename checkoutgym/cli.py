import argparse
import json
import sys

from dotenv import load_dotenv


def main(argv=None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="checkoutgym")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run agent x scenario x seed matrix")
    r.add_argument("--agents", default="naive,oracle")
    r.add_argument("--scenarios", default="all")
    r.add_argument("--seeds", type=int, default=2)
    r.add_argument("--stripe", default="fake", choices=["fake", "fake-cumulative", "real"])
    r.add_argument("--merchant-url", default=None, help="use a running merchant instead of in-process (real stripe only)")
    r.add_argument("--model", default=None)
    r.add_argument("--out", default=None)
    r.add_argument("--config", default="tasks.yaml")
    r.add_argument("--no-score", action="store_true")
    s = sub.add_parser("score", help="score a run dir")
    s.add_argument("run_dir")
    c = sub.add_parser("chart", help="draw chart.png for a run dir")
    c.add_argument("run_dir")
    c.add_argument("--out", default=None)
    v = sub.add_parser("serve", help="run the mock merchant on a port")
    v.add_argument("--scenario", default="S1")
    v.add_argument("--stripe", default="fake", choices=["fake", "fake-cumulative", "real"])
    v.add_argument("--port", type=int, default=8787)
    v.add_argument("--auto-mint", action="store_true", help="fake stripe accepts any spt_ token (for the acp-test validator)")
    sub.add_parser("probe", help="20 minute spt truth test against real stripe test mode")
    t = sub.add_parser("trace", help="print one trial's log lines")
    t.add_argument("run_dir")
    t.add_argument("trial")
    a = p.parse_args(argv)

    if a.cmd == "run":
        from .runner import ALL_SCENARIOS, run_matrix
        from .score import score_run, table
        scenarios = ALL_SCENARIOS if a.scenarios == "all" else a.scenarios.split(",")
        out = run_matrix(a.agents.split(","), scenarios, a.seeds, a.stripe, a.out, a.merchant_url, a.config, a.model)
        if not a.no_score:
            print(table(score_run(out)))
        return 0
    if a.cmd == "score":
        from .score import score_run, table
        summary = score_run(a.run_dir)
        print(table(summary))
        if summary["protocol_traps"]:
            print("\nprotocol traps (fired for every agent):", json.dumps(summary["protocol_traps"]))
        return 0
    if a.cmd == "chart":
        from .chart import draw, failure_map
        print(draw(a.run_dir, a.out))
        print(failure_map(a.run_dir))
        return 0
    if a.cmd == "serve":
        import uvicorn

        from .merchant.app import Store, app
        from .stripe_leg import FakeStripe, make_backend
        backend = FakeStripe(auto_mint=True) if a.auto_mint else make_backend(a.stripe)
        app.state.store = Store(backend)
        app.state.store.reset(a.scenario, "serve")
        print(f"merchant on http://127.0.0.1:{a.port} scenario={a.scenario} stripe={a.stripe}")
        uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="info")
        return 0
    if a.cmd == "trace":
        from .score import print_trace
        print_trace(a.run_dir, a.trial)
        return 0
    if a.cmd == "probe":
        from scripts.spt_probe import probe
        return probe()
    return 1


if __name__ == "__main__":
    sys.exit(main())
