# Pre-registered confirmation: session 5

Written and committed on 2026-11-02, **before** session 5 was run. The commit time of this file in
the repository history is the proof of that order.

## What sessions 1–4 suggested

Sessions 1–4 (2026-11-02, 228 leader windows across 10 clients, raw data in `data/`) showed this
pattern. Take two of our transactions in the same block whose send order and fee point in opposite
directions (a "conflict pair"). The higher fee won that conflict more often when the batch was
fired to arrive **before** the leader's window ("pre") than when it arrived **inside** the window,
about one slot in ("inside"):

* all clients pooled: 0.53 (pre) against 0.31 (inside), difference +0.23, 95% CI +0.05 … +0.39;
* the same direction in every client with conflict pairs in both phases, but no single client
  reaches significance on its own;
* in US market hours (session 4, blocks about twice as full) the difference was smaller (+0.13,
  CI −0.11 … +0.42) than in quiet hours (+0.26, CI +0.04 … +0.45).

This was found by looking at the data, so on its own it is exploratory. Session 5 tests it on
new data with the test fixed in advance.

## Hypotheses

* **H1 (primary).** Pooled over all clients, the share of conflict pairs won by the higher fee is
  larger for `pre` windows than for `inside` windows.
* **H2 (secondary).** The direction of H1 holds for each client separately (reported as a count
  of clients, not tested one by one).
* **H3 (secondary).** The effect is smaller in busy hours than in quiet hours (compared with
  sessions 1–3; session 5 runs in US market hours like session 4).

## Test, fixed in advance

* Data: **session 5 only**. Sessions 1–4 are not used for the test.
* Unit: same-block pairs of our own transactions in the scheduled leader's blocks where the send
  order and the fee point in opposite directions.
* Statistic: share of those pairs where the higher fee was placed first, `pre` minus `inside`.
* Uncertainty: 95% interval from 4000 resamples of whole windows within each phase
  (`phase_effect_table` in `analysis/analyze.py`, seed fixed in the file).
* **H1 is confirmed if the lower end of the 95% interval is above 0**, and not confirmed otherwise.
  Both outcomes are reported.
* Command: `python analysis/analyze.py --data data --out results-s5 --sessions <session 5 id>`,
  row `all clients` of the "Phase effect" table.

## Session 5 settings, fixed in advance

Chosen to collect as many conflict pairs as possible for this one question; every other factor
was already measured in sessions 1–4.

* Start: 2026-11-03, 17:30 Kyiv (15:30 UTC), US market hours.
* `--no-tpu` (rpc path only: the direct QUIC path loses about half of a 2 ms burst),
  `--equal-fee-share 0` (every window has mixed fees, so every window can produce conflict pairs),
  phase drawn 50/50 as before, contention and spacing drawn as before.
* Clients: Agave, AgaveBam, FireBAM, Firedancer, HarmonicAgave, HarmonicFiredancer, JitoLabs,
  Rakurai (Raiku and the unknown client have one validator each and are left out).
* `--max-windows 100 --max-minutes 120 --windows-per-client 12 --validators-per-client 8
  --windows-per-validator 2`, same spend cap as before.
