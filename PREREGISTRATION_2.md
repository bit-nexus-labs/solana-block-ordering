# Pre-registered confirmation 2: session 6

Written and committed on 2026-11-03, **before** session 6 was run (session 6 starts right after this commit). The commit time of this file is
the proof of that order. The first pre-registration ([`PREREGISTRATION.md`](PREREGISTRATION.md))
stays as it was; its result is reported in `REPORT.md` exactly as it came out.

## What happened since the first pre-registration

Session 5 (2026-11-03, 96 windows, RPC path only, US market hours) did **not** confirm H1 of the
first pre-registration: fee wins in conflict pairs, pre minus inside = +0.09, 95% CI −0.11 … +0.29.

Looking at sessions 1–5 together *after* that result, split by send path (exploratory, found by
looking at the data):

* direct QUIC to the leader (116 windows): pre minus inside = **+0.44**, 95% CI +0.18 … +0.65;
* public RPC (`api.mainnet-beta.solana.com`, 208 windows): **+0.03**, 95% CI −0.12 … +0.18.

A plausible reading: on the direct path our timing decides when the batch reaches the leader; the
public RPC forwards to several leaders and retries on its own schedule, so "before the window" and
"inside the window" no longer mean what we intended. Session 5 used only the RPC path, which would
explain the null result. Session 6 tests this on new data.

## Hypotheses

* **H4 (primary).** On the direct QUIC path, the share of conflict pairs won by the higher fee is
  larger for `pre` windows than for `inside` windows (all clients pooled).
* **H5 (exploratory, no confirmation claim).** RPC providers differ in where our transactions land
  and in whether the phase effect survives. Compared: public RPC, RPC Fast (free plan), Digital
  Energy (Builder plan). Reported descriptively with intervals.

## Test, fixed in advance

* Data: **session 6 only**, windows with path `tpu` (direct QUIC).
* Unit, statistic and interval exactly as in the first pre-registration (conflict pairs in the
  scheduled leader's blocks; pre minus inside; 4000 window resamples per phase).
* **H4 is confirmed if the lower end of the 95% interval is above 0**, not confirmed otherwise.
  Both outcomes are reported.
* Command: `python analysis/analyze.py --data data --out results-s6 --sessions <session 6 id>`,
  table "By send path and provider", row `direct-quic`, columns `phase_diff` and `phase_diff_ci`.

## Session 6 settings, fixed in advance

* Start: 2026-11-03, right after this file is committed (about 19:00 UTC), US market hours like
  sessions 4 and 5. In session 4 (US hours, exploratory) the direct QUIC path showed +0.51,
  CI +0.26 … +0.80, so busy hours are not expected to hide the effect on that path.
* Path drawn 50/50 direct QUIC / RPC as in sessions 1–4; for an RPC window the provider is drawn
  uniformly from those configured (`--extra-send-rpc-env`), and the label is logged per window.
* `--equal-fee-share 0` (every window has mixed fees); phase, contention and spacing drawn as before.
* Clients: Agave, AgaveBam, FireBAM, Firedancer, HarmonicAgave, HarmonicFiredancer, JitoLabs,
  Rakurai. `--max-windows 100 --max-minutes 120 --windows-per-client 12 --validators-per-client 8
  --windows-per-validator 2`, same spend cap.
