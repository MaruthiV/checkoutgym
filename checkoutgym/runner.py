import importlib
import json
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import yaml
from fastapi.testclient import TestClient

from .agents.base import StepLimit, ToolRunner
from .stripe_leg import make_backend

AGENT_MODULES = {"naive": "checkoutgym.agents.naive", "oracle": "checkoutgym.agents.oracle",
                 "claude": "checkoutgym.agents.claude", "gpt": "checkoutgym.agents.gpt"}
ALL_SCENARIOS = [f"S{i}" for i in range(1, 13)]


def load_agent(name: str):
    return importlib.import_module(AGENT_MODULES[name])


def load_config(path: str = "tasks.yaml") -> dict:
    return yaml.safe_load(open(path))


def build_task(cfg: dict, scenario: str) -> dict:
    sc = cfg["scenarios"][scenario]
    base = cfg["tasks"][sc["task"]]
    dollars = f"{sc['budget'] / 100:.2f}"
    task = {"scenario": scenario, "task": sc["task"], "budget": sc["budget"], "card": sc.get("card", "pm_card_visa"),
            "token_ttl": sc.get("token_ttl", 600), "expect": sc["expect"], "line_items": base["line_items"],
            "coupon": base.get("coupon"), "one_delivery": bool(base.get("one_delivery")),
            "text": base["text"].replace("${budget_dollars}", dollars),
            "reply": (sc.get("reply") or "").replace("${budget_dollars}", dollars) or None}
    return task


def git_rev() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5).stdout.strip() or None
    except Exception:
        return None


def make_client(stripe, merchant_url: str | None):
    if merchant_url:
        return httpx.Client(base_url=merchant_url, timeout=120)
    from .merchant.app import Store, app
    app.state.store = Store(stripe)
    return TestClient(app)


def primary_session(state: dict) -> dict | None:
    sessions = list(state["sessions"].values())
    if not sessions:
        return None
    ordered = {o["checkout_session_id"] for o in state["orders"]}
    for s in sessions:
        if s["id"] in ordered:
            return s
    return sessions[-1]


def run_trial(agent_name: str, scenario: str, seed: int, cfg: dict, stripe, client, log, model: str | None = None) -> dict:
    agent = load_agent(agent_name)
    task = build_task(cfg, scenario)
    trial = f"{agent_name}-{scenario}-s{seed}"
    tok = stripe.mint(max_amount=task["budget"], expires_in=task["token_ttl"], card=task["card"])
    task["token"] = tok["id"]
    task["seed"] = seed
    client.post("/_control/reset", json={"scenario": scenario, "trial": trial})
    meta = {"trial": trial, "agent": agent_name, "model": model or getattr(agent, "NAME", agent_name), "task": task["task"], "scenario": scenario, "seed": seed}
    tools = ToolRunner(client, meta, tok["id"], task["budget"], task["reply"], cfg["buyer"], cfg["fulfillment_details"], log)
    t0 = time.time()
    error = None
    try:
        agent.run(tools, task)
    except StepLimit as e:
        error = f"step_cap: {e}"
    except Exception as e:  # errored trials are recorded, never dropped
        error = f"{type(e).__name__}: {e}"
    wall_ms = int((time.time() - t0) * 1000)
    state = client.get("/_control/state").json()
    sess = primary_session(state)
    orders = [o for o in state["orders"] if sess is None or o["checkout_session_id"] == sess["id"]]
    tok_state = stripe.get_token(tok["id"])
    captured = ((tok_state.get("usage_details") or {}).get("amount_captured") or {}).get("value")
    final_status = "completed" if orders else (sess["status"] if sess else None)
    gt = {"ts": round(time.time(), 3), "event": "ground_truth", **meta, "budget": task["budget"], "expect": task["expect"],
          "one_delivery": task["one_delivery"], "stripe_backend": getattr(stripe, "name", "?"),
          "sessions": len(state["sessions"]), "orders": len(state["orders"]), "order_ids": [o["id"] for o in state["orders"]],
          "amount_charged_merchant": sum(c["amount"] for c in state["charges"] if c["ok"]),
          "amount_captured_stripe": captured, "token_deactivated_reason": tok_state.get("deactivated_reason"),
          "charges": [{"ok": c["ok"], "code": c["code"], "amount": c["amount"]} for c in state["charges"]],
          "final_status": final_status, "session_status_field": sess["status"] if sess else None,
          "final_line_items": [[li["item"]["id"], li["quantity"]] for li in sess["line_items"]] if sess else [],
          "selected_options": [s["option_id"] for s in sess["selected_fulfillment_options"]] if sess else [],
          "offered_options": [o["id"] for o in sess["fulfillment_options"]] if sess else [],
          "merchant_calls": [{"path": c["path"], "method": c["method"], "api_version": c["api_version"], "has_key": bool(c["idempotency_key"])} for c in state["calls"]],
          "agent_claimed_order": bool(tools.claim and tools.claim.get("order_id")), "claimed_order_id": (tools.claim or {}).get("order_id"),
          "finished": tools.claim is not None, "api_calls": tools.step, "asked": tools.asked, "leaks": tools.leaks,
          "tokens_in": tools.tokens_in, "tokens_out": tools.tokens_out, "wall_ms": wall_ms, "error": error}
    log(gt)
    return gt


def run_matrix(agents: list[str], scenarios: list[str], seeds: int, stripe_kind: str, out_dir: str | None = None,
               merchant_url: str | None = None, config_path: str = "tasks.yaml", model: str | None = None, quiet: bool = False) -> Path:
    cfg = load_config(config_path)
    stripe = make_backend(stripe_kind)
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    out = Path(out_dir or f"results/{run_id}")
    out.mkdir(parents=True, exist_ok=True)
    events = open(out / "events.jsonl", "a")

    def log(line: dict) -> None:
        events.write(json.dumps(line, separators=(",", ":")) + "\n")
        events.flush()

    log({"ts": round(time.time(), 3), "event": "run", "run_id": run_id, "agents": agents, "scenarios": scenarios, "seeds": seeds,
         "stripe_backend": stripe.name, "merchant": merchant_url or "in-process", "config": config_path, "git_rev": git_rev(), "model": model})
    client = make_client(stripe, merchant_url)
    n = 0
    for agent in agents:
        for scenario in scenarios:
            for seed in range(1, seeds + 1):
                gt = run_trial(agent, scenario, seed, cfg, stripe, client, log, model=model)
                n += 1
                if not quiet:
                    flag = "ERR " if gt["error"] else ""
                    print(f"{flag}{gt['trial']:<22} orders={gt['orders']} charged={gt['amount_charged_merchant']:>5} status={gt['final_status']} claimed={gt['agent_claimed_order']} calls={gt['api_calls']}")
    events.close()
    if not quiet:
        print(f"{n} trials -> {out}")
    return out
