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
    return (pd.DataFrame(blocks),
            pd.DataFrame(txs, columns=["session", "slot", "client", "leader"] + ROW_FIELDS))


def higher_first_share(pos, price):
    """Share of pairs with different prices where the higher price sits earlier."""
    import numpy as np
    pos, price = np.asarray(pos), np.asarray(price, dtype=float)
    if len(pos) < 2:
        return float("nan"), 0
    dp = np.sign(price[:, None] - price[None, :])
    dx = np.sign(pos[None, :] - pos[:, None])  # +1 when i is earlier than j
    iu = np.triu_indices(len(pos), 1)
    dp, dx = dp[iu], dx[iu]
    m = dp != 0
    n = int(m.sum())
    if n == 0:
        return float("nan"), 0
    return float((dp[m] == dx[m]).mean()), n


def block_metrics(txs):
    """One row per block: how the non-vote transactions are ordered."""
    import numpy as np
    import pandas as pd
    out = []
    for (sess, slot), g in txs.groupby(["session", "slot"], sort=False):
        nv = g[g["vote"] == 0]
        n = len(g)
        rel = g["idx"] / max(n - 1, 1)
        price = nv["cu_price"].fillna(0).to_numpy()
        hf, npairs = higher_first_share(nv["idx"].to_numpy(), price)
        tipped = nv[nv["tip"] > 0]
        first_q = nv["idx"] < n * 0.25
        paying = (price > 0).mean() if len(nv) else float("nan")
        # Spearman rank correlation between position and price (negative = expensive first)
        rho = float("nan")
        if len(nv) > 2 and np.ptp(price) > 0:
            rho = float(pd.Series(nv["idx"].to_numpy()).rank().corr(pd.Series(price).rank()))
        out.append({
            "session": sess, "slot": slot, "client": g["client"].iat[0], "leader": g["leader"].iat[0],
            "n_tx": n, "votes": int((g["vote"] == 1).sum()), "non_vote": len(nv),
            "vote_mean_pos": float(rel[g["vote"] == 1].mean()) if (g["vote"] == 1).any() else float("nan"),
            "paying_share": float(paying),
            "higher_fee_first": hf, "fee_pairs": npairs, "spearman_pos_price": rho,
            "tipped_share": float(len(tipped) / len(nv)) if len(nv) else float("nan"),
            "tipped_in_first_quarter": float((tipped["idx"] < n * 0.25).mean()) if len(tipped) else float("nan"),
            "first_quarter_paying": float((price[first_q.to_numpy()] > 0).mean()) if first_q.any() else float("nan"),
            "failed_share": float(nv["err"].mean()) if len(nv) else float("nan"),
        })
    return pd.DataFrame(out)


def cluster_ci(df, col, unit="leader", n_boot=2000, seed=20261103):
    """Mean over blocks with a 95% interval from resampling whole validators."""
    import numpy as np
    d = df.dropna(subset=[col])
    if d.empty:
        return float("nan"), float("nan"), float("nan")
    groups = [g[col].to_numpy() for _, g in d.groupby(unit)]
    rng = np.random.default_rng(seed)
    est = float(d[col].mean())
    if len(groups) < 2:
        return est, float("nan"), float("nan")
    boots = []
    for _ in range(n_boot):
        pick = rng.integers(0, len(groups), len(groups))
        boots.append(np.concatenate([groups[i] for i in pick]).mean())
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return est, float(lo), float(hi)


def fmt_ci(est, lo, hi):
    import math
    if math.isnan(est):
        return ""
    if math.isnan(lo):
        return f"{est:.2f}"
    return f"{est:.2f} ({lo:.2f}–{hi:.2f})"


def client_table(bm, probe_fee=None):
    import pandas as pd
    rows = []
    for client, g in bm.groupby("client"):
        hf = cluster_ci(g, "higher_fee_first")
        row = {
            "client": client, "validators": g["leader"].nunique(), "blocks": len(g),
            "median_tx": int(g["n_tx"].median()),
            "vote_share": round(float((g["votes"] / g["n_tx"]).mean()), 2),
            "paying_share": round(float(g["paying_share"].mean()), 2),
            "higher_fee_first": fmt_ci(*hf),
            "spearman_pos_price": round(float(g["spearman_pos_price"].mean()), 2),
            "tipped_share": round(float(g["tipped_share"].mean()), 3),
            "tips_in_first_quarter": round(float(g["tipped_in_first_quarter"].mean()), 2)
                if g["tipped_in_first_quarter"].notna().any() else None,
            "_hf": hf[0], "_lo": hf[1], "_hi": hf[2],
        }
        if probe_fee is not None and client in probe_fee:
            row["probe_fee_agree"] = probe_fee[client]
        rows.append(row)
    t = pd.DataFrame(rows).sort_values("blocks", ascending=False)
    allrow = cluster_ci(bm, "higher_fee_first")
    return t, allrow


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


def figure(t, allrow, out: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    d = t[(t["blocks"] >= 8) & (t["validators"] >= 3)].sort_values("_hf")
    if d.empty:
        return
    fig, ax = plt.subplots(figsize=(7.2, 0.42 * len(d) + 1.4), dpi=150)
    y = np.arange(len(d))
    lo = np.clip(d["_hf"] - d["_lo"], 0, None).fillna(0)
    hi = np.clip(d["_hi"] - d["_hf"], 0, None).fillna(0)
    ax.errorbar(d["_hf"], y, xerr=[lo, hi], fmt="o", color="#2a78d6", ecolor="#2a78d6",
                elinewidth=1.4, capsize=0, ms=6, label="all transactions in the leader's blocks")
    if "probe_fee_agree" in d:
        ax.plot(d["probe_fee_agree"], y, "D", color="#eb6834", ms=5, label="our probe transactions")
    ax.axvline(0.5, color="#898781", lw=1, ls="--")
    ax.text(0.505, -0.45, "coin flip", color="#898781", fontsize=8, va="center")
    ax.set_yticks(y, d["client"])
    ax.set_xlim(0.3, 1.0)
    ax.set_xlabel("pairs with different fees where the higher fee is placed first")
    ax.set_title("Does the higher fee go first? Whole blocks vs our probes", loc="left",
                 fontsize=11, color="#0b0b0b")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(axis="x", color="#e1e0d9", lw=0.8)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    fig.tight_layout()
    fig.savefig(out / "whole_block_fee_order.png")
    plt.close(fig)


def cmd_analyze(a) -> int:
    import pandas as pd
    data, out = Path(a.data), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    blocks, txs = load_blocks(data, a.sessions)
    if blocks.empty:
        print("no fullblocks files found; run `blocks.py fetch` first", file=sys.stderr)
        return 1
    bm = block_metrics(txs)
    bm.to_csv(out / "whole_blocks.csv", index=False)
    probe = probe_fee_by_client(data, a.sessions)
    t, allrow = client_table(bm, probe)
    show = t.drop(columns=[c for c in t.columns if c.startswith("_")])
    show.to_csv(out / "whole_blocks_by_client.csv", index=False)
    by_sess = bm.groupby("session").agg(blocks=("slot", "size"), median_tx=("n_tx", "median"),
                                        higher_fee_first=("higher_fee_first", "mean"),
                                        paying_share=("paying_share", "mean")).round(2)
    figure(t, allrow, out)
    md = [
        "# Whole blocks of the scheduled leaders", "",
        f"{len(bm)} blocks from {bm['leader'].nunique()} validators and {bm['client'].nunique()} clients, "
        f"{int(bm['n_tx'].sum())} transactions ({int(bm['non_vote'].sum())} non-vote).", "",
        "`higher_fee_first`: among non-vote transactions in one block with different compute-unit "
        "prices, the share of pairs where the higher price is placed earlier (0.5 = no relation). "
        "Interval: 95%, resampling whole validators. `tips_in_first_quarter`: share of transactions "
        "that pay a Jito tip that sit in the first quarter of the block.", "",
        f"All clients: higher_fee_first = {fmt_ci(*allrow)}.", "",
        show.to_markdown(index=False), "", "## By session", "", by_sess.to_markdown(), "",
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
