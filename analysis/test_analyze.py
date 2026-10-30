"""Checks that the analysis recovers known rules from synthetic data.

A fake leader orders our transactions by a known rule; the analysis must name that rule.
Run: python -m pytest analysis -q   (or python analysis/test_analyze.py)
"""

import json
import random
import shutil
import tempfile
from pathlib import Path

import analyze

LEVELS = 6


def make_session(root: Path, rule: str, client: str, n_windows: int = 12, seed: int = 1) -> None:
    rnd = random.Random(seed)
    d = root / f"sess-{client}-{rule}"
    d.mkdir(parents=True)
    sends, windows, landed, blocks = [], [], [], []
    for wid in range(n_windows):
        leader = f"L{wid % 3}"
        first = 1000 + wid * 40
        windows.append({"session": d.name, "window_id": wid, "leader": leader, "client_id": client,
                        "version": "x", "tpu_quic": None, "first_slot": first, "last_slot": first + 3,
                        "arm": {"contention": "shared", "fees": "mixed", "path": "rpc", "spacing_ms": 2, "n_tx": 12}})
        fees = [i % LEVELS for i in range(12)]
        rnd.shuffle(fees)
        order = list(range(12))
        rnd.shuffle(order)
        txs = []
        for rank, idx in enumerate(order):
            sig = f"{d.name}-{wid}-{idx}"
            txs.append((sig, rank, fees[idx]))
            sends.append({"session": d.name, "window_id": wid, "leader": leader, "client_id": client,
                          "first_slot": first, "tx_index": idx, "send_rank": rank, "signature": sig,
                          "payer": "P", "fee_level": fees[idx], "cu_price_micro_lamports": 0, "cu_limit": 1000,
                          "max_fee_lamports": 5000, "path": "rpc", "contention": "shared", "fees": "mixed",
                          "spacing_ms": 2, "sent_at_unix_ns": rank * 2_000_000, "est_slot_at_send": first,
                          "dry_run": False, "send_error": None})
        if rule == "fifo":
            placed = sorted(txs, key=lambda t: t[1])
        elif rule == "priority":
            placed = sorted(txs, key=lambda t: (-t[2], rnd.random()))
        else:
            placed = txs[:]
            rnd.shuffle(placed)
        for pos, (sig, _, _) in enumerate(placed):
            landed.append({"session": d.name, "window_id": wid, "signature": sig, "landed": True,
                           "slot": first, "index_in_block": pos * 3 + 1, "block_tx_count": 50,
                           "block_producer": leader, "fee_lamports": 5000, "compute_units_consumed": 450,
                           "tx_error": None})
        blocks.append({"session": d.name, "window_id": wid, "slot": first, "scheduled_leader": leader,
                       "block_producer": leader, "tx_count": 50, "skipped": False})
    for name, rows in [("sends", sends), ("windows", windows), ("landed", landed), ("blocks", blocks)]:
        (d / f"{name}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def run_case(rule: str, client: str) -> str:
    tmp = Path(tempfile.mkdtemp())
    try:
        make_session(tmp / "data", rule, client)
        sends, windows, blocks = analyze.load_sessions(sorted((tmp / "data").iterdir()))
        wins, pairs = analyze.window_rows(sends, windows)
        by_client = analyze.pooled(pairs, wins, ["client"])
        return by_client.iloc[0].verdict
    finally:
        shutil.rmtree(tmp)


def test_detects_fifo():
    assert run_case("fifo", "A").startswith("arrival (strict)")


def test_detects_priority():
    assert run_case("priority", "B").startswith("priority (strict)")


def test_random_is_no_rule():
    assert run_case("random", "C") == "no detectable rule"


def test_kendall_basics():
    assert analyze.kendall_tau_b([0, 1, 2], [0, 1, 2]) == 1.0
    assert analyze.kendall_tau_b([0, 1, 2], [2, 1, 0]) == -1.0


if __name__ == "__main__":
    for t in [test_kendall_basics, test_detects_fifo, test_detects_priority, test_random_is_no_rule]:
        t()
        print("ok", t.__name__)
