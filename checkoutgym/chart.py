import json
import time
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch, Rectangle

from .score import CLASS_ORDER, CLASSES, score_run

# reference categorical palette, fixed slot order, validated light mode 2026-09-08
CLASS_COLORS = {"amount": "#2a78d6", "thing": "#eb6834", "place": "#1baf7a", "credential": "#eda100", "hygiene": "#e87ba4", "escalation": "#008300"}
CLASS_LABELS = {"amount": "wrong amount", "thing": "wrong thing", "place": "wrong place", "credential": "wrong credential",
                "hygiene": "protocol hygiene", "escalation": "escalation / reporting"}
SHORT = {"paid_over_budget": "over budget", "ignored_price_change": "price change", "ignored_failed_discount": "bad coupon",
         "wrong_sku": "wrong sku", "wrong_qty": "wrong qty", "silent_substitution": "silent swap", "split_shipment": "split ship",
         "bad_api_version": "api version", "token_reuse": "token reuse", "expired_token_retry": "expired retry", "credential_leak": "cred leak",
         "missing_idempotency_key": "missing key", "new_key_on_retry": "new key retry", "complete_before_ready": "not ready",
         "retry_after_hard_error": "blind retry", "failed_to_escalate": "no escalate", "over_escalated": "over escalate",
         "hallucinated_success": "fake success", "missed_success": "missed success", "gave_up_early": "gave up"}
SURFACE, INK, INK2, MUTED, GRID, AXIS, CLEAN = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#f0efec"

plt.rcParams.update({"font.family": "sans-serif", "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
                     "axes.edgecolor": AXIS, "axes.linewidth": 0.8, "xtick.color": INK2, "ytick.color": MUTED,
                     "axes.labelcolor": INK2, "text.color": INK, "svg.fonttype": "none"})


def _load(run_dir: Path) -> tuple[dict, list[dict]]:
    summary = json.load(open(run_dir / "summary.json")) if (run_dir / "summary.json").exists() else score_run(run_dir)
    rows = [json.loads(line) for line in open(run_dir / "results.jsonl")]
    return summary, rows


def _agent_label(summary: dict, a: str) -> str:
    m = summary["agents"][a].get("model")
    return m.replace("claude-", "") if a.startswith("claudecode") and m else a


def _save(fig, base: Path) -> str:
    fig.savefig(base.with_suffix(".svg"), facecolor=SURFACE)
    fig.savefig(base.with_suffix(".png"), facecolor=SURFACE, dpi=300)
    plt.close(fig)
    return str(base.with_suffix(".svg"))


def _luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def draw(run_dir: str | Path, out: str | None = None) -> str:
    run_dir = Path(run_dir)
    summary, _ = _load(run_dir)
    agents = [a for a, v in summary["agents"].items() if v["n"]]
    if not agents:
        raise SystemExit("no scored trials in run dir")
    fig, ax = plt.subplots(figsize=(8.4, 4.8), facecolor=SURFACE)
    width = 0.32 if len(agents) >= 4 else 0.22
    for i, a in enumerate(agents):
        v = summary["agents"][a]
        bottom = 0.0
        for cls in CLASS_ORDER:
            h = v["by_class_per_100"].get(cls) or 0
            if h:
                ax.bar(i, h, width=width, bottom=bottom, color=CLASS_COLORS[cls], edgecolor=SURFACE, linewidth=1.6)
                bottom += h
        ax.text(i, bottom + 2, f"{bottom:.0f}", ha="center", va="bottom", fontsize=10, color=INK2)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0)
    ax.set_axisbelow(True)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_facecolor(SURFACE)
    ax.set_xticks(range(len(agents)), [f"{_agent_label(summary, a)}\n{summary['agents'][a]['success']}/{summary['agents'][a]['n']} clean" for a in agents], fontsize=9.5)
    ax.set_xlim(-0.6, len(agents) - 0.4)
    ax.set_ylabel("failures per 100 sessions", fontsize=10)
    ax.set_ylim(0, max(10.0, ax.get_ylim()[1] * 1.1))
    ax.tick_params(axis="y", labelsize=9)
    present = [c for c in CLASS_ORDER if any((summary["agents"][a]["by_class_per_100"].get(c) or 0) for a in agents)]
    ax.legend([Patch(facecolor=CLASS_COLORS[c]) for c in present], [CLASS_LABELS[c] for c in present], loc="upper center",
              bbox_to_anchor=(0.5, -0.17), ncol=min(6, len(present)), frameon=False, fontsize=9, labelcolor=INK2, handlelength=1.0, columnspacing=1.4)
    run = summary.get("run", {})
    sub = (f"{summary.get('n_trials', 0)} trials · {len(agents)} agents · {len(run.get('scenarios', []))} scenarios · {run.get('seeds', '?')} seeds · "
           f"stripe {run.get('stripe_backend', '?')} · {time.strftime('%Y-%m-%d')}")
    fig.text(0.02, 0.97, "agent had a bad day, by reason", fontsize=14, fontweight="bold", color=INK, va="top")
    fig.text(0.02, 0.9, sub, fontsize=9, color=MUTED, va="top")
    fig.subplots_adjust(top=0.79, bottom=0.27, left=0.1, right=0.98)
    return _save(fig, Path(out).with_suffix("") if out else run_dir / "chart")


def failure_map(run_dir: str | Path, out: str | None = None) -> str:
    run_dir = Path(run_dir)
    summary, rows = _load(run_dir)
    agents = [a for a, v in summary["agents"].items() if v["n"]]
    scenarios = sorted({r["scenario"] for r in rows}, key=lambda x: int(x[1:]))
    fig, ax = plt.subplots(figsize=(11, 0.7 * len(agents) + 2.3), facecolor=SURFACE)
    seen = set()
    for i, a in enumerate(agents):
        for j, sc in enumerate(scenarios):
            trials = [r for r in rows if r["agent"] == a and r["scenario"] == sc]
            codes = Counter(c for r in trials for c in r["codes"])
            bad = sum(bool(r["codes"]) for r in trials)
            if not codes:
                ax.add_patch(Rectangle((j, i), 1, 1, facecolor=CLEAN, edgecolor=SURFACE, linewidth=2.5))
                ax.text(j + 0.5, i + 0.5, "clean", ha="center", va="center", fontsize=8, color=MUTED)
                continue
            top = codes.most_common(1)[0][0]
            cls = CLASSES[top]
            seen.add(cls)
            color = CLASS_COLORS[cls]
            ax.add_patch(Rectangle((j, i), 1, 1, facecolor=color, edgecolor=SURFACE, linewidth=2.5))
            label = SHORT.get(top, top) + ("" if bad == len(trials) else f"\n{bad} of {len(trials)}")
            ax.text(j + 0.5, i + 0.5, label, ha="center", va="center", fontsize=8, color="#ffffff" if _luminance(color) < 0.3 else INK)
    ax.set_xlim(0, len(scenarios))
    ax.set_ylim(len(agents), 0)
    ax.set_xticks([j + 0.5 for j in range(len(scenarios))], scenarios, fontsize=9.5, color=INK2)
    ax.set_yticks([i + 0.5 for i in range(len(agents))], [_agent_label(summary, a) for a in agents], fontsize=9.5, color=INK2)
    ax.tick_params(length=0)
    for side in ax.spines.values():
        side.set_visible(False)
    ax.set_facecolor(SURFACE)
    present = [c for c in CLASS_ORDER if c in seen]
    ax.legend([Patch(facecolor=CLASS_COLORS[c]) for c in present], [CLASS_LABELS[c] for c in present], loc="upper center",
              bbox_to_anchor=(0.5, -0.12), ncol=min(6, len(present)), frameon=False, fontsize=9, labelcolor=INK2, handlelength=1.0, columnspacing=1.4)
    fig.text(0.02, 0.96, "which failure, where", fontsize=14, fontweight="bold", color=INK, va="top")
    fig.text(0.02, 0.88, "one cell per agent and scenario: the most common failure code across both seeds, colored by class. blank means clean in both seeds.",
             fontsize=9, color=MUTED, va="top")
    fig.subplots_adjust(top=0.76, bottom=0.2, left=0.13, right=0.99)
    return _save(fig, Path(out).with_suffix("") if out else run_dir / "failure_map")
