//! A measurement session: pick leader windows, fire probe batches into them, log everything.

use {
    crate::{
        keys,
        model::{Contention, NodeInfo, Phase, SendPath, SendRecord, WindowPlan},
        plan::{self, ArmConfig, Progress, Quotas},
        sender::{self, TpuPath},
        slotclock::SlotClock,
        spend::SpendCap,
        txbuild::{self, FEE_LEVELS, PROBE_CU_LIMIT},
        util,
    },
    anyhow::{bail, Context, Result},
    rand::{rngs::StdRng, SeedableRng},
    solana_commitment_config::CommitmentConfig,
    solana_keypair::Keypair,
    solana_rpc_client::nonblocking::rpc_client::RpcClient,
    solana_rpc_client_api::config::RpcSimulateTransactionConfig,
    solana_signer::Signer,
    solana_transaction::Transaction,
    std::{
        collections::HashMap,
        net::SocketAddr,
        path::PathBuf,
        sync::Arc,
        time::{Duration, Instant},
    },
};

#[derive(Clone, Debug)]
pub struct RunConfig {
    pub read_rpc_url: String,
    pub send_rpc_url: String,
    pub keypair_path: PathBuf,
    pub out_dir: PathBuf,
    pub live: bool,
    pub spend_cap_lamports: u64,
    pub clients: Option<Vec<String>>,
    pub min_stake_sol: u64,
    pub quotas: Quotas,
    pub arms: ArmConfig,
    pub max_windows: usize,
    pub max_minutes: u64,
    /// Fire the first send this many slots before the window's first slot.
    pub lead_slots: f64,
    /// Extra lead for the `rpc` path, which reaches the leader later than direct QUIC.
    pub rpc_extra_lead_slots: f64,
    /// Where the first send of an `inside` window is aimed, in slots after the window start.
    pub inside_offset_slots: f64,
    /// Skip a window instead of sending when the clock is more than this many slots past the
    /// planned firing point.
    pub max_late_slots: f64,
    /// Ignore windows that start sooner than this (time to build and sign).
    pub min_prep_slots: u64,
    /// How far ahead to look when choosing between windows.
    pub horizon_slots: u64,
    pub nominal_slot_ms: f64,
    pub clock_bias_slots: f64,
    pub seed: u64,
}

pub async fn snapshot_nodes(rpc: &RpcClient) -> Result<HashMap<String, NodeInfo>> {
    let nodes = rpc.get_cluster_nodes().await.context("getClusterNodes")?;
    let votes = rpc.get_vote_accounts().await.context("getVoteAccounts")?;
    let mut stake: HashMap<String, u64> = HashMap::new();
    for v in votes.current.iter().chain(votes.delinquent.iter()) {
        *stake.entry(v.node_pubkey.clone()).or_default() += v.activated_stake;
    }
    Ok(nodes
        .into_iter()
        .map(|n| {
            let info = NodeInfo {
                activated_stake: stake.get(&n.pubkey).copied().unwrap_or(0),
                identity: n.pubkey.clone(),
                client_id: n.client_id.clone(),
                version: n.version.clone(),
                tpu_quic: n.tpu_quic.map(|a| a.to_string()),
            };
            (n.pubkey, info)
        })
        .collect())
}

/// Background re-anchoring of the slot clock. Uses the midpoint of the request as the
/// observation time, which halves the error caused by RPC latency.
fn spawn_clock_sync(rpc: Arc<RpcClient>, clock: SlotClock, every: Duration) {
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(every).await;
            let t0 = Instant::now();
            if let Ok(slot) = rpc
                .get_slot_with_commitment(CommitmentConfig::processed())
                .await
            {
                let t1 = Instant::now();
                clock.observe(slot, t0 + (t1 - t0) / 2);
            }
        }
    });
}

struct LeaderCache {
    windows: Vec<plan::Window>,
    fetched_to: u64,
}

impl LeaderCache {
    async fn refresh(&mut self, rpc: &RpcClient, from_slot: u64, count: u64) -> Result<()> {
        let leaders = rpc
            .get_slot_leaders(from_slot, count)
            .await
            .context("getSlotLeaders")?;
        let leaders: Vec<String> = leaders.into_iter().map(|p| p.to_string()).collect();
        self.windows = plan::group_windows(from_slot, &leaders);
        self.fetched_to = from_slot + count;
        Ok(())
    }
}

pub async fn run_session(cfg: RunConfig) -> Result<()> {
    let session = util::new_session_id(cfg.live);
    let dir = cfg.out_dir.join(&session);
    std::fs::create_dir_all(&dir)?;
    println!(
        "session {session} ({})",
        if cfg.live { "LIVE" } else { "dry-run" }
    );

    let rpc = Arc::new(RpcClient::new_with_commitment(
        cfg.read_rpc_url.clone(),
        CommitmentConfig::confirmed(),
    ));
    let send_rpc = Arc::new(RpcClient::new_with_commitment(
        cfg.send_rpc_url.clone(),
        CommitmentConfig::confirmed(),
    ));

    let main = keys::load_main(&cfg.keypair_path)?;
    let payers = keys::derive_payers(&main, cfg.arms.independent_payers as u32)?;
    println!("main payer {}", main.pubkey());

    let mut nodes = snapshot_nodes(&rpc).await?;
    let min_stake = cfg.min_stake_sol.saturating_mul(1_000_000_000);
    nodes.retain(|_, n| n.activated_stake >= min_stake);
    util::write_json(&dir.join("nodes.json"), &nodes.values().collect::<Vec<_>>())?;
    let census = plan::client_census(&nodes);
    util::write_json(&dir.join("census.json"), &census)?;
    for (c, (n, s)) in &census {
        println!(
            "  client {c:<32} validators {n:>5}  stake {:>12} SOL",
            s / 1_000_000_000
        );
    }
    let wanted: Vec<String> = match &cfg.clients {
        Some(list) => list.clone(),
        None => census.keys().cloned().collect(),
    };
    println!("wanted clients: {}", wanted.join(", "));

    let start_slot = rpc
        .get_slot_with_commitment(CommitmentConfig::processed())
        .await?;
    let clock = SlotClock::new(start_slot, cfg.nominal_slot_ms, cfg.clock_bias_slots);
    spawn_clock_sync(rpc.clone(), clock.clone(), Duration::from_millis(1000));

    let committed = util::committed_live_spend(&cfg.out_dir)?;
    let mut cap = SpendCap::with_committed(cfg.spend_cap_lamports, committed);
    println!(
        "spend cap {} lamports, already committed by earlier live sessions {}",
        cfg.spend_cap_lamports, committed
    );
    if cfg.live {
        let bal = rpc.get_balance(&main.pubkey()).await?;
        println!("main balance {bal} lamports");
        if bal < 50_000 {
            bail!("main payer is not funded");
        }
    }

    let tpu = if cfg.live && cfg.arms.allow_tpu {
        Some(TpuPath::start()?)
    } else {
        None
    };

    let mut rng = StdRng::seed_from_u64(cfg.seed);
    util::write_json(
        &dir.join("session.json"),
        &serde_json::json!({
            "session": session, "live": cfg.live, "seed": cfg.seed,
            "read_rpc": util::host_of(&cfg.read_rpc_url), "send_rpc": util::host_of(&cfg.send_rpc_url),
            "start_slot": start_slot, "started_at": chrono::Utc::now(),
            "fee_levels_micro_lamports": FEE_LEVELS, "cu_limit": PROBE_CU_LIMIT,
            "lead_slots": cfg.lead_slots, "rpc_extra_lead_slots": cfg.rpc_extra_lead_slots,
            "max_late_slots": cfg.max_late_slots, "inside_offset_slots": cfg.inside_offset_slots, "clock_bias_slots": cfg.clock_bias_slots,
            "quotas": format!("{:?}", cfg.quotas), "arms": format!("{:?}", cfg.arms),
        }),
    )?;

    let mut cache = LeaderCache {
        windows: vec![],
        fetched_to: 0,
    };
    let mut progress = Progress::default();
    let started = Instant::now();
    let mut window_id: u64 = 0;
    let mut idle_loops = 0u32;

    loop {
        if window_id as usize >= cfg.max_windows {
            println!("stop: max windows");
            break;
        }
        if started.elapsed() > Duration::from_secs(cfg.max_minutes * 60) {
            println!("stop: time limit");
            break;
        }
        if progress.done(&cfg.quotas, &wanted) {
            println!("stop: all client quotas met");
            break;
        }

        let now = clock.estimate_now();
        let earliest = now.ceil() as u64 + cfg.min_prep_slots;
        if cache.fetched_to < earliest + cfg.horizon_slots + 8 {
            cache
                .refresh(&rpc, earliest, (cfg.horizon_slots * 4).max(400))
                .await?;
        }
        let upcoming: Vec<plan::Window> = cache
            .windows
            .iter()
            .filter(|w| w.first_slot >= earliest)
            .cloned()
            .collect();
        let Some(w) = plan::pick_window(
            &upcoming,
            &nodes,
            &wanted,
            &cfg.quotas,
            &progress,
            cfg.horizon_slots,
        )
        .cloned() else {
            idle_loops += 1;
            if idle_loops % 20 == 1 {
                println!(
                    "no eligible window in the next {} slots, waiting",
                    cfg.horizon_slots
                );
            }
            tokio::time::sleep(Duration::from_millis((clock.slot_ms() * 4.0) as u64)).await;
            continue;
        };
        idle_loops = 0;
        let node = nodes
            .get(&w.leader)
            .cloned()
            .context("leader not in node list")?;
        let tpu_addr: Option<SocketAddr> = node.tpu_quic.as_deref().and_then(|a| a.parse().ok());
        let arm = plan::draw_arm(&mut rng, &cfg.arms, tpu_addr.is_some());
        let (levels, order) = plan::draw_fees_and_order(&mut rng, &arm);
        let mut wp = plan::window_plan(&session, window_id, &w, &node, arm.clone());

        let blockhash = rpc.get_latest_blockhash().await?;
        let mut txs: Vec<(Transaction, &Keypair, usize)> = Vec::with_capacity(arm.n_tx);
        let mut window_cost = 0u64;
        for (i, &level) in levels.iter().enumerate() {
            let payer: &Keypair = match arm.contention {
                Contention::Shared => &main,
                Contention::Independent => &payers[i % payers.len()],
            };
            let price = FEE_LEVELS[level];
            let unique = 1 + (window_id % 1000) * 64 + i as u64;
            txs.push((
                txbuild::build_probe(payer, price, unique, blockhash),
                payer,
                level,
            ));
            window_cost += txbuild::max_fee_lamports(price, PROBE_CU_LIMIT);
        }
        if cfg.live {
            if !cap.fits(window_cost) {
                println!(
                    "stop: spend cap would be exceeded by the next window ({window_cost} lamports)"
                );
                break;
            }
            cap.reserve(window_cost)?;
        }

        // Open the QUIC connection early with a transaction the leader will drop.
        if let (Some(t), SendPath::Tpu, Some(addr)) = (&tpu, arm.path, tpu_addr) {
            t.pin(addr);
            let _ = t.send(&txbuild::build_warmup(blockhash)).await;
        }

        let base = match arm.phase {
            Phase::Pre => cfg.lead_slots,
            Phase::Inside => -cfg.inside_offset_slots,
        };
        let lead = base
            + if arm.path == SendPath::Rpc {
                cfg.rpc_extra_lead_slots
            } else {
                0.0
            };
        let target = w.first_slot as f64 - lead;
        if clock.instant_of(target) < Instant::now() {
            // Nothing was sent. The in-memory reservation stays (conservative); the logs that
            // carry the cap to the next session only count transactions that were sent.
            println!(
                "window {} at slot {} came too early, skipped",
                window_id, w.first_slot
            );
            skip_log(&dir, &wp, "too_early", clock.estimate_now())?;
            continue;
        }
        // The clock keeps learning while we wait, so the firing time is recomputed in short
        // steps instead of sleeping once until a time computed long before.
        let fire_at = loop {
            let at = clock.instant_of(target);
            let now = Instant::now();
            if at <= now {
                break now;
            }
            tokio::time::sleep((at - now).min(Duration::from_millis(50))).await;
        };
        let est_at_fire = clock.estimate_now();
        if est_at_fire > target + cfg.max_late_slots {
            println!(
                "window {} at slot {} would be sent late (clock says {:.1}), skipped",
                window_id, w.first_slot, est_at_fire
            );
            skip_log(&dir, &wp, "too_late", est_at_fire)?;
            continue;
        }
        wp.lead_slots = Some(lead);
        wp.est_slot_at_fire = Some(est_at_fire);
        wp.slot_ms_at_fire = Some(clock.slot_ms());

        let spacing = Duration::from_millis(arm.spacing_ms);
        let mut records: Vec<SendRecord> = Vec::with_capacity(arm.n_tx);
        let mut rpc_tasks = Vec::new();
        for (rank, &tx_index) in order.iter().enumerate() {
            let at = fire_at + spacing * rank as u32;
            tokio::time::sleep_until(at.into()).await;
            let (tx, payer, level) = &txs[tx_index];
            let price = FEE_LEVELS[*level];
            let mut rec = SendRecord {
                session: session.clone(),
                window_id,
                leader: w.leader.clone(),
                client_id: wp.client_id.clone(),
                first_slot: w.first_slot,
                tx_index,
                send_rank: rank,
                signature: tx.signatures[0].to_string(),
                payer: payer.pubkey().to_string(),
                fee_level: *level,
                cu_price_micro_lamports: price,
                cu_limit: PROBE_CU_LIMIT,
                max_fee_lamports: txbuild::max_fee_lamports(price, PROBE_CU_LIMIT),
                path: arm.path,
                contention: arm.contention,
                fees: arm.fees,
                spacing_ms: arm.spacing_ms,
                sent_at_unix_ns: util::unix_ns(),
                est_slot_at_send: clock.estimate_now(),
                dry_run: !cfg.live,
                send_error: None,
            };
            if cfg.live {
                match arm.path {
                    SendPath::Tpu => {
                        if let Some(t) = &tpu {
                            if let Err(e) = t.send(tx).await {
                                rec.send_error = Some(e.to_string());
                            }
                        }
                    }
                    SendPath::Rpc => {
                        let r = send_rpc.clone();
                        let tx = tx.clone();
                        rpc_tasks.push((
                            records.len(),
                            tokio::spawn(async move { sender::send_rpc(&r, &tx).await }),
                        ));
                    }
                }
            }
            records.push(rec);
        }
        for (pos, h) in rpc_tasks {
            if let Ok(Err(e)) = h.await {
                records[pos].send_error = Some(e.to_string());
            }
        }

        if !cfg.live {
            // One simulation per window checks that the transaction is valid for this cluster.
            let sim_cfg = RpcSimulateTransactionConfig {
                sig_verify: false,
                replace_recent_blockhash: true,
                ..RpcSimulateTransactionConfig::default()
            };
            let msg = match rpc
                .simulate_transaction_with_config(&txs[order[0]].0, sim_cfg)
                .await
            {
                Ok(r) => match r.value.err {
                    None => "simulate ok".to_string(),
                    Some(e) => format!("simulate error: {e:?}"),
                },
                Err(e) => format!("simulate rpc error: {e}"),
            };
            records[0].send_error = Some(format!("dry-run ({msg})"));
        }

        util::append_jsonl(&dir.join("windows.jsonl"), std::slice::from_ref(&wp))?;
        util::append_jsonl(&dir.join("sends.jsonl"), &records)?;
        progress.record(&wp.client_id, &wp.leader);
        print_window(&wp, &records, cap.committed(), cfg.live);
        window_id += 1;

        // Do not prepare the next window before this one is over.
        let after = clock.instant_of(w.last_slot as f64 + 1.0);
        if after > Instant::now() {
            tokio::time::sleep_until(after.into()).await;
        }
    }

    if let Some(t) = tpu {
        t.shutdown().await;
    }
    println!(
        "session {session} finished: {window_id} windows. Data in {}",
        dir.display()
    );
    println!("next: ordprobe collect --session {session}");
    Ok(())
}

/// Windows that were planned but not sent, so a report can say how many and why.
fn skip_log(dir: &std::path::Path, wp: &WindowPlan, reason: &str, est_slot: f64) -> Result<()> {
    let rec = serde_json::json!({
        "session": wp.session, "leader": wp.leader, "client_id": wp.client_id,
        "first_slot": wp.first_slot, "path": wp.arm.path, "phase": wp.arm.phase, "reason": reason,
        "est_slot": est_slot,
        "at": chrono::Utc::now(),
    });
    util::append_jsonl(&dir.join("skipped.jsonl"), std::slice::from_ref(&rec))
}

fn print_window(wp: &WindowPlan, recs: &[SendRecord], committed: u64, live: bool) {
    let errors = recs
        .iter()
        .filter(|r| r.send_error.is_some() && !r.dry_run)
        .count();
    let offset = wp
        .est_slot_at_fire
        .map(|e| e - wp.first_slot as f64)
        .unwrap_or(f64::NAN);
    println!(
        "window {:>4} slot {} leader {}.. {:<24} {:?}/{:?}/{:?}/{:?} spacing {}ms n={} fired_at {:+.1} send_errors={} committed={}{}",
        wp.window_id,
        wp.first_slot,
        &wp.leader[..6.min(wp.leader.len())],
        wp.client_id,
        wp.arm.path,
        wp.arm.contention,
        wp.arm.fees,
        wp.arm.phase,
        wp.arm.spacing_ms,
        recs.len(),
        offset,
        errors,
        committed,
        if live { "" } else { " (dry-run)" }
    );
    if !live {
        if let Some(m) = recs.first().and_then(|r| r.send_error.clone()) {
            println!("        {m}");
        }
    }
}
