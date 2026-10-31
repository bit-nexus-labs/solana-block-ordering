# Inside the Block: how Solana leaders order transactions

A measurement tool and study. We send our own small transactions into the leader windows of
chosen mainnet validators, read the finished blocks back, and test for every validator which
rule decided the order of *our* transactions: arrival time, priority fee, both, or neither.

The question is narrow on purpose. Agave's scheduler is open source, but many leaders run
something else (Jito-Solana, BAM, Paladin, Harmonic, Firedancer, Frankendancer, Rakurai…), and
what those do with ordinary TPU traffic is not documented. We do not try to read anybody's
code. We look only at outcomes we caused ourselves and can verify on chain.

* `src/` — `ordprobe`, the Rust tool that plans, sends, and collects.
* `analysis/` — the Python analysis that turns raw logs into results.
* `data/` — raw logs of every session (signatures, slots, timestamps). Committed.
* `results/` — tables and figures generated from `data/`. Committed and reproducible.
* [`REPORT.md`](REPORT.md) — findings, dates of every measurement, what went wrong.

## Quick start (one command per step)

Requirements: Docker with Compose. Nothing else.

```bash
cp .env.example .env              # set RPC_URL (a mainnet RPC for real runs)
docker compose run --rm analyze   # rebuild results/ from the committed data/  <- judges start here
```

To measure yourself:

```bash
docker compose run --rm probe census              # validators by client type and stake (read only)
docker compose run --rm probe keygen              # creates ./keys/probe.json, prints the address
#   send ~0.06 SOL to that address (mainnet), or `probe airdrop` on devnet
docker compose run --rm probe fund                # dry-run: shows what funding the 8 extra payers would do
docker compose run --rm probe fund --live         # funds them (0.002 SOL each, returned by `sweep`)
docker compose run --rm probe run                 # DRY RUN: builds, signs, simulates, sends nothing
docker compose run --rm probe run --live          # real session, stopped by the spend cap
docker compose run --rm probe collect --session <id printed by run>
docker compose run --rm analyze
docker compose run --rm probe sweep --live        # return the extra payers' SOL
```

Safety rules built into the tool:

* **Dry run by default.** `run`, `fund` and `sweep` only send with `--live`.
* **Spend cap.** `SPEND_CAP_LAMPORTS` (default 45,000,000 = 0.045 SOL) limits the sum of the
  *maximum possible fee* of every transaction ever handed to a sender in `data/`, counted before
  sending and carried across sessions. A window that would cross the cap is not sent.
* **Keys stay outside the repo.** The key is read from a mounted file (`KEYS_DIR`); `.env` and
  `keys/` are git-ignored. The RPC key is written to logs only as a host name.

## What one measurement is

1. `ordprobe` reads the leader schedule (`getSlotLeaders`) a few hundred slots ahead and groups
   it into leader windows (4 consecutive slots, `NUM_CONSECUTIVE_LEADER_SLOTS`).
2. It labels every leader with its client from gossip (`getClusterNodes` → `clientId`, e.g.
   `Agave`, `JitoLabs`, `AgaveBam`, `Firedancer`), plus version and stake.
3. It picks the next window whose client still needs data (quotas per client and per validator,
   so one big validator cannot stand for its whole client).
4. It draws an **experiment arm** for that window at random (seeded, logged):
   contention × fee design × path × spacing × phase (see below).
5. It builds N transactions. Each is a self-transfer with a fixed compute budget
   (`SetComputeUnitLimit(1000)`, `SetComputeUnitPrice(p)`), so all have the **same cost** and
   differ only in the priority fee `p` and in the amount that makes the signature unique.
   Fee levels and the send order are **two independent random permutations**.
6. It fires the batch either half a slot before the window starts or about one slot into it
   (the *phase*), one transaction every 2 ms ("burst") or every 25 ms ("spaced"), and logs the
   wall-clock time and the probe's slot estimate of every send (`--rpc-extra-lead-slots` can
   fire the `rpc` path earlier if it reaches the leader later than direct QUIC). A window the probe
   would reach late is skipped and logged in `skipped.jsonl`, never sent late.
7. `collect` reads where each transaction landed (`getSignatureStatuses`), the order of all
   transactions in those blocks (`getBlock`, `transactionDetails: "signatures"`), and who really
   produced each block (the `Fee` reward recipient).

## The experiment arms

| factor | levels | why |
|---|---|---|
| contention | **shared** (one fee payer, every tx write-locks the same account) / **independent** (8 payers, no shared writable account) | a scheduler may order conflicting transactions differently from independent ones |
| fees | **mixed** (6 levels 0 … 5,000,000 µlamports/CU, each used twice) / **equal** (all the same) | mixed separates fee from arrival; equal shows the arrival rule alone |
| path | **rpc** (`sendTransaction`, `skipPreflight`, `maxRetries 0`) / **tpu** (QUIC to the leader's `tpuQuic`, unstaked) | the route changes arrival order and timing |
| spacing | **2 ms** / **25 ms** | short gaps test batching inside the scheduler, long gaps test streaming |
| phase | **pre** (arrives just before the window) / **inside** (arrives about one slot in) | before the window the leader can see the whole batch at once; inside it, transactions reach the scheduler one by one |

Assignment is random per window and independent of who the leader is, so arm effects and leader
effects are not confounded.

## How each required consideration is handled

**Leader schedule and types.** The schedule comes from `getSlotLeaders` for the live epoch, not
from a cached file. Windows are aligned to 4 slots and a window is only used if all four slots
belong to the same leader. Client type comes from the gossip `clientId` (Agave ≥ 4 RPC nodes
return it); if an RPC node does not return it, the label falls back to the version string and
says so (`Firedancer-family(version-only)`). After collection every block's real producer is
compared with the schedule; blocks produced by someone else, and skipped slots, are reported and
their transactions are excluded from the rule analysis.

**Transaction path from backend to executing node.** Two paths are measured as a factor.
`rpc`: our process → RPC provider (HTTP) → provider's forwarding (usually staked QUIC to the
current and next leaders). `tpu`: our process → leader's TPU QUIC port directly, unstaked, with
the leader pinned (no fan-out). Before a `tpu` window the QUIC connection is opened with a
transaction that the leader will drop (unfunded random fee payer), so the handshake does not eat
into the window. For Jito-Solana and BAM leaders the TPU path is the leader's normal TPU; bundles
and tips are out of scope and never used.

**Interaction among our own transactions.** Our transactions compete with each other only through
the accounts they lock. The `shared` arm makes them conflict on purpose (same writable fee payer);
the `independent` arm removes every conflict. Results are reported per arm.

**Competition within the block.** Other users' transactions are in the same blocks. We do not
interpret their order (we do not know when they arrived). We use the block only as the ruler that
gives our transactions' positions, and we record the block size so busy and quiet blocks can be
compared. Pairs of our transactions that ended up in *different* blocks of the window are kept
apart from pairs in the *same* block: "sent later, so landed a slot later" is timing, not a
scheduling decision.

**Send order.** Recorded per transaction with a nanosecond wall-clock timestamp and the planned
rank. Because fee level and send rank are independent random draws, a leader that sorts by fee
and a leader that keeps arrival order produce different, distinguishable outcomes.

**Timing.** The probe keeps its own estimate of the current slot from `getSlot` answers: the
slot duration is a least-squares fit over the last answers, and the line is placed at the most
advanced answer, because a load-balanced RPC endpoint can answer from a node that lags. The
firing time is recomputed every 50 ms while waiting. `results/timing_by_client_path.csv`
shows, per client and path, where the clock was at the first send and how many slots later the
transactions landed, so every result can be checked against how well the window was hit.

**Noise versus rule.** The unit of evidence is a pair of our transactions in one block. For each
validator and each client we report the share of pairs ordered by arrival and by fee, with 95%
confidence intervals from resampling **whole windows** (pairs from one window are not
independent), the share of "conflict" pairs (arrival and fee disagree) won by the fee, and a
pairwise logistic model `P(first) ~ β_arrival·arrival + β_fee·fee`. A rule is called only if its
interval excludes 0.5 (coin flip); "strict" means at least 95% of pairs follow it. On random
synthetic data this procedure names a rule in about 5% of cases (`analysis/test_analyze.py`).
"No detectable rule" is a legitimate result.

## What is not separated

* **Network jitter on the path** versus **the scheduler**: with 2 ms spacing two transactions can
  swap on the way. The 25 ms arm and the direct TPU path bound this effect but cannot remove it.
* **RPC provider behaviour**: on the `rpc` path the provider may forward to more than one leader;
  we see the result, not the provider's internals.
* **Load**: we do not control how full a leader's queue is. Block size is logged, not controlled.
* **Unstaked QUIC**: on the `tpu` path our connection has the lowest stake-weighted QoS. A leader
  may treat staked traffic differently.
* **Local fee markets**: all our transactions touch only our own accounts, so we do not measure
  ordering among transactions that fight over a popular account.

## Running without Docker

`cargo build --release` (Rust 1.97), then the same subcommands with `target/release/ordprobe`.
The analysis needs Python 3.12 with `analysis/requirements.txt`; on newer Python versions
install the latest `numpy pandas matplotlib tabulate pytest` instead of the pinned ones.

## Repository map

| file | content |
|---|---|
| `src/plan.rs` | windows from the schedule, client labels, quotas, random arm assignment |
| `src/txbuild.rs` | probe transactions, fee levels, maximum fee |
| `src/spend.rs` | spend cap |
| `src/sender.rs` | RPC path and direct TPU (QUIC) path |
| `src/slotclock.rs` | slot estimate between RPC polls |
| `src/run.rs` | a measurement session |
| `src/collect.rs` | landing slot, block position, block producer |
| `analysis/analyze.py` | pairs, intervals, verdicts, figures |
| `analysis/test_analyze.py` | the analysis recovers known rules from synthetic data |
