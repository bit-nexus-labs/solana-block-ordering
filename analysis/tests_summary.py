#!/usr/bin/env python3
"""One table and one figure for the three pre-registered tests.

Reads the numbers exactly where each pre-registration said they would be read, so the figure shows
the official results and nothing recomputed:

* pre-registration 1 (H1): `results-s5/phase_effect.csv`, row `all clients`;
* pre-registration 2 (H4): `results-s6/by_provider.csv`, row `direct-quic`;
* pre-registration 3 (H4): `results-s7/by_provider.csv`, row `direct-quic`;
* context rows: all sessions, `results/by_provider.csv`, rows `direct-quic` and `public`;
  the exploratory value from sessions 1-4 is quoted from PREREGISTRATION.md.

    python analysis/analyze.py --data data --out results-s5 --sessions 20261103T153045Z-live
    python analysis/analyze.py --data data --out results-s6 --sessions 20261103T192233Z-live
    python analysis/analyze.py --data data --out results-s7 --sessions 20261104T080046Z-live
    python analysis/analyze.py --data data --out results
    python analysis/tests_summary.py
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

INK, MUTED, GRID = "#0b0b0b", "#898781", "#e1e0d9"
BLUE, ORANGE, NEUTRAL = "#2a78d6", "#eb6834", "#c3c2b7"


def _ci(text: str) -> tuple[float, float]:
    lo, hi = re.findall(r"[-+−]?\d+\.\d+", str(text).replace("−", "-"))[:2]
    return float(lo), float(hi)


def collect(root: Path) -> pd.DataFrame:
    rows = [{"test": "sessions 1-4, all paths (exploratory, where the idea came from)", "kind": "exploratory",
             "windows": 228, "diff": 0.23, "lo": 0.05, "hi": 0.39, "source": "PREREGISTRATION.md"}]
    pe = root / "results-s5" / "phase_effect.csv"
    if pe.exists():
        r = pd.read_csv(pe).set_index("client").loc["all clients"]
        rows.append({"test": "pre-registration 1: session 5, public RPC (H1)", "kind": "pre-registered",
                     "windows": 96, "diff": r["diff"], "lo": r.diff_lo, "hi": r.diff_hi,
                     "source": "results-s5/phase_effect.csv, all clients"})
    for n, sess, label in [(6, "results-s6", "pre-registration 2: session 6, direct QUIC (H4)"),
                           (7, "results-s7", "pre-registration 3: session 7, direct QUIC (H4)")]:
        p = root / sess / "by_provider.csv"
        if p.exists():
            r = pd.read_csv(p).set_index("provider").loc["direct-quic"]
            lo, hi = _ci(r.phase_diff_ci)
            rows.append({"test": label, "kind": "pre-registered", "windows": int(r.windows),
                         "diff": r.phase_diff, "lo": lo, "hi": hi, "source": f"{sess}/by_provider.csv, direct-quic"})
    p = root / "results" / "by_provider.csv"
    if p.exists():
        d = pd.read_csv(p).set_index("provider")
        for prov, label in [("direct-quic", "all sessions, direct QUIC"), ("public", "all sessions, public RPC")]:
            if prov in d.index and pd.notna(d.loc[prov, "phase_diff"]):
                lo, hi = _ci(d.loc[prov, "phase_diff_ci"])
                rows.append({"test": label, "kind": "all data", "windows": int(d.loc[prov, "windows"]),
                             "diff": d.loc[prov, "phase_diff"], "lo": lo, "hi": hi,
                             "source": f"results/by_provider.csv, {prov}"})
    t = pd.DataFrame(rows)
    t["confirmed"] = [("yes" if lo > 0 else "no") if k == "pre-registered" else ""
                      for k, lo in zip(t.kind, t.lo)]
    return t


def figure(t: pd.DataFrame, out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    fig, ax = plt.subplots(figsize=(10.5, 0.62 * len(t) + 1.7), dpi=150)
    y = np.arange(len(t))[::-1]
    colours = {"exploratory": NEUTRAL, "pre-registered": BLUE, "all data": ORANGE}
    for yi, (_, r) in zip(y, t.iterrows()):
        c = colours[r.kind]
        ax.plot([r.lo, r.hi], [yi, yi], color=c, lw=2.6, solid_capstyle="round")
        filled = r.kind != "pre-registered" or r.confirmed == "yes"
        ax.scatter([r["diff"]], [yi], s=80, zorder=3, color=c if filled else "white", edgecolor=c, linewidth=2)
        note = f"{r['diff']:+.2f}  ({r.lo:+.2f} … {r.hi:+.2f}),  {r.windows} windows"
        if r.confirmed == "yes":
            note += "   confirmed"
        elif r.confirmed == "no":
            note += "   not confirmed"
        ax.text(0.86, yi, note, va="center", fontsize=8.5, color=INK)
    ax.axvline(0, color=MUTED, lw=1, ls="--")
    ax.set_yticks(y, t.test, fontsize=9)
    ax.set_xlim(-0.35, 0.8)
    ax.set_xlabel("conflict pairs won by the higher fee: batch before the window minus inside it (95% CI)",
                  fontsize=9)
    ax.set_title("Three pre-registered tests: the effect lives on the direct path to the leader",
                 loc="left", fontsize=11, color=INK)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(axis="x", color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(out / "preregistered_tests.png", bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=Path("."), help="folder that holds results*/")
    ap.add_argument("--out", type=Path, default=Path("results"))
    a = ap.parse_args()
    t = collect(a.root)
    a.out.mkdir(parents=True, exist_ok=True)
    t.round(2).to_csv(a.out / "preregistered_tests.csv", index=False)
    figure(t, a.out)
    print(t.round(2).to_markdown(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
