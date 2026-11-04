#!/usr/bin/env python3
"""Exploratory: when exactly does the fee decide, and how close in time can arrival order be seen?

Two cuts of the same same-block pairs that analyze.py uses (all sessions, scheduled leader's blocks):

* contention × phase: our transactions either share one writable account (one fee payer) or touch
  nothing in common (eight payers); the batch arrives before the window or inside it;
* send gap: how far apart two transactions were sent (from our own nanosecond timestamps), and how
  often the earlier one is placed first.

    python analysis/mechanism.py --data data --out results

Not pre-registered. Intervals: 95%, resampling whole windows (analyze.cluster_share).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import analyze as A  # noqa: E402

GAP_BINS = [0, 4, 16, 32, 64, 128, 1000]
GAP_LABELS = ["≤4", "4–16", "16–32", "32–64", "64–128", ">128"]


def tables(pairs: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    sb = pairs[pairs.same_block]
    rows = []
    for (c, ph), g in sb.groupby(["contention", "phase"]):
        r = A.pair_summary(g)
        rows.append({"contention": c, "phase": ph, "pairs": r["pairs"], "windows": r["pair_windows"],
                     "follows_send_order": r["arrival_agree"], "lo_a": r["arrival_agree_lo"], "hi_a": r["arrival_agree_hi"],
                     "follows_fee": r["fee_agree"], "lo_f": r["fee_agree_lo"], "hi_f": r["fee_agree_hi"],
                     "fee_wins_conflicts": r["fee_wins_conflicts"], "lo_c": r["fee_wins_lo"], "hi_c": r["fee_wins_hi"]})
    cp = pd.DataFrame(rows)
    g = sb[sb.arrival_sign != 0].copy()
    g["gap"] = pd.cut(g.gap_ms, GAP_BINS, labels=GAP_LABELS)
    rows = []
    for (ph, c, gb), x in g.groupby(["phase", "contention", "gap"], observed=True):
        if len(x) < 30:
            continue
        sh, lo, hi = A.cluster_share(x, x.arrival_sign > 0)
        rows.append({"phase": ph, "contention": c, "gap_ms": str(gb), "pairs": len(x),
                     "follows_send_order": sh, "lo": lo, "hi": hi})
    return cp, pd.DataFrame(rows)


def figure(cp: pd.DataFrame, gp: pd.DataFrame, out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    INK, MUTED, GRID = A.INK, A.MUTED, A.GRID
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11.5, 4.3), dpi=150, gridspec_kw={"width_ratios": [1, 1.35]})
    order = [("shared", "pre"), ("independent", "pre"), ("shared", "inside"), ("independent", "inside")]
    labels = ["one shared account,\nbefore the window", "no shared account,\nbefore the window",
              "one shared account,\ninside the window", "no shared account,\ninside the window"]
    y = np.arange(len(order))[::-1]
    for yi, key in zip(y, order):
        r = cp[(cp.contention == key[0]) & (cp.phase == key[1])].iloc[0]
        col = A.ORANGE if key == ("shared", "pre") else A.NEUTRAL
        a1.plot([r.lo_c, r.hi_c], [yi, yi], color=col, lw=2.6, solid_capstyle="round")
        a1.scatter([r.fee_wins_conflicts], [yi], s=70, color=col, zorder=3)
        a1.text(r.hi_c + 0.02, yi, f"{r.fee_wins_conflicts:.2f}", va="center", fontsize=8.5, color=INK)
    a1.axvline(0.5, color=MUTED, lw=1, ls="--")
    a1.set_yticks(y, labels, fontsize=8.5)
    a1.set_xlim(0.1, 0.9)
    a1.set_xlabel("conflict pairs won by the higher fee (95% CI)", fontsize=9)
    a1.set_title("The fee decides only for transactions that\nwait together and lock the same account", loc="left",
                 fontsize=10, color=INK)
    for (ph, c), col, ls in [(("inside", "shared"), A.BLUE, "-"), (("inside", "independent"), A.BLUE, "--"),
                             (("pre", "shared"), A.ORANGE, "-"), (("pre", "independent"), A.ORANGE, "--")]:
        d = gp[(gp.phase == ph) & (gp.contention == c)]
        d = d.set_index("gap_ms").reindex(GAP_LABELS).dropna(subset=["follows_send_order"])
        xs = [GAP_LABELS.index(i) for i in d.index]
        a2.plot(xs, d.follows_send_order, ls, color=col, lw=2, marker="o", ms=4.5,
                label=f"{'inside' if ph == 'inside' else 'before'} the window, {'one shared' if c == 'shared' else 'no shared'} account")
    a2.axhline(0.5, color=MUTED, lw=1, ls="--")
    a2.set_xticks(range(len(GAP_LABELS)), GAP_LABELS)
    a2.set_ylim(0.3, 1.0)
    a2.set_xlabel("how far apart we sent the two transactions (ms)", fontsize=9)
    a2.set_ylabel("pairs placed in send order", fontsize=9)
    a2.set_title("Arrival order shows only for gaps above ~30 ms;\ninside the window it is almost strict",
                 loc="left", fontsize=10, color=INK)
    a2.legend(frameon=False, fontsize=7.5, loc="lower right")
    for ax in (a1, a2):
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        ax.grid(axis="x" if ax is a1 else "y", color=GRID, lw=0.8)
        ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(out / "mechanism.png", bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=Path("data"))
    ap.add_argument("--out", type=Path, default=Path("results"))
    a = ap.parse_args()
    sends, windows, _, _ = A.load_sessions(sorted(p for p in a.data.iterdir() if p.is_dir()))
    _, pairs = A.window_rows(sends, windows)
    cp, gp = tables(pairs)
    a.out.mkdir(parents=True, exist_ok=True)
    cp.round(3).to_csv(a.out / "mechanism_contention_phase.csv", index=False)
    gp.round(3).to_csv(a.out / "mechanism_send_gap.csv", index=False)
    figure(cp, gp, a.out)
    print(cp.round(2).to_markdown(index=False))
    print()
    print(gp.round(2).to_markdown(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
