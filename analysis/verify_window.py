#!/usr/bin/env python3
"""Check one measurement window against the chain yourself. No key, no SOL, standard library only.

For one window it reads what we sent (send order, fee, signature) from `data/`, fetches the
blocks from a public RPC with `getBlock`, finds our transactions in them, and prints where each one
sits. It also checks that the order on chain is the order recorded in `data/<session>/landed.jsonl`.

    python analysis/verify_window.py --list --session 20261102T071208Z-live
    python analysis/verify_window.py --session 20261102T071208Z-live --window 16

Default RPC: https://api.mainnet-beta.solana.com (set --rpc or RPC_URL to use another one).
With Docker: docker compose run --rm --entrypoint python analyze analysis/verify_window.py --data /data ...
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from itertools import combinations
from pathlib import Path

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def rpc(url: str, method: str, params: list, attempts: int = 6):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    delay = 1.0
    for _ in range(attempts):
        req = urllib.request.Request(url, body, {"Content-Type": "application/json",
                                                 "User-Agent": "solana-block-ordering-verify/1"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(delay); delay *= 2; continue
            raise SystemExit(f"{method}: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError):
            time.sleep(delay); delay *= 2; continue
        if "error" in out:
            return None, out["error"]
        return out["result"], None
    raise SystemExit(f"{method}: no answer from the RPC after {attempts} attempts")


def get_block(url: str, slot: int):
    for version in (1, 0):
        res, err = rpc(url, "getBlock", [slot, {"encoding": "json", "transactionDetails": "signatures",
                                                "rewards": True, "maxSupportedTransactionVersion": version,
                                                "commitment": "finalized"}])
        if err is None:
            return res
        if version == 1 and (err.get("code") == -32602 or "maxSupportedTransactionVersion" in str(err)):
            continue
        raise SystemExit(f"getBlock {slot}: {err.get('message', err)}")
    return None


def list_windows(sess: Path) -> None:
    sends = read_jsonl(sess / "sends.jsonl")
    landed = {r["signature"]: r for r in read_jsonl(sess / "landed.jsonl")}
    print(f"{'window':>6}  {'client':<20} {'path':<4} {'phase':<6} {'fees':<5} {'sent':>4} {'in leader block':>15}")
    for w in read_jsonl(sess / "windows.jsonl"):
        mine = [s for s in sends if s["window_id"] == w["window_id"]]
        ok = sum(1 for s in mine if landed.get(s["signature"], {}).get("block_producer") == w["leader"])
        arm = w["arm"]
        print(f"{w['window_id']:>6}  {w['client_id']:<20} {arm['path']:<4} {arm.get('phase', 'pre'):<6} "
              f"{arm['fees']:<5} {len(mine):>4} {ok:>15}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data")
    ap.add_argument("--session", required=True)
    ap.add_argument("--window", type=int)
    ap.add_argument("--list", action="store_true", help="list the windows of the session")
    ap.add_argument("--rpc", default=os.environ.get("RPC_URL", PUBLIC_RPC))
    a = ap.parse_args()

    sess = Path(a.data) / a.session
    if not (sess / "windows.jsonl").exists():
        print(f"no session folder {sess}", file=sys.stderr)
        return 2
    if a.list or a.window is None:
        list_windows(sess)
        return 0

    w = next((x for x in read_jsonl(sess / "windows.jsonl") if x["window_id"] == a.window), None)
    if w is None:
        print(f"window {a.window} not found in {a.session}", file=sys.stderr)
        return 2
    sends = sorted((s for s in read_jsonl(sess / "sends.jsonl") if s["window_id"] == a.window),
                   key=lambda s: s["send_rank"])
    landed = {r["signature"]: r for r in read_jsonl(sess / "landed.jsonl")}
    arm = w["arm"]
    host = a.rpc.split("//")[-1].split("/")[0].split("?")[0]
    print(f"session {a.session}, window {a.window}: leader {w['leader']} ({w['client_id']} {w.get('version', '')})")
    print(f"slots {w['first_slot']}–{w['last_slot']}; arm: path {arm['path']}, phase {arm.get('phase', 'pre')}, "
          f"contention {arm['contention']}, fees {arm['fees']}, spacing {arm['spacing_ms']} ms")
    print(f"reading blocks from {host}\n")

    slots = sorted({landed[s["signature"]]["slot"] for s in sends
                    if landed.get(s["signature"], {}).get("landed") and landed[s["signature"]].get("slot")})
    found = {}
    for slot in slots:
        b = get_block(a.rpc, slot)
        if b is None:
            print(f"slot {slot}: block not available")
            continue
        producer = next((r["pubkey"] for r in (b.get("rewards") or []) if r.get("rewardType") == "Fee"), None)
        sigs = b.get("signatures") or []
        pos = {sig: i for i, sig in enumerate(sigs)}
        for s in sends:
            if s["signature"] in pos:
                found[s["signature"]] = (slot, pos[s["signature"]], len(sigs), producer)
        tag = "scheduled leader" if producer == w["leader"] else f"produced by {producer}"
        print(f"slot {slot}: {len(sigs)} transactions, {tag}  https://solscan.io/block/{slot}")

    rows = []
    for s in sends:
        f = found.get(s["signature"])
        rows.append({"sent": s["send_rank"] + 1, "fee": s["cu_price_micro_lamports"], "sig": s["signature"],
                     "slot": f[0] if f else None, "pos": f[1] if f else None,
                     "leader": bool(f and f[3] == w["leader"]),
                     "recorded": landed.get(s["signature"], {}).get("index_in_block")})
    print(f"\n{'slot':>11} {'position':>8}  {'sent as':>7}  {'fee µlamports/CU':>16}  signature")
    for r in sorted(rows, key=lambda r: (r["slot"] is None, r["slot"] or 0, r["pos"] or 0)):
        where = f"{r['slot']:>11} {r['pos']:>8}" if r["slot"] is not None else f"{'not in any block':>20}"
        print(f"{where}  {r['sent']:>7}  {r['fee']:>16,}  {r['sig'][:20]}…")

    # same order as recorded by `ordprobe collect`?
    on_chain = [r["sig"] for r in sorted((r for r in rows if r["slot"] is not None), key=lambda r: (r["slot"], r["pos"]))]
    recorded = [s["signature"] for s in sorted((s for s in sends if landed.get(s["signature"], {}).get("index_in_block") is not None),
                                               key=lambda s: (landed[s["signature"]]["slot"], landed[s["signature"]]["index_in_block"]))]
    print(f"\nfound on chain: {len(on_chain)} of {len(sends)} sent; recorded as landed: {len(recorded)}")
    print("order on chain matches data/landed.jsonl:", "yes" if on_chain == recorded else "NO")

    # what this one window says (pairs in the same block of the scheduled leader)
    lead = sorted((r for r in rows if r["leader"]), key=lambda r: (r["slot"], r["pos"]))
    arr = fee = arr_n = fee_n = 0
    for x, y in combinations(lead, 2):
        if x["slot"] != y["slot"]:
            continue
        arr_n += 1; arr += x["sent"] < y["sent"]
        if x["fee"] != y["fee"]:
            fee_n += 1; fee += x["fee"] > y["fee"]
    if arr_n:
        print(f"same-block pairs in the leader's blocks: {arr_n}; follow send order {arr}/{arr_n}"
              + (f"; follow the fee {fee}/{fee_n}" if fee_n else ""))
    return 0 if on_chain == recorded else 1


if __name__ == "__main__":
    raise SystemExit(main())
