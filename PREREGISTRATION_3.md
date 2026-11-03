# Pre-registered confirmation 3: session 7 (direct QUIC only, enough windows)

Written and committed on 2026-11-03, **before** session 7 was run. The commit time of this file is
the proof of that order. [`PREREGISTRATION.md`](PREREGISTRATION.md) and
[`PREREGISTRATION_2.md`](PREREGISTRATION_2.md) stay as they were; all three results are reported in
`REPORT.md` exactly as they came out.

## Why a third test

* Pre-registration 1 (session 5, public RPC only): H1 not confirmed, +0.09 (95% CI −0.11 … +0.29).
* Pre-registration 2 (session 6, H4, direct QUIC): **not confirmed**, +0.38 (95% CI −0.14 … +0.63).
  The estimate is close to the exploratory value (+0.44), but only 44 of the 96 windows used the
  direct path (the rest compared RPC providers), and only some of them produced conflict pairs.

Power, computed after session 6 by resampling the 160 direct-QUIC windows of sessions 1–6 (this
uses the same data the effect was found in, so it is optimistic): with 44 windows the test confirms
in about 41% of resamples, with 100 windows in about 76%, with 160 windows in about 87%, with 200 windows in about 93%. Session 7
therefore sends **only** on the direct path and aims at 200 windows.

Not hidden: in sessions 1 and 2 the direct path showed the opposite direction (few windows: 6 and
10 with conflict pairs); sessions 3, 4 and 6 showed pre > inside.

## Hypothesis

* **H4 (same as pre-registration 2).** On the direct QUIC path, the share of conflict pairs won by
  the higher fee is larger for `pre` windows than for `inside` windows (all clients pooled).

## Test, fixed in advance

* Data: **session 7 only**.
* Unit, statistic and interval exactly as before (conflict pairs in the scheduled leader's blocks;
  pre minus inside; 4000 window resamples per phase).
* **Confirmed if the lower end of the 95% interval is above 0**, not confirmed otherwise. Both
  outcomes are reported. No further confirmation session is planned for this bounty.
* Command: `python analysis/analyze.py --data data --out results-s7 --sessions <session 7 id>`,
  table "By send path and provider", row `direct-quic`, columns `phase_diff` and `phase_diff_ci`
  (the same number as row `all clients` of the "Phase effect" table, since every window is direct).

## Session 7 settings, fixed in advance

* Start: 2026-11-04, 10:00 Kyiv (08:00 UTC).
* `--no-rpc` (direct QUIC only; a leader that publishes no QUIC address falls back to the public
  RPC, and such windows are not part of the test), `--equal-fee-share 0`; phase drawn 50/50, contention and spacing
  drawn as before.
* Clients: Agave, AgaveBam, FireBAM, Firedancer, HarmonicAgave, HarmonicFiredancer, JitoLabs,
  Rakurai. `--max-windows 200 --max-minutes 180 --windows-per-client 25 --validators-per-client 13
  --windows-per-validator 2`, same spend cap. If the session stops early (time limit, connection),
  the windows collected are the test data; nothing is added from other sessions.
