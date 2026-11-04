#!/usr/bin/env python3
"""Turns the raw probe logs into per-window, per-validator and per-client results.

Input: one or more session folders, each with sends.jsonl, windows.jsonl, landed.jsonl,
blocks.jsonl (written by `ordprobe run` and `ordprobe collect`).

The unit of evidence is a *pair* of our own transactions that landed in the scheduled
leader's own blocks. For every pair we know which one was sent first and which one paid
more per compute unit, and we observe which one the leader put first. Because fee levels
and send order are drawn independently at random, the two explanations can be told apart:

* arrival rule: the earlier-sent transaction comes first;
* priority rule: the higher-paying transaction comes first;
* no rule: neither predicts the order better than a coin flip.

Pairs inside one block show the scheduler's choice directly. Pairs split across two
blocks of the window are reported separately, because "sent later, so it landed in a later
slot" is a timing effect and not a scheduling decision.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

RNG = np.random.default_rng(20261026)


# ---------------------------------------------------------------- loading

def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_sessions(paths: list[Path]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    sends, windows, blocks, skipped = [], [], [], []
    for p in paths:
        s = read_jsonl(p / "sends.jsonl")
        if not s or all(r.get("dry_run") for r in s):
            continue
        landed = {r["signature"]: r for r in read_jsonl(p / "landed.jsonl")}
        if not landed:
            print(f"warning: {p.name} has no landed.jsonl yet (run `ordprobe collect`)", file=sys.stderr)
            continue
        for r in s:
            l = landed.get(r["signature"], {})
            r.update({k: l.get(k) for k in ("landed", "slot", "index_in_block", "block_tx_count",
                                            "block_producer", "fee_lamports", "compute_units_consumed",
                                            "tx_error")})
        sends += s
        for w in read_jsonl(p / "windows.jsonl"):
            arm = w.pop("arm")
            arm.setdefault("phase", "pre")  # sessions before the phase factor existed
            w.update({f"arm_{k}": v for k, v in arm.items()})
            windows.append(w)
        blocks += read_jsonl(p / "blocks.jsonl")
        skipped += read_jsonl(p / "skipped.jsonl")
    return pd.DataFrame(sends), pd.DataFrame(windows), pd.DataFrame(blocks), pd.DataFrame(skipped)


# ---------------------------------------------------------------- statistics

def kendall_tau_b(x: np.ndarray, y: np.ndarray) -> float:
    """Kendall's tau-b; NaN if one side has no variation."""
    n = len(x)
    if n < 2:
        return float("nan")
    conc = disc = tx = ty = 0
    for i in range(n):
        for j in range(i + 1, n):
            dx = np.sign(x[i] - x[j])
            dy = np.sign(y[i] - y[j])
            if dx == 0 and dy == 0:
                continue
            if dx == 0:
                tx += 1
            elif dy == 0:
                ty += 1
            elif dx == dy:
                conc += 1
            else:
                disc += 1
    denom = math.sqrt((conc + disc + tx) * (conc + disc + ty))
    return (conc - disc) / denom if denom else float("nan")


def perm_pvalue(pos: np.ndarray, pred: np.ndarray, n_perm: int = 2000) -> float:
    """Two-sided permutation p-value for tau(pos, pred) against random order."""
    obs = kendall_tau_b(pos, pred)
    if math.isnan(obs):
        return float("nan")
    hits = 0
    for _ in range(n_perm):
        t = kendall_tau_b(RNG.permutation(pos), pred)
        if not math.isnan(t) and abs(t) >= abs(obs) - 1e-12:
            hits += 1
    return (hits + 1) / (n_perm + 1)


def bootstrap_ci(values: np.ndarray, n_boot: int = 4000, alpha: float = 0.05) -> tuple[float, float]:
    values = values[~np.isnan(values)]
    if len(values) == 0:
        return float("nan"), float("nan")
    if len(values) == 1:
        return float(values[0]), float(values[0])
    idx = RNG.integers(0, len(values), size=(n_boot, len(values)))
    means = values[idx].mean(axis=1)
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return float("nan"), float("nan")
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def logistic_fit(X: np.ndarray, y: np.ndarray, iters: int = 50) -> tuple[np.ndarray, np.ndarray]:
    """Logistic regression by IRLS with a tiny ridge; returns coefficients and std errors."""
    beta = np.zeros(X.shape[1])
    for _ in range(iters):
        z = X @ beta
        p = 1 / (1 + np.exp(-z))
        w = np.clip(p * (1 - p), 1e-9, None)
        H = X.T @ (X * w[:, None]) + 1e-6 * np.eye(X.shape[1])
        g = X.T @ (y - p)
        step = np.linalg.solve(H, g)
        beta += step
        if np.max(np.abs(step)) < 1e-8:
            break
    cov = np.linalg.inv(H)
    return beta, np.sqrt(np.diag(cov))


# ---------------------------------------------------------------- per window

def _provider(w) -> str:
    """Send path label: the RPC provider for `rpc` windows (sessions before the provider
    factor used the public RPC only), `direct-quic` for `tpu` windows."""
    if w.arm_path != "rpc":
        return "direct-quic"
    v = w.get("send_provider") if hasattr(w, "get") else None
    return v if isinstance(v, str) and v else "public"


def window_rows(sends: pd.DataFrame, windows: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-window metrics and the table of all within-window pairs."""
    out, pairs = [], []
    for wid_key, g in sends.groupby(["session", "window_id"]):
        w = windows[(windows.session == wid_key[0]) & (windows.window_id == wid_key[1])]
        if w.empty:
            continue
        w = w.iloc[0]
        own = g[(g.landed == True) & (g.block_producer == w.leader) & g.index_in_block.notna()]  # noqa: E712
        other = g[(g.landed == True) & (g.block_producer != w.leader)]  # noqa: E712
        landed = g[g.landed == True]  # noqa: E712
        first = g.sort_values("send_rank").iloc[0]
        row = {
            "session": w.session, "window_id": int(w.window_id), "leader": w.leader,
            "client": w.client_id, "version": w.version, "first_slot": int(w.first_slot),
            "path": w.arm_path, "contention": w.arm_contention, "fees": w.arm_fees,
            "spacing_ms": int(w.arm_spacing_ms), "phase": w.arm_phase, "n_sent": len(g),
            "provider": _provider(w),
            # where the probe's clock was when the first transaction went out, in slots from
            # the window start, and how many slots later the transactions landed
            "fire_offset": float(first.est_slot_at_send - w.first_slot),
            "land_delay": float((landed.slot - landed.est_slot_at_send).median()) if len(landed) else float("nan"),
            "n_landed_leader": len(own), "n_landed_other_leader": len(other),
            "n_dropped": int((g.landed != True).sum()),  # noqa: E712
            "slots_used": int(own.slot.nunique()) if len(own) else 0,
        }
        if len(own) >= 2:
            own = own.sort_values(["slot", "index_in_block"]).reset_index(drop=True)
            pos = np.arange(len(own), dtype=float)
            send_rank = own.send_rank.to_numpy(dtype=float)
            neg_fee = -own.fee_level.to_numpy(dtype=float)
            row["tau_arrival"] = kendall_tau_b(pos, send_rank)
            row["p_arrival"] = perm_pvalue(pos, send_rank)
            row["tau_priority"] = kendall_tau_b(pos, neg_fee)
            row["p_priority"] = perm_pvalue(pos, neg_fee) if row["fees"] == "mixed" else float("nan")
            for i, j in combinations(range(len(own)), 2):
                a, b = own.iloc[i], own.iloc[j]  # a is placed before b
                pairs.append({
                    "session": w.session, "window_id": int(w.window_id), "leader": w.leader,
                    "client": w.client_id, "path": w.arm_path, "contention": w.arm_contention,
                    "spacing_ms": int(w.arm_spacing_ms), "fees": w.arm_fees, "phase": w.arm_phase,
                    "provider": _provider(w),
                    "same_block": bool(a.slot == b.slot),
                    # +1 if the first-placed one was sent first / pays more, -1 if the opposite
                    "arrival_sign": int(np.sign(b.send_rank - a.send_rank)),
                    "fee_sign": int(np.sign(a.fee_level - b.fee_level)),
                    "gap_ms": abs(int(a.sent_at_unix_ns) - int(b.sent_at_unix_ns)) / 1e6,
                })
        out.append(row)
    return pd.DataFrame(out), pd.DataFrame(pairs)


# ---------------------------------------------------------------- pooling

def cluster_share(sub: pd.DataFrame, hit: pd.Series, n_boot: int = 4000) -> tuple[float, float, float]:
    """Share of `hit` with a 95% CI from resampling whole windows.

    Pairs from one window are not independent (12 transactions give 66 pairs), so a plain
    binomial interval would be far too narrow. Resampling windows keeps that dependence.
    """
    if len(sub) == 0:
        return float("nan"), float("nan"), float("nan")
    keys = sub.session.astype(str) + "/" + sub.window_id.astype(str)
    per = pd.DataFrame({"k": hit.astype(int).to_numpy(), "w": keys.to_numpy()}).groupby("w").k.agg(["sum", "count"])
    k, n = per["sum"].to_numpy(dtype=float), per["count"].to_numpy(dtype=float)
    share = k.sum() / n.sum()
    if len(per) < 2:
        lo, hi = wilson(int(k.sum()), int(n.sum()))
        return share, lo, hi
    idx = RNG.integers(0, len(per), size=(n_boot, len(per)))
    boots = k[idx].sum(axis=1) / np.maximum(n[idx].sum(axis=1), 1)
    return share, float(np.quantile(boots, 0.025)), float(np.quantile(boots, 0.975))


def pair_summary(p: pd.DataFrame) -> dict:
    """Share of pairs ordered by arrival and by fee, using only pairs where the rule decides."""
    res = {"pairs": len(p), "pair_windows": int((p.session.astype(str) + "/" + p.window_id.astype(str)).nunique())}
    arr = p[p.arrival_sign != 0]
    fee = p[p.fee_sign != 0]
    res["arrival_agree"], res["arrival_agree_lo"], res["arrival_agree_hi"] = cluster_share(arr, arr.arrival_sign > 0)
    res["fee_agree"], res["fee_agree_lo"], res["fee_agree_hi"] = cluster_share(fee, fee.fee_sign > 0)
    res["fee_pairs"] = len(fee)
    # Conflict pairs: arrival and fee point in opposite directions. Who wins?
    conf = p[(p.arrival_sign != 0) & (p.fee_sign != 0) & (p.arrival_sign != p.fee_sign)]
    res["conflict_pairs"] = len(conf)
    res["fee_wins_conflicts"], res["fee_wins_lo"], res["fee_wins_hi"] = cluster_share(conf, conf.fee_sign > 0)
    # Pairwise logistic model on symmetrised pairs: P(first placed) ~ arrival + fee
    if len(p) >= 10:
        X = np.vstack([p.arrival_sign, p.fee_sign]).T.astype(float)
        X = np.vstack([X, -X])
        y = np.concatenate([np.ones(len(p)), np.zeros(len(p))])
        beta, se = logistic_fit(X, y)
        res["beta_arrival"], res["beta_fee"] = beta
        res["se_arrival"], res["se_fee"] = se
    return res


MIN_VERDICT_WINDOWS = 4


def classify(r: dict) -> str:
    """Label from same-block pairs. Thresholds are stated in the README."""
    fee_lo, arr_lo = r.get("fee_agree_lo", float("nan")), r.get("arrival_agree_lo", float("nan"))
    fee_hi, arr_hi = r.get("fee_agree_hi", float("nan")), r.get("arrival_agree_hi", float("nan"))
    fee_sig = not math.isnan(fee_lo) and fee_lo > 0.5
    arr_sig = not math.isnan(arr_lo) and arr_lo > 0.5
    # Window-level resampling needs enough windows; with two or three, one tiny window drawn
    # three times decides the interval.
    if r.get("pairs", 0) < 20 or r.get("pair_windows", 0) < MIN_VERDICT_WINDOWS:
        return "insufficient data"
    if fee_sig and (not arr_sig or r.get("fee_wins_lo", 0) > 0.5):
        strict = r["fee_agree"]
        return f"priority ({'strict' if strict >= 0.95 else 'loose'})"
    if arr_sig and (not fee_sig or r.get("fee_wins_hi", 1) < 0.5):
        strict = r["arrival_agree"]
        return f"arrival ({'strict' if strict >= 0.95 else 'loose'})"
    if fee_sig and arr_sig:
        return "mixed (both matter)"
    if not math.isnan(fee_hi) and not math.isnan(arr_hi) and fee_hi < 0.5 and arr_hi < 0.5:
        return "reversed/other"
    return "no detectable rule"


def pooled(pairs: pd.DataFrame, wins: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    rows = []
    for key, g in pairs[pairs.same_block].groupby(by):
        key = key if isinstance(key, tuple) else (key,)
        r = dict(zip(by, key))
        r.update(pair_summary(g))
        wsub = wins
        for k, v in zip(by, key):
            if k in wsub.columns:
                wsub = wsub[wsub[k] == v]
        r["windows"] = len(wsub)
        r["validators"] = wsub.leader.nunique() if "leader" in wsub else float("nan")
        lo, hi = bootstrap_ci(wsub.tau_priority.to_numpy(dtype=float)) if "tau_priority" in wsub else (np.nan, np.nan)
        r["tau_priority_mean"] = float(np.nanmean(wsub.tau_priority)) if "tau_priority" in wsub and wsub.tau_priority.notna().any() else float("nan")
        r["tau_priority_lo"], r["tau_priority_hi"] = lo, hi
        lo, hi = bootstrap_ci(wsub.tau_arrival.to_numpy(dtype=float)) if "tau_arrival" in wsub else (np.nan, np.nan)
        r["tau_arrival_mean"] = float(np.nanmean(wsub.tau_arrival)) if "tau_arrival" in wsub and wsub.tau_arrival.notna().any() else float("nan")
        r["tau_arrival_lo"], r["tau_arrival_hi"] = lo, hi
        r["verdict"] = classify(r)
        rows.append(r)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- figures

INK, MUTED, GRID = "#0b0b0b", "#898781", "#e1e0d9"
BLUE, ORANGE, YELLOW, NEUTRAL = "#2a78d6", "#eb6834", "#eda100", "#c3c2b7"


def _style(ax) -> None:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)
    ax.tick_params(colors=INK, labelsize=9)
    ax.grid(axis="x", color=GRID, lw=0.6)
    ax.set_axisbelow(True)


def figures(pairs: pd.DataFrame, by_client: pd.DataFrame, out: Path, wins: pd.DataFrame | None = None) -> None:
    """Static figures for the report: a rule map per client and the landing by path."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if by_client.empty:
        return
    bc = by_client.sort_values("arrival_agree").reset_index(drop=True)
    fig, axes = plt.subplots(1, 2, figsize=(10, 0.42 * len(bc) + 1.8), sharey=True)
    for ax, (col, title, colr) in zip(axes, [("arrival_agree", "ordered by send order (arrival)", BLUE),
                                             ("fee_agree", "ordered by fee (priority)", ORANGE)]):
        for i, r in bc.iterrows():
            v, lo, hi = r[col], r[f"{col}_lo"], r[f"{col}_hi"]
            if pd.isna(v):
                continue
            ok = r.verdict != "insufficient data"
            c = colr if ok else NEUTRAL
            if not (pd.isna(lo) or pd.isna(hi)):
                ax.plot([lo, hi], [i, i], color=c, lw=2, solid_capstyle="round")
            ax.plot(v, i, "o", ms=8, color=c, mec="white", mew=2)
        ax.axvline(0.5, color=MUTED, lw=0.8, ls="--")
        ax.set_xlim(0, 1)
        ax.set_title(f"share of same-block pairs {title}", fontsize=10, color=INK)
        _style(ax)
    axes[0].set_yticks(range(len(bc)))
    axes[0].set_yticklabels([f"{c} ({w} windows)" for c, w in zip(bc.client, bc.windows)], fontsize=9)
    fig.suptitle("Which rule orders our transactions? Dots: share; bars: 95% CI from resampling windows. "
                 "Grey: fewer than 4 windows.", fontsize=9, color=INK)
    fig.tight_layout()
    fig.savefig(out / "rule_map_by_client.png", dpi=150)
    plt.close(fig)

    if wins is None or wins.empty:
        return
    g = wins.groupby(["path", "phase"])[["n_sent", "n_landed_leader", "n_landed_other_leader", "n_dropped"]].sum()
    g = g.reindex([("rpc", "pre"), ("rpc", "inside"), ("tpu", "pre"), ("tpu", "inside")]).dropna()
    if g.empty:
        return
    fig, ax = plt.subplots(figsize=(8, 0.6 * len(g) + 1.6))
    labels = [f"{'RPC' if p == 'rpc' else 'direct QUIC'}, {'before' if ph == 'pre' else 'inside'} the window"
              for p, ph in g.index]
    left = np.zeros(len(g))
    for col, name, colr in [("n_landed_leader", "landed in the scheduled leader's block", BLUE),
                            ("n_landed_other_leader", "landed in another leader's block", YELLOW),
                            ("n_dropped", "never landed", NEUTRAL)]:
        share = (g[col] / g.n_sent).to_numpy()
        ax.barh(range(len(g)), share, left=left, color=colr, edgecolor="white", linewidth=2, height=0.6, label=name)
        for i, (l, w) in enumerate(zip(left, share)):
            if w >= 0.08:
                ax.text(l + w / 2, i, f"{w:.0%}", ha="center", va="center", fontsize=8,
                        color="#ffffff" if colr == BLUE else INK)
        left += share
    ax.set_yticks(range(len(g)))
    ax.set_yticklabels(labels, fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(0, 1)
    ax.set_xlabel("share of our transactions", fontsize=9, color=INK)
    ax.set_title("Where our transactions ended up, by path and timing", fontsize=10, color=INK)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.25), ncol=3, fontsize=8, frameon=False)
    _style(ax)
    fig.tight_layout()
    fig.savefig(out / "landing_by_path.png", dpi=150)
    plt.close(fig)


# ---------------------------------------------------------------- main

def _window_counts(sub: pd.DataFrame, hit: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    keys = sub.session.astype(str) + "/" + sub.window_id.astype(str)
    per = pd.DataFrame({"k": hit.astype(int).to_numpy(), "w": keys.to_numpy()}).groupby("w").k.agg(["sum", "count"])
    return per["sum"].to_numpy(dtype=float), per["count"].to_numpy(dtype=float)


def phase_effect_table(pairs: pd.DataFrame, n_boot: int = 4000) -> pd.DataFrame:
    """Conflict pairs (send order and fee disagree) won by the fee, before vs inside the window.

    The difference pre minus inside gets a 95% CI by resampling windows within each phase;
    `p_le_0` is the share of resampled differences at or below zero (one-sided).
    """
    sb = pairs[pairs.same_block & (pairs.arrival_sign != 0) & (pairs.fee_sign != 0)
               & (pairs.arrival_sign != pairs.fee_sign)]
    rows = []
    groups = [(c, sb[sb.client == c]) for c in sorted(sb.client.unique())] + [("all clients", sb)]
    for client, g in groups:
        r = {"client": client}
        boots = {}
        for ph in ("pre", "inside"):
            sub = g[g.phase == ph]
            r[f"{ph}_conflicts"] = len(sub)
            r[f"{ph}_windows"] = int((sub.session.astype(str) + "/" + sub.window_id.astype(str)).nunique())
            if len(sub) == 0:
                r[f"fee_wins_{ph}"] = float("nan")
                continue
            k, n = _window_counts(sub, sub.fee_sign > 0)
            r[f"fee_wins_{ph}"] = k.sum() / n.sum()
            idx = RNG.integers(0, len(k), size=(n_boot, len(k)))
            boots[ph] = k[idx].sum(axis=1) / np.maximum(n[idx].sum(axis=1), 1)
            r[f"fee_wins_{ph}_lo"] = float(np.quantile(boots[ph], 0.025))
            r[f"fee_wins_{ph}_hi"] = float(np.quantile(boots[ph], 0.975))
        if len(boots) == 2 and min(r["pre_windows"], r["inside_windows"]) >= MIN_VERDICT_WINDOWS:
            d = boots["pre"] - boots["inside"]
            r["diff"] = r["fee_wins_pre"] - r["fee_wins_inside"]
            r["diff_lo"], r["diff_hi"] = float(np.quantile(d, 0.025)), float(np.quantile(d, 0.975))
            r["p_le_0"] = float((d <= 0).mean())
        rows.append(r)
    return pd.DataFrame(rows)


def phase_figure(pe: pd.DataFrame, out: Path) -> None:
    """Dumbbell per client: conflict pairs won by the fee, before vs inside the window."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if pe.empty or "fee_wins_pre" not in pe or "fee_wins_inside" not in pe:
        return
    pe = pe[pe.fee_wins_pre.notna() & pe.fee_wins_inside.notna()].reset_index(drop=True)
    if pe.empty:
        return
    order = list(pe[pe.client != "all clients"].sort_values("fee_wins_pre").client) + ["all clients"]
    pe = pe.set_index("client").loc[[c for c in order if c in pe.client.values] if False else
                                     [c for c in order if c in set(pe.client)]].reset_index()
    fig, ax = plt.subplots(figsize=(10, 0.5 * len(pe) + 1.9))
    for i, r in pe.iterrows():
        enough = min(r.pre_windows, r.inside_windows) >= MIN_VERDICT_WINDOWS
        ax.plot([r.fee_wins_inside, r.fee_wins_pre], [i, i], color=GRID, lw=3, zorder=1, solid_capstyle="round")
        for ph, colr, mk in (("inside", BLUE, "o"), ("pre", ORANGE, "s")):
            v = r[f"fee_wins_{ph}"]
            ax.plot(v, i, mk, ms=9, color=colr if enough else "white", mec=colr, mew=2, zorder=3)
        note = (f"+{r['diff']:.2f} (95% CI {r.diff_lo:+.2f} … {r.diff_hi:+.2f})" if pd.notna(r.get("diff"))
                else "fewer than 4 windows in a phase")
        ax.text(1.02, i, note, va="center", fontsize=8, color=INK if pd.notna(r.get("diff")) else MUTED,
                transform=ax.get_yaxis_transform())
    ax.set_yticks(range(len(pe)))
    ax.set_yticklabels([("all clients" if c == "all clients" else c) for c in pe.client], fontsize=9)
    for lab in ax.get_yticklabels():
        if lab.get_text() == "all clients":
            lab.set_fontweight("bold")
    ax.axvline(0.5, color=MUTED, lw=0.8, ls="--")
    ax.set_xlim(0, 1)
    ax.set_xlabel("conflict pairs won by the higher fee (send order said the opposite)", fontsize=9, color=INK)
    ax.set_title("When the batch waits for the leader, the fee decides more often", fontsize=11, color=INK)
    from matplotlib.lines import Line2D
    handles = [Line2D([], [], ls="", marker="o", ms=8, color=BLUE, label="arrives inside the window"),
               Line2D([], [], ls="", marker="s", ms=8, color=ORANGE, label="arrives before the window"),
               Line2D([], [], ls="", marker="o", ms=8, color="white", mec=MUTED, mew=2,
                      label="hollow: fewer than 4 windows in a phase")]
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.62, -0.16), ncol=3, fontsize=8, frameon=False)
    _style(ax)
    fig.tight_layout(rect=(0, 0, 0.74, 1))
    fig.savefig(out / "phase_effect.png", dpi=150)
    plt.close(fig)


def provider_table(wins: pd.DataFrame, pairs: pd.DataFrame) -> pd.DataFrame:
    """Landing and ordering per send path / RPC provider."""
    rows = []
    for prov, g in wins.groupby("provider"):
        r = {"provider": prov, "windows": len(g), "sent": int(g.n_sent.sum()),
             "landed_in_leader": round(g.n_landed_leader.sum() / max(g.n_sent.sum(), 1), 2),
             "landed_other_leader": round(g.n_landed_other_leader.sum() / max(g.n_sent.sum(), 1), 2),
             "never_landed": round(g.n_dropped.sum() / max(g.n_sent.sum(), 1), 2),
             "land_delay": round(float(g.land_delay.median()), 2)}
        pp = pairs[pairs.provider == prov] if "provider" in pairs else pairs.iloc[0:0]
        sb = pp[pp.same_block]
        if len(sb):
            summ = pair_summary(sb)
            r["pairs"] = len(sb)
            r["arrival_agree"] = round(float(summ["arrival_agree"]), 2)
            r["fee_agree"] = round(float(summ["fee_agree"]), 2) if pd.notna(summ["fee_agree"]) else float("nan")
            pe = phase_effect_table(pp)
            a = pe[pe.client == "all clients"]
            if len(a) and "diff" in a and pd.notna(a.iloc[0].get("diff")):
                r["phase_diff"] = round(float(a.iloc[0]["diff"]), 2)
                r["phase_diff_ci"] = f"{a.iloc[0].diff_lo:+.2f} … {a.iloc[0].diff_hi:+.2f}"
        rows.append(r)
    return pd.DataFrame(rows)


def timing_table(wins: pd.DataFrame, skipped: pd.DataFrame) -> pd.DataFrame:
    """How well the probe hit the windows, per client and path. Used to calibrate the clock."""
    rows = []
    for (client, path, phase), g in wins.groupby(["client", "path", "phase"]):
        n_sk = 0
        if not skipped.empty:
            ph = skipped["phase"] if "phase" in skipped.columns else pd.Series("pre", index=skipped.index)
            n_sk = int(((skipped.client_id == client) & (skipped.path == path) & (ph.fillna("pre") == phase)).sum())
        rows.append({
            "client": client, "path": path, "phase": phase, "windows": len(g), "skipped": n_sk,
            "fire_offset": round(float(g.fire_offset.median()), 2),
            "land_delay": round(float(g.land_delay.median()), 2),
            "landed_in_leader": round(float(g.n_landed_leader.sum() / max(g.n_sent.sum(), 1)), 2),
            "landed_other_leader": round(float(g.n_landed_other_leader.sum() / max(g.n_sent.sum(), 1)), 2),
        })
    return pd.DataFrame(rows)


def fmt(x, nd=2):
    return "" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{nd}f}"


def markdown_table(df: pd.DataFrame, cols: list[str]) -> str:
    head = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
    body = ""
    for _, r in df.iterrows():
        body += "| " + " | ".join(fmt(r[c]) if isinstance(r[c], float) else str(r[c]) for c in cols) + " |\n"
    return head + body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=Path("data"), help="folder with session folders")
    ap.add_argument("--out", type=Path, default=Path("results"))
    ap.add_argument("--sessions", nargs="*", help="only these session ids")
    args = ap.parse_args()

    paths = sorted(p for p in args.data.iterdir() if p.is_dir())
    if args.sessions:
        paths = [p for p in paths if p.name in args.sessions]
    sends, windows, blocks, skipped = load_sessions(paths)
    if sends.empty:
        print("no live, collected sessions found", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)

    wins, pairs = window_rows(sends, windows)
    wins.to_csv(args.out / "windows.csv", index=False)
    pairs.to_csv(args.out / "pairs.csv", index=False)

    by_client = pooled(pairs, wins, ["client"])
    by_validator = pooled(pairs, wins, ["client", "leader"])
    by_arm = pooled(pairs, wins, ["client", "contention", "spacing_ms"])
    by_path = pooled(pairs, wins, ["client", "path"])
    by_phase = pooled(pairs, wins, ["client", "phase"])
    for name, df in [("by_client", by_client), ("by_validator", by_validator),
                     ("by_client_arm", by_arm), ("by_client_path", by_path), ("by_client_phase", by_phase)]:
        df.to_csv(args.out / f"{name}.csv", index=False)

    landing = wins.groupby("client")[["n_sent", "n_landed_leader", "n_landed_other_leader", "n_dropped"]].sum()
    landing.to_csv(args.out / "landing_by_client.csv")
    timing = timing_table(wins, skipped)
    prov = provider_table(wins, pairs)
    prov.to_csv(args.out / "by_provider.csv", index=False)
    timing.to_csv(args.out / "timing_by_client_path.csv", index=False)
    if not blocks.empty:
        b = blocks[blocks.scheduled_leader.astype(str) != ""]
        mism = b[(~b.skipped) & (b.block_producer.notna()) & (b.block_producer != b.scheduled_leader)]
        b_summary = {"window_blocks": len(b), "skipped": int(b.skipped.sum()),
                     "producer_differs_from_schedule": len(mism)}
        (args.out / "blocks_summary.json").write_text(json.dumps(b_summary, indent=2))

    figures(pairs, by_client, args.out, wins)
    pe = phase_effect_table(pairs)
    pe.to_csv(args.out / "phase_effect.csv", index=False)
    phase_figure(pe, args.out)
    # by client and version string (computed last so the numbers above do not change)
    pv = pairs.merge(wins[["session", "window_id", "version"]], on=["session", "window_id"], how="left")
    by_version = pooled(pv, wins, ["client", "version"])
    by_version.to_csv(args.out / "by_client_version.csv", index=False)

    cols = ["client", "validators", "windows", "pairs", "fee_agree", "fee_agree_lo", "fee_agree_hi",
            "arrival_agree", "arrival_agree_lo", "arrival_agree_hi", "conflict_pairs", "fee_wins_conflicts",
            "verdict"]
    md = "# Results (generated)\n\nSame-block pairs of our own transactions in the scheduled leader's blocks.\n\n"
    md += "## By client\n\n" + markdown_table(by_client, [c for c in cols if c in by_client.columns])
    vcols = ["client", "leader", "windows", "pairs", "fee_agree", "arrival_agree", "fee_wins_conflicts", "verdict"]
    md += "\n## By validator\n\n" + markdown_table(by_validator, [c for c in vcols if c in by_validator.columns])
    acols = ["client", "contention", "spacing_ms", "pairs", "fee_agree", "arrival_agree", "verdict"]
    md += "\n## By client and version\n\n" + markdown_table(by_version, [c for c in vcols if c != "leader"] [:1] + ["version"] + [c for c in vcols if c not in ("client", "leader")])
    md += "\n## By client and arm\n\n" + markdown_table(by_arm, [c for c in acols if c in by_arm.columns])
    pcols = ["client", "phase", "pairs", "fee_agree", "arrival_agree", "fee_wins_conflicts", "verdict"]
    md += "\n## By client and phase\n\n" + markdown_table(by_phase, [c for c in pcols if c in by_phase.columns])
    pecols = ["client", "pre_conflicts", "pre_windows", "fee_wins_pre", "fee_wins_pre_lo", "fee_wins_pre_hi",
              "inside_conflicts", "inside_windows", "fee_wins_inside", "fee_wins_inside_lo", "fee_wins_inside_hi",
              "diff", "diff_lo", "diff_hi", "p_le_0"]
    md += ("\n## Phase effect\n\nConflict pairs (send order and fee disagree) won by the fee, when the batch "
           "arrives before the window vs inside it. diff = pre minus inside, 95% CI from resampling windows.\n\n"
           + markdown_table(pe, [c for c in pecols if c in pe.columns]))
    md += "\n## Landing\n\n" + landing.to_markdown() + "\n"
    if not prov.empty:
        md += ("\n## By send path and provider\n\nShares of sent transactions, and the phase effect pooled "
               "over all clients for each provider.\n\n" + prov.to_markdown(index=False) + "\n")
    md += ("\n## Timing\n\nfire_offset: probe clock at the first send, slots from the window start. "
           "land_delay: landed slot minus probe clock at send (median). skipped: windows planned but not "
           "sent because the clock was already past the firing point.\n\n" + timing.to_markdown(index=False) + "\n")
    (args.out / "results.md").write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
