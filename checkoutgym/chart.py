import json
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from .score import CLASS_ORDER, score_run

# reference categorical palette, fixed slot order, validated light mode 2026-09-08
CLASS_COLORS = {"amount": "#2a78d6", "thing": "#eb6834", "place": "#1baf7a", "credential": "#eda100", "hygiene": "#e87ba4", "escalation": "#008300"}
CLASS_LABELS = {"amount": "wrong amount", "thing": "wrong thing", "place": "wrong place", "credential": "wrong credential",
                "hygiene": "protocol hygiene", "escalation": "escalation / reporting"}
SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"

plt.rcParams.update({"font.family": "sans-serif", "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
                     "axes.edgecolor": AXIS, "axes.linewidth": 0.8, "xtick.color": INK2, "ytick.color": MUTED,
                     "axes.labelcolor": INK2, "text.color": INK})


def _clean_axes(ax):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.tick_params(length=0)
    ax.set_axisbelow(True)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_facecolor(SURFACE)


def draw(run_dir: str | Path, out: str | None = None) -> str:
    run_dir = Path(run_dir)
    summary = json.load(open(run_dir / "summary.json")) if (run_dir / "summary.json").exists() else score_run(run_dir)
    agents = [a for a, v in summary["agents"].items() if v["n"]]
    if not agents:
        raise SystemExit("no scored trials in run dir")
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(9.2, 4.4), dpi=200, gridspec_kw={"width_ratios": [3, 1.5], "wspace": 0.35}, facecolor=SURFACE)
    xs = list(range(len(agents)))
    width = 0.34 if len(agents) >= 4 else 0.2
    for i, agent in enumerate(agents):
        a = summary["agents"][agent]
        bottom = 0.0
        for cls in CLASS_ORDER:
            v = a["by_class_per_100"].get(cls) or 0
            if v:
                ax.bar(i, v, width=width, bottom=bottom, color=CLASS_COLORS[cls], edgecolor=SURFACE, linewidth=1.4, label=CLASS_LABELS[cls])
                bottom += v
        ax.text(i, bottom + 1.5, f"{bottom:.0f}", ha="center", va="bottom", fontsize=9, color=INK2)
    _clean_axes(ax)
    def label(a):
        m = summary["agents"][a].get("model")
        name = m if a.startswith("claudecode") and m else a
        return f"{name}\nn={summary['agents'][a]['n']}"
    ax.set_xticks(xs, [label(a) for a in agents], fontsize=8.5)
    ax.set_xlim(-0.6, len(agents) - 0.4)
    ax.set_ylabel("failures per 100 sessions", fontsize=9)
    ax.set_ylim(0, max(1.0, ax.get_ylim()[1] * 1.08))
    handles, labels = ax.get_legend_handles_labels()
    seen = {}
    for h, l in zip(handles, labels):
        seen.setdefault(l, h)
    order = [CLASS_LABELS[c] for c in CLASS_ORDER if CLASS_LABELS[c] in seen]
    ax.legend([seen[l] for l in order], order, loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=6, frameon=False, fontsize=8, labelcolor=INK2, handlelength=1.0)

    rates = [100 * (summary["agents"][a]["success_rate"] or 0) for a in agents]
    ax2.bar(xs, rates, width=width, color=MUTED, edgecolor=SURFACE, linewidth=1.4)
    for i, r in enumerate(rates):
        ax2.text(i, r + 2, f"{r:.0f}%", ha="center", va="bottom", fontsize=9, color=INK2)
    _clean_axes(ax2)
    ax2.set_xticks(xs, [label(a).split("\n")[0].replace("claude-", "") for a in agents], fontsize=8)
    ax2.set_ylim(0, 112)
    ax2.set_yticks([0, 25, 50, 75, 100])
    ax2.set_title("success rate", fontsize=10, color=INK, loc="left")

    run = summary.get("run", {})
    n = summary.get("n_trials", 0)
    sub = (f"{n} trials · {len(agents)} agents · {len(run.get('scenarios', []))} scenarios · {run.get('seeds', '?')} seeds · "
           f"stripe: {run.get('stripe_backend', '?')} · {time.strftime('%Y-%m-%d')}")
    fig.text(0.02, 0.97, "agent had a bad day, by reason", fontsize=13, fontweight="semibold", color=INK, va="top")
    fig.text(0.02, 0.905, sub, fontsize=8, color=MUTED, va="top")
    fig.subplots_adjust(top=0.80, bottom=0.26, left=0.08, right=0.98)
    path = Path(out) if out else run_dir / "chart.png"
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return str(path)
