#!/usr/bin/env python3
"""Build the one-page interactive report `docs/index.html` from the committed data and results.

    python analysis/build_site.py            # reads data/, results/, results-s6/, results-blocks/

Every number on the page comes from those files; nothing is typed in by hand. The page is a
single self-contained HTML file (no external scripts) served by GitHub Pages from `docs/`.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

SESSION_NOTES = {
    "20261102T003610Z-live": "first live session",
    "20261102T053618Z-live": "",
    "20261102T071208Z-live": "",
    "20261102T143037Z-live": "US market hours",
    "20261103T153045Z-live": "pre-registered test 1",
    "20261103T192233Z-live": "pre-registered test 2 + RPC providers",
    "20261104T080046Z-live": "pre-registered test 3",
}
PREREG_COMMITS = [
    ("PREREGISTRATION.md", "16f4af8", "2026-11-02 16:59 UTC"),
    ("PREREGISTRATION_2.md", "625f04a", "2026-11-03 18:43 UTC"),
    ("PREREGISTRATION_3.md", "c39d4f7", "2026-11-03 21:01 UTC"),
]


def read_csv(p: Path) -> list[dict]:
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def num(x, nd=3):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else round(v, nd)


def ci(text):
    vals = re.findall(r"[-+−]?\d+\.\d+", str(text).replace("−", "-"))
    return [float(v) for v in vals[-2:]] if len(vals) >= 2 else [None, None]


def build_windows(data: Path) -> tuple[list, list]:
    sessions, windows = [], []
    for si, sess in enumerate(sorted(p for p in data.iterdir() if p.is_dir() and p.name.endswith("-live"))):
        sends = read_jsonl(sess / "sends.jsonl")
        if not sends or all(s.get("dry_run") for s in sends):
            continue
        landed = {r["signature"]: r for r in read_jsonl(sess / "landed.jsonl")}
        wins = read_jsonl(sess / "windows.jsonl")
        by_w: dict[int, list] = {}
        for s in sends:
            by_w.setdefault(s["window_id"], []).append(s)
        sessions.append({"id": sess.name, "note": SESSION_NOTES.get(sess.name, ""), "windows": len(wins)})
        for w in wins:
            arm = w["arm"]
            txs = []
            for s in sorted(by_w.get(w["window_id"], []), key=lambda s: s["send_rank"]):
                l = landed.get(s["signature"], {})
                ok = bool(l.get("landed")) and l.get("slot") is not None
                txs.append([s["send_rank"] + 1, s["cu_price_micro_lamports"],
                            l.get("slot") if ok else 0,
                            l.get("index_in_block") if ok and l.get("index_in_block") is not None else -1,
                            # in the scheduled leader's window slots (only that leader can produce them;
                            # some leaders pay block fees to another account, see fee_collectors.py)
                            1 if ok and w["first_slot"] <= l["slot"] <= w["last_slot"] else 0,
                            s["signature"] if ok else ""])
            windows.append([len(sessions) - 1, w["window_id"], w["client_id"], w["leader"], arm["path"],
                            arm.get("phase", "pre"), arm["contention"], arm["fees"], arm["spacing_ms"],
                            w.get("send_provider") or ("direct" if arm["path"] == "tpu" else "public"), txs])
    return sessions, windows


def build_data(root: Path) -> dict:
    res, s6, blk = root / "results", root / "results-s6", root / "results-blocks"
    tests = [{"test": r["test"], "kind": r["kind"], "windows": int(r["windows"]), "diff": num(r["diff"], 2),
              "lo": num(r["lo"], 2), "hi": num(r["hi"], 2), "confirmed": r.get("confirmed", "")}
             for r in read_csv(res / "preregistered_tests.csv")]
    clients = []
    for r in read_csv(res / "by_client.csv"):
        clients.append({"client": r["client"], "validators": int(r["validators"]), "windows": int(r["windows"]),
                        "pairs": int(r["pairs"]), "verdict": r["verdict"],
                        "all": {k: num(r[k]) for k in ("arrival_agree", "arrival_agree_lo", "arrival_agree_hi",
                                                        "fee_agree", "fee_agree_lo", "fee_agree_hi",
                                                        "fee_wins_conflicts", "fee_wins_lo", "fee_wins_hi")}})
    phase = {}
    for r in read_csv(res / "by_client_phase.csv"):
        phase.setdefault(r["client"], {})[r["phase"]] = {
            "pairs": int(r["pairs"]), "verdict": r["verdict"],
            **{k: num(r[k]) for k in ("arrival_agree", "arrival_agree_lo", "arrival_agree_hi", "fee_agree",
                                      "fee_agree_lo", "fee_agree_hi", "fee_wins_conflicts", "fee_wins_lo",
                                      "fee_wins_hi")}}
    for c in clients:
        c.update({"pre": phase.get(c["client"], {}).get("pre"), "inside": phase.get(c["client"], {}).get("inside")})
    providers_all = {r["provider"]: r for r in read_csv(res / "by_provider.csv")}
    providers_s6 = [{"provider": r["provider"], "windows": int(r["windows"]), "leader": num(r["landed_in_leader"], 2),
                     "other": num(r["landed_other_leader"], 2), "never": num(r["never_landed"], 2),
                     "delay": num(r["land_delay"], 2)} for r in read_csv(s6 / "by_provider.csv")]
    paths = []
    for key, label in [("direct-quic", "direct QUIC to the leader"), ("public", "public RPC")]:
        r = providers_all.get(key)
        if r:
            lo, hi = ci(r["phase_diff_ci"])
            paths.append({"path": label, "windows": int(r["windows"]), "leader": num(r["landed_in_leader"], 2),
                          "other": num(r["landed_other_leader"], 2), "never": num(r["never_landed"], 2),
                          "diff": num(r["phase_diff"], 2), "lo": lo, "hi": hi})
    price = [{"bucket": r["cu_price (micro-lamports)"], "share": num(r["share"]), "pos": num(r["mean_position"])}
             for r in read_csv(blk / "whole_blocks_by_price.csv")]
    blk_clients = []
    for r in read_csv(blk / "whole_blocks_by_client.csv"):
        if int(r["validators"]) < 3:
            continue
        near = ci(r["higher_fee_first_near"])
        blk_clients.append({"client": r["client"], "blocks": int(r["blocks"]),
                            "near": num(re.findall(r"\d+\.\d+", r["higher_fee_first_near"])[0], 3),
                            "near_lo": near[0], "near_hi": near[1], "probe": num(r["probe_fee_agree"], 2)})
    md = (blk / "WHOLE_BLOCKS.md").read_text(encoding="utf-8") if (blk / "WHOLE_BLOCKS.md").exists() else ""
    m = re.search(r"(\d[\d,]*) blocks from (\d+) validators and (\d+) clients, (\d[\d,]*) transactions", md)
    shift = re.search(r"top_fee_shift = ([-\d.]+) \(([-\d.]+) … ([-\d.]+)\)", md)
    sessions, windows = build_windows(root / "data")
    sent = sum(len(w[10]) for w in windows)
    landed = sum(1 for w in windows for t in w[10] if t[2])
    return {
        "tests": tests, "clients": clients, "providers": providers_s6, "paths": paths, "price": price,
        "blockClients": blk_clients, "sessions": sessions, "windows": windows,
        "blocks": {"blocks": m.group(1) if m else "", "validators": m.group(2) if m else "",
                   "tx": m.group(4) if m else "",
                   "shift": [float(shift.group(i)) for i in (1, 2, 3)] if shift else None},
        "totals": {"sessions": len(sessions), "windows": len(windows),
                   "validators": len({w[3] for w in windows}), "clients": len({w[2] for w in windows}),
                   "sent": sent, "landed": landed},
        "prereg": [{"file": f, "commit": c, "time": t} for f, c, t in PREREG_COMMITS],
        "hero": next(({"pre": num(r["fee_wins_pre"]), "inside": num(r["fee_wins_inside"])}
                      for r in read_csv(root / "results-s7" / "phase_effect.csv") if r["client"] == "all clients"), None),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--out", type=Path, default=Path("docs/index.html"))
    ap.add_argument("--template", type=Path, default=Path(__file__).with_name("site_template.html"))
    a = ap.parse_args()
    data = build_data(a.root)
    html = a.template.read_text(encoding="utf-8").replace(
        "/*__DATA__*/null", json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(html, encoding="utf-8")
    (a.out.parent / ".nojekyll").write_text("", encoding="utf-8")
    img = a.root / "results" / "mechanism.png"
    if img.exists():
        (a.out.parent / "mechanism.png").write_bytes(img.read_bytes())
    t = data["totals"]
    print(f"wrote {a.out} ({len(html) // 1024} KB): {t['sessions']} sessions, {t['windows']} windows, "
          f"{t['sent']} transactions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
