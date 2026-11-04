#!/usr/bin/env python3
"""Blocks whose `Fee` reward goes to someone other than the scheduled leader.

`ordprobe collect` names the producer of a block by the recipient of its `Fee` reward. For 21
leaders that recipient is a different account in every one of their blocks. Only the scheduled
leader can produce a block for its slot, so these are most likely accounts that collect the
leader's block fees, not other producers. This script

* lists those leaders and recipients from `data/` (offline);
* with `--rpc`, checks each pair on chain, read only: the leader of the slot per `getSlotLeaders`,
  whether the recipient is the identity of any validator (`getVoteAccounts`), and what kind of
  account it is (`getAccountInfo`);
* recomputes the main results with every block inside a window counted as the scheduled
  leader's (a sensitivity check; the committed results keep the definition the pre-registered
  tests ran with).

    python analysis/fee_collectors.py --data data --out results                  # offline part
    python analysis/fee_collectors.py --data data --out results --rpc <URL>      # plus the on-chain check
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import analyze as A  # noqa: E402

TESTS = [("pre-registration 1: session 5 (H1)", ["20261103T153045Z-live"], None),
         ("pre-registration 2: session 6, direct QUIC (H4)", ["20261103T192233Z-live"], "tpu"),
         ("pre-registration 3: session 7, direct QUIC (H4)", ["20261104T080046Z-live"], "tpu"),
         ("all sessions, direct QUIC", None, "tpu"),
         ("all sessions, every path", None, None)]


def mismatches(data: Path) -> pd.DataFrame:
    rows = []
    for f in sorted(data.glob("*/blocks.jsonl")):
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("scheduled_leader") and not r.get("skipped") and r.get("block_producer") \
                    and r["block_producer"] != r["scheduled_leader"]:
                rows.append(r)
    return pd.DataFrame(rows)


def rpc(url: str, method: str, params: list, attempts: int = 5):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for i in range(attempts):
        req = urllib.request.Request(url, body, {"Content-Type": "application/json", "User-Agent": "solana-block-ordering/1"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.load(r)
            break
        except (OSError, urllib.error.URLError):
            if i == attempts - 1:
                raise
            time.sleep(2 ** i)
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error']}")
    return out["result"]


def onchain(mm: pd.DataFrame, url: str) -> pd.DataFrame:
    va = rpc(url, "getVoteAccounts", [])
    identities = {v["nodePubkey"] for v in va["current"] + va["delinquent"]}
    vote_of = {v["votePubkey"]: v["nodePubkey"] for v in va["current"] + va["delinquent"]}
    rows = []
    for (leader, rec), g in mm.groupby(["scheduled_leader", "block_producer"]):
        slot = int(g.slot.iloc[0])
        sched = rpc(url, "getSlotLeaders", [slot, 1])[0]
        info = rpc(url, "getAccountInfo", [rec, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}])["value"]
        time.sleep(0.3)
        rows.append({"scheduled_leader": leader, "fee_recipient": rec, "blocks": len(g), "example_slot": slot,
                     "slot_leader_per_schedule": sched, "schedule_matches_leader": sched == leader,
                     "recipient_is_a_validator_identity": rec in identities,
                     "recipient_is_vote_account_of_leader": vote_of.get(rec) == leader,
                     "recipient_owner": info["owner"] if info else "(no account)",
                     "recipient_lamports": info["lamports"] if info else 0})
    return pd.DataFrame(rows)


def relabel(sends: pd.DataFrame, windows: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    s = sends.copy()
    w = windows[["session", "window_id", "leader", "first_slot", "last_slot"]].rename(
        columns={"leader": "_leader", "first_slot": "_first", "last_slot": "_last"})
    m = s.merge(w, on=["session", "window_id"], how="left")
    sel = (m.landed == True) & (m.slot >= m._first) & (m.slot <= m._last)  # noqa: E712
    changed = int(((m.block_producer != m._leader) & sel).sum())
    s.loc[sel.values, "block_producer"] = m.loc[sel, "_leader"].values
    return s, changed


def sensitivity(data: Path) -> tuple[pd.DataFrame, int]:
    sends, windows, _, _ = A.load_sessions(sorted(p for p in data.iterdir() if p.is_dir()))
    corrected, changed = relabel(sends, windows)
    rows = []
    for label, S in [("as committed (Fee recipient = producer)", sends), ("window slots = scheduled leader", corrected)]:
        _, pairs = A.window_rows(S, windows)
        summ = A.pair_summary(pairs[pairs.same_block])
        row = {"definition": label, "pair_windows": summ["pair_windows"], "pairs": summ["pairs"],
               "follows_send_order": round(summ["arrival_agree"], 3), "follows_fee": round(summ["fee_agree"], 3),
               "fee_wins_conflicts": round(summ["fee_wins_conflicts"], 3)}
        for name, sess, path in TESTS:
            p = pairs
            if sess:
                p = p[p.session.isin(sess)]
            if path:
                p = p[p.path == path]
            A.RNG = np.random.default_rng(20261026)  # same seed for both definitions
            r = A.phase_effect_table(p)
            r = r[r.client == "all clients"].iloc[0]
            row[name] = f"{r['diff']:+.2f} ({r.diff_lo:+.2f} … {r.diff_hi:+.2f})"
        rows.append(row)
    return pd.DataFrame(rows), changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=Path("data"))
    ap.add_argument("--out", type=Path, default=Path("results"))
    ap.add_argument("--rpc", help="RPC URL for the on-chain check (read only)")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    mm = mismatches(a.data)
    per = mm.groupby(["scheduled_leader", "block_producer"]).size().reset_index(name="blocks")
    print(f"{len(mm)} window blocks whose Fee reward went to another account, "
          f"{per.scheduled_leader.nunique()} leaders, {len(per)} distinct recipients\n")
    if a.rpc:
        oc = onchain(mm, a.rpc)
        oc.to_csv(a.out / "fee_collectors.csv", index=False)
        print(oc.drop(columns=["recipient_lamports"]).to_markdown(index=False), "\n")
    else:
        per.to_csv(a.out / "fee_collectors.csv", index=False)
    sens, changed = sensitivity(a.data)
    sens.to_csv(a.out / "fee_collectors_sensitivity.csv", index=False)
    print(f"transactions moved into the scheduled leader's blocks: {changed}\n")
    print(sens.T.to_markdown())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
