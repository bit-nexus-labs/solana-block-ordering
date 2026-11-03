#!/usr/bin/env python3
"""Whole-block view: how the same leaders ordered *all* transactions in their blocks.

The probe (`ordprobe run`) controls send time and fee of its own transactions. This script
looks at everybody else's transactions in the scheduled leader's blocks of the same windows,
so the two views can be compared on the same leaders at the same moments.

    python analysis/blocks.py fetch   --data data --per-window 2 --rps 2   # reads only, no SOL
    python analysis/blocks.py analyze --data data --out results-blocks

`fetch` needs RPC_URL (an RPC that serves getBlock). The URL is never printed. Output is one
compressed file per session, `data/<session>/fullblocks.jsonl.gz`, one line per block with one
compact row per transaction. Interrupted fetches resume where they stopped.

Per transaction we keep: position in the block, vote or not, signature count, fee, compute
units consumed, compute-unit limit and price requested (ComputeBudget instructions), Jito tip
paid (lamports moved to a Jito tip account), failed or not.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

VOTE_PROGRAM = "Vote111111111111111111111111111111111111111"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
JITO_TIP_ACCOUNTS = {
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
}
BASE_FEE = 5000
ROW_FIELDS = ["idx", "vote", "sigs", "fee", "cu", "cu_limit", "cu_price", "tip", "err"]
OUT_NAME = "fullblocks.jsonl.gz"

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_IDX = {c: i for i, c in enumerate(_B58)}


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58_IDX[c]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + body


# ---------------------------------------------------------------- parsing

def compact_tx(idx: int, tx: dict) -> list:
    """One getBlock(json, full) transaction -> compact row (see ROW_FIELDS)."""
    msg = tx["transaction"]["message"]
    meta = tx.get("meta") or {}
    keys = list(msg["accountKeys"])
    loaded = meta.get("loadedAddresses") or {}
    keys += loaded.get("writable", []) + loaded.get("readonly", [])
    cu_limit = cu_price = None
    vote = 0
    for ins in msg.get("instructions", []):
        prog = keys[ins["programIdIndex"]] if ins["programIdIndex"] < len(keys) else ""
        if prog == VOTE_PROGRAM:
            vote = 1
        elif prog == COMPUTE_BUDGET:
            try:
                data = b58decode(ins.get("data", ""))
            except KeyError:
                continue
            if len(data) >= 9 and data[0] == 3:
                cu_price = int.from_bytes(data[1:9], "little")
            elif len(data) >= 5 and data[0] == 2:
                cu_limit = int.from_bytes(data[1:5], "little")
    tip = 0
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    for i, k in enumerate(keys):
        if k in JITO_TIP_ACCOUNTS and i < len(pre) and i < len(post):
            tip += max(0, post[i] - pre[i])
    sigs = len(tx["transaction"].get("signatures", []))
    return [idx, vote, sigs, meta.get("fee"), meta.get("computeUnitsConsumed"),
            cu_limit, cu_price, tip, 0 if meta.get("err") is None else 1]


def compact_block(block: dict) -> list[list]:
    return [compact_tx(i, tx) for i, tx in enumerate(block.get("transactions") or [])]


# ---------------------------------------------------------------- choosing blocks

def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def session_dirs(data: Path, sessions: list[str] | None) -> list[Path]:
    out = []
    for p in sorted(data.iterdir()):
        if not p.is_dir() or not (p / "windows.jsonl").exists():
            continue
        if sessions and p.name not in sessions:
            continue
        sends = read_jsonl(p / "sends.jsonl")
        if not sends or all(r.get("dry_run") for r in sends):
            continue
        out.append(p)
    return out


def pick_blocks(sess: Path, per_window: int, seed: int) -> list[dict]:
    """Up to `per_window` produced blocks of the scheduled leader per window.

    Blocks holding our own landed transactions come first (they link the two views);
    the rest are drawn at random with a fixed seed."""
    windows = {w["window_id"]: w for w in read_jsonl(sess / "windows.jsonl")}
    ours = {r["slot"] for r in read_jsonl(sess / "landed.jsonl") if r.get("landed") and r.get("slot")}
    by_window: dict[int, list[dict]] = {}
    for b in read_jsonl(sess / "blocks.jsonl"):
        w = windows.get(b["window_id"])
        if not w or b.get("skipped") or b.get("block_producer") != w["leader"]:
            continue
        by_window.setdefault(b["window_id"], []).append(b)
    rng = random.Random(seed)
    picked = []
    for wid in sorted(by_window):
        w = windows[wid]
        cands = sorted(by_window[wid], key=lambda b: b["slot"])
        rng.shuffle(cands)
        cands.sort(key=lambda b: b["slot"] not in ours)  # stable: ours first, rest shuffled
        for b in cands[:per_window]:
            picked.append({"session": sess.name, "window_id": wid, "slot": b["slot"],
                           "leader": w["leader"], "client": w.get("client_id") or "Unknown",
                           "has_ours": b["slot"] in ours})
    return picked


# ---------------------------------------------------------------- fetching

def rpc_call(url: str, method: str, params: list, timeout: float = 60.0) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "Accept-Encoding": "gzip",
        "User-Agent": "ordering-probe-blocks/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return json.loads(raw)


def get_block(url: str, slot: int, attempts: int = 6) -> dict | None:
    params_v = lambda v: [slot, {"encoding": "json", "transactionDetails": "full", "rewards": False,
                                 "maxSupportedTransactionVersion": v, "commitment": "finalized"}]
    version, delay = 1, 1.0
    for _ in range(attempts):
        try:
            resp = rpc_call(url, "getBlock", params_v(version))
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(delay); delay = min(delay * 2, 20); continue
            raise SystemExit(f"getBlock {slot}: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(delay); delay = min(delay * 2, 20); continue
        err = resp.get("error")
        if not err:
            return resp.get("result")
        msg = str(err.get("message", ""))
        if version == 1 and (err.get("code") == -32602 or "maxSupportedTransactionVersion" in msg):
            version = 0; continue
        if err.get("code") in (-32007, -32009):  # slot skipped / missing in long-term storage
            return None
        time.sleep(delay); delay = min(delay * 2, 20)
    return None


def cmd_fetch(a) -> int:
    url = os.environ.get("RPC_URL")
    if not url:
        print("environment variable RPC_URL is not set", file=sys.stderr)
        return 2
    total_new = 0
    for sess in session_dirs(Path(a.data), a.sessions):
        out = sess / OUT_NAME
        done = set()
        if out.exists():
            with gzip.open(out, "rt") as f:
                done = {json.loads(l)["slot"] for l in f if l.strip()}
        todo = [b for b in pick_blocks(sess, a.per_window, a.seed) if b["slot"] not in done]
        print(f"{sess.name}: {len(done)} blocks already, {len(todo)} to fetch")
        with gzip.open(out, "at") as f:
            for i, b in enumerate(todo, 1):
                t0 = time.monotonic()
                block = get_block(url, b["slot"])
                if block is None:
                    print(f"  slot {b['slot']}: not available, skipped")
                    continue
                rows = compact_block(block)
                f.write(json.dumps({**b, "block_time": block.get("blockTime"), "rows": rows},
                                   separators=(",", ":")) + "\n")
                f.flush()
                total_new += 1
                if i % 25 == 0 or i == len(todo):
                    print(f"  {i}/{len(todo)} (slot {b['slot']}, {len(rows)} tx)")
                time.sleep(max(0.0, 1.0 / a.rps - (time.monotonic() - t0)))
    print(f"fetched {total_new} blocks")
    return 0


# ---------------------------------------------------------------- analysis

TOP_PRICE = 100_000  # micro-lamports per CU; roughly the top tenth of paying transactions
PRICE_BINS = [-1, 0, 1e3, 1e4, 1e5, 1e6, 1e18]
PRICE_LABELS = ["0", "1–1k", "1k–10k", "10k–100k", "100k–1M", "≥1M"]
NEAR = 10  # "nearby" = at most this many positions apart


def load_blocks(data: Path, sessions: list[str] | None):
    import pandas as pd
    blocks, txs = [], []
    for sess in session_dirs(data, sessions):
        p = sess / OUT_NAME
        if not p.exists():
            continue
        with gzip.open(p, "rt") as f:
            for line in f:
                if not line.strip():
                    continue
                b = json.loads(line)
                rows = b.pop("rows")
                blocks.append({**b, "n_tx": len(rows)})
                for r in rows:
                    txs.append([b["session"], b["slot"], b["client"], b["leader"]] + r)
    tx = pd.DataFrame(txs, columns=["session", "slot", "client", "leader"] + ROW_FIELDS)
    if not tx.empty:
        last = tx.groupby(["session", "slot"]).idx.transform("max").clip(lower=1)
        tx["rel"] = tx.idx / last  # 0 = first in the block, 1 = last
        tx["price"] = tx.cu_price.fillna(0)
        tx["top"] = tx.price >= TOP_PRICE
        tx["bucket"] = pd.cut(tx.price, PRICE_BINS, labels=PRICE_LABELS)
    return pd.DataFrame(blocks), tx


def higher_first_share(pos, price):
    """Share of pairs with different prices where the higher price sits earlier."""
    import numpy as np
    pos, price = np.asarray(pos), np.asarray(price, dtype=float)
    if len(pos) < 2:
        return float("nan"), 0
    order = np.argsort(pos, kind="stable")
    p = price[order]
    dp = np.sign(p[:, None] - p[None, :])
    iu = np.triu_indices(len(p), 1)
    d = dp[iu]
    m = d != 0
    n = int(m.sum())
    return (float((d[m] > 0).mean()) if n else float("nan")), n


def near_share(price, k: int = NEAR):
    """Same as higher_first_share, only for pairs at most k positions apart."""
    import numpy as np
    p = np.asarray(price, dtype=float)
    hit = tot = 0
    for d in range(1, min(k, len(p) - 1) + 1):
        a, b = p[:-d], p[d:]
        m = a != b
        hit += int((a[m] > b[m]).sum())
        tot += int(m.sum())
    return (hit / tot if tot else float("nan")), tot


def block_metrics(tx):
    """One row per block."""
    import numpy as np
    import pandas as pd
    out = []
    for (sess, slot), g in tx.groupby(["session", "slot"], sort=False):
        g = g.sort_values("idx")
        price = g.price.to_numpy()
        hf, npairs = higher_first_share(g.idx.to_numpy(), price)
        nf, nnear = near_share(price)
        top, rest = g[g.top], g[~g.top]
        tipped = g[g.tip > 0]
        out.append({
            "session": sess, "slot": slot, "client": g.client.iat[0], "leader": g.leader.iat[0],
            "n_tx": len(g), "paying_share": float((price > 0).mean()),
            "top_share": float(g.top.mean()),
            "higher_fee_first": hf, "fee_pairs": npairs,
            "higher_fee_first_near": nf, "near_pairs": nnear,
            "top_pos": float(top.rel.mean()) if len(top) else float("nan"),
            "rest_pos": float(rest.rel.mean()) if len(rest) else float("nan"),
            "top_shift": float(top.rel.mean() - rest.rel.mean()) if len(top) and len(rest) else float("nan"),
            "tipped_share": float(len(tipped) / len(g)),
            "tipped_pos": float(tipped.rel.mean()) if len(tipped) else float("nan"),
            "failed_share": float(g.err.mean()),
        })
    return pd.DataFrame(out)


def cluster_ci(df, col, unit="leader", n_boot=2000, seed=20261103):
    """Mean over blocks with a 95% interval from resampling whole validators."""
    import numpy as np
    d = df.dropna(subset=[col])
    if d.empty:
        return float("nan"), float("nan"), float("nan")
    groups = [g[col].to_numpy() for _, g in d.groupby(unit)]
    est = float(d[col].mean())
    if len(groups) < 3:
        return est, float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    sums = np.array([g.sum() for g in groups]); cnts = np.array([len(g) for g in groups])
    idx = rng.integers(0, len(groups), size=(n_boot, len(groups)))
    boots = sums[idx].sum(axis=1) / cnts[idx].sum(axis=1)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return est, float(lo), float(hi)


def fmt_ci(est, lo, hi, signed=False):
    import math
    if est is None or math.isnan(est):
        return ""
    f = "{:+.3f}" if signed else "{:.2f}"
    if math.isnan(lo):
        return f.format(est)
    return f"{f.format(est)} ({f.format(lo)} … {f.format(hi)})"


def client_table(bm, probe_fee=None):
    import pandas as pd
    rows = []
    for client, g in bm.groupby("client"):
        sh = cluster_ci(g, "top_shift")
        nr = cluster_ci(g, "higher_fee_first_near")
        rows.append({
            "client": client, "validators": g.leader.nunique(), "blocks": len(g),
            "median_tx": int(g.n_tx.median()),
            "paying_share": round(float(g.paying_share.mean()), 2),
            "failed_share": round(float(g.failed_share.mean()), 2),
            "top_fee_position": round(float(g.top_pos.mean()), 3),
            "top_fee_shift": fmt_ci(*sh, signed=True),
            "higher_fee_first_all": round(float(g.higher_fee_first.mean()), 3),
            "higher_fee_first_near": fmt_ci(*nr),
            "jito_tipped_share": round(float(g.tipped_share.mean()), 3),
            "probe_fee_agree": (probe_fee or {}).get(client, float("nan")),
            "_shift": sh, "_near": nr,
        })
    return pd.DataFrame(rows).sort_values("blocks", ascending=False)


def bucket_table(tx):
    import pandas as pd
    g = tx.groupby("bucket", observed=True)
    t = pd.DataFrame({
        "transactions": g.size(),
        "share": (g.size() / len(tx)).round(3),
        "mean_position": g.rel.mean().round(3),
        "failed_share": g.err.mean().round(2),
        "jito_tipped_share": g.tip.apply(lambda x: (x > 0).mean()).round(3),
    })
    t.index.name = "cu_price (micro-lamports)"
    return t


def probe_fee_by_client(data: Path, sessions):
    """Our own probes' fee agreement per client, from analyze.py, for the side-by-side column."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import analyze as A
        sends, windows, _, _ = A.load_sessions(session_dirs(data, sessions))
        if sends.empty:
            return None
        _, pairs = A.window_rows(sends, windows)
        if pairs.empty:
            return None
        p = pairs[pairs.same_block & (pairs.fee_sign != 0)]
        g = (p.fee_sign > 0).groupby(p.client).agg(["mean", "size"])
        return g[g["size"] >= 20]["mean"].round(2).to_dict()  # too few pairs -> no number
    except Exception as e:  # the side column is optional
        print(f"note: probe column skipped ({type(e).__name__}: {e})", file=sys.stderr)
        return None


def _style(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.spines["left"].set_color("#898781"); ax.spines["bottom"].set_color("#898781")
    ax.tick_params(colors="#0b0b0b", labelsize=9)
    ax.set_axisbelow(True)


def figure(t, bt, out: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    d = t[(t.blocks >= 8) & (t.validators >= 3)].copy()
    if d.empty:
        return
    d["s"] = [x[0] for x in d._near]
    d = d.sort_values("s")
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 0.45 * len(d) + 1.8), dpi=150,
                                 gridspec_kw={"width_ratios": [1, 1.25]})
    # left: where transactions sit, by price
    y = np.arange(len(bt))
    cols = ["#c3c2b7"] * (len(bt) - 2) + ["#2a78d6"] * 2
    a1.hlines(y, 0.5, bt.mean_position, color=cols, lw=2.2)
    a1.scatter(bt.mean_position, y, color=cols, s=40, zorder=3)
    a1.axvline(0.5, color="#898781", lw=1, ls="--")
    a1.set_yticks(y, [f"{i}  ({s:.0%})" for i, s in zip(bt.index, bt.share)])
    a1.set_xlim(0.40, 0.58)
    a1.set_xlabel("mean position in the block (0 = first, 1 = last; 0.5 = middle)", fontsize=9)
    a1.set_title("Where do transactions sit, by fee?\ncompute-unit price in micro-lamports (share of all)",
                 loc="left", fontsize=10, color="#0b0b0b")
    a1.grid(axis="x", color="#e1e0d9", lw=0.8)
    _style(a1)
    # right: nearby pairs, whole blocks vs our probes
    y = np.arange(len(d))
    est = np.array([x[0] for x in d._near]); lo = np.array([x[1] for x in d._near]); hi = np.array([x[2] for x in d._near])
    err = np.nan_to_num(np.vstack([np.clip(est - lo, 0, None), np.clip(hi - est, 0, None)]))
    a2.errorbar(est, y, xerr=err, fmt="o", color="#2a78d6", ecolor="#2a78d6", elinewidth=1.4, capsize=0,
                ms=6, label=f"all transactions, neighbours ≤{NEAR} apart")
    if d.probe_fee_agree.notna().any():
        a2.plot(d.probe_fee_agree, y, "D", color="#eb6834", ms=5, label="our probes, sent together")
    a2.axvline(0.5, color="#898781", lw=1, ls="--")
    a2.set_yticks(y, d.client)
    a2.set_xlim(0.4, 0.85)
    a2.set_xlabel("pairs with different fees where the higher fee comes first (0.5 = coin flip)", fontsize=9)
    a2.set_title("Does the higher fee go first?\nwhole blocks vs our probes", loc="left", fontsize=10,
                 color="#0b0b0b")
    a2.grid(axis="x", color="#e1e0d9", lw=0.8)
    a2.legend(frameon=False, fontsize=8, loc="upper center", bbox_to_anchor=(0.45, -0.13), ncol=2)
    _style(a2)
    fig.tight_layout()
    fig.savefig(out / "whole_block_fee_order.png")
    plt.close(fig)


def cmd_analyze(a) -> int:
    data, out = Path(a.data), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    blocks, tx = load_blocks(data, a.sessions)
    if blocks.empty:
        print("no fullblocks files found; run `blocks.py fetch` first", file=sys.stderr)
        return 1
    bm = block_metrics(tx)
    bm.to_csv(out / "whole_blocks.csv", index=False)
    probe = probe_fee_by_client(data, a.sessions)
    t = client_table(bm, probe)
    show = t.drop(columns=[c for c in t.columns if c.startswith("_")])
    show.to_csv(out / "whole_blocks_by_client.csv", index=False)
    bt = bucket_table(tx)
    bt.to_csv(out / "whole_blocks_by_price.csv")
    by_sess = bm.groupby("session").agg(
        blocks=("slot", "size"), median_tx=("n_tx", "median"), paying_share=("paying_share", "mean"),
        failed_share=("failed_share", "mean"), top_fee_shift=("top_shift", "mean"),
        higher_fee_first_near=("higher_fee_first_near", "mean")).round(3)
    figure(t, bt, out)
    sh, nr = cluster_ci(bm, "top_shift"), cluster_ci(bm, "higher_fee_first_near")
    md = [
        "# Whole blocks of the scheduled leaders", "",
        f"{len(bm)} blocks from {bm.leader.nunique()} validators and {bm.client.nunique()} clients, "
        f"{len(tx)} transactions. The RPC's full-block answer did not include vote transactions, so "
        "everything below is about non-vote transactions only.", "",
        "* `top_fee_shift`: mean position (0 = first, 1 = last) of transactions paying at least "
        f"{TOP_PRICE:,} micro-lamports per compute unit, minus that of all other transactions in the "
        "same block. Negative = expensive transactions sit earlier.",
        f"* `higher_fee_first_near`: pairs at most {NEAR} positions apart with different prices; share where "
        "the higher price comes first (0.5 = no relation). `higher_fee_first_all`: the same over all pairs "
        "in the block.",
        "* `probe_fee_agree`: the same share for our own probe transactions, which were sent together "
        "(from `analyze.py`, same-block pairs, clients with at least 20 pairs).",
        "* Intervals: 95%, resampling whole validators.", "",
        f"All clients: top_fee_shift = {fmt_ci(*sh, signed=True)}, "
        f"higher_fee_first_near = {fmt_ci(*nr)}, "
        f"failed transactions {bm.failed_share.mean():.0%}, paying a priority fee {bm.paying_share.mean():.0%}.", "",
        "## By client", "", show.to_markdown(index=False), "",
        "## By compute-unit price", "", bt.to_markdown(), "",
        "## By session", "", by_sess.to_markdown(), "",
    ]
    (out / "WHOLE_BLOCKS.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download the chosen blocks (read only)")
    f.add_argument("--data", default="data")
    f.add_argument("--sessions", nargs="*")
    f.add_argument("--per-window", type=int, default=2)
    f.add_argument("--rps", type=float, default=2.0)
    f.add_argument("--seed", type=int, default=20261103)
    z = sub.add_parser("analyze", help="tables and figure from the fetched blocks")
    z.add_argument("--data", default="data")
    z.add_argument("--out", default="results-blocks")
    z.add_argument("--sessions", nargs="*")
    a = p.parse_args(argv)
    return cmd_fetch(a) if a.cmd == "fetch" else cmd_analyze(a)


if __name__ == "__main__":
    raise SystemExit(main())
