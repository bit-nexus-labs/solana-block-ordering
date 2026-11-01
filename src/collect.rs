//! Reads back where every probe landed.
//!
//! For each window the blocks of its four slots (plus a few slots on each side, because a
//! probe can land early or late) are fetched with `transactionDetails: "signatures"`, which
//! gives the exact order of transactions in the block at low cost. The block's fee reward
//! names the identity that really produced it, so a block made by someone other than the
//! scheduled leader is detected instead of silently mixed in.

use {
    crate::{
        model::{BlockRecord, LandRecord, SendRecord, WindowPlan},
        util,
    },
    anyhow::{Context, Result},
    serde_json::{json, Value},
    solana_commitment_config::CommitmentConfig,
    solana_rpc_client::nonblocking::rpc_client::RpcClient,
    solana_rpc_client_api::request::RpcRequest,
    solana_signature::Signature,
    std::{
        collections::{BTreeMap, HashMap},
        path::Path,
        str::FromStr,
        time::Duration,
    },
};

pub struct Throttle {
    gap: Duration,
    last: tokio::time::Instant,
}

impl Throttle {
    pub fn new(per_second: f64) -> Self {
        Self {
            gap: Duration::from_secs_f64(1.0 / per_second.max(0.1)),
            last: tokio::time::Instant::now(),
        }
    }

    pub async fn wait(&mut self) {
        let next = self.last + self.gap;
        tokio::time::sleep_until(next).await;
        self.last = tokio::time::Instant::now();
    }
}

#[derive(Clone, Debug)]
pub struct Block {
    pub skipped: bool,
    pub signatures: Vec<String>,
    pub producer: Option<String>,
}

pub async fn fetch_block(rpc: &RpcClient, slot: u64, th: &mut Throttle) -> Result<Block> {
    // Mainnet carries version-1 transactions; a block that holds one is refused (-32015) unless
    // the request allows version 1. An RPC node that does not know version 1 rejects the
    // parameter instead, and then the request falls back to version 0.
    let mut max_version = 1;
    for attempt in 0..5 {
        let cfg = json!({
            "encoding": "json",
            "transactionDetails": "signatures",
            "rewards": true,
            "commitment": "confirmed",
            "maxSupportedTransactionVersion": max_version
        });
        th.wait().await;
        match rpc
            .send::<Value>(RpcRequest::GetBlock, json!([slot, cfg]))
            .await
        {
            Ok(v) => {
                let signatures = v["signatures"]
                    .as_array()
                    .map(|a| {
                        a.iter()
                            .filter_map(|s| s.as_str().map(String::from))
                            .collect()
                    })
                    .unwrap_or_default();
                let producer = v["rewards"].as_array().and_then(|rs| {
                    rs.iter()
                        .find(|r| r["rewardType"].as_str() == Some("Fee"))
                        .and_then(|r| r["pubkey"].as_str().map(String::from))
                });
                return Ok(Block {
                    skipped: false,
                    signatures,
                    producer,
                });
            }
            Err(e) => {
                let msg = e.to_string();
                // -32007 skipped, -32009 missing in long-term storage, -32004 not available yet
                if msg.contains("-32007")
                    || msg.contains("-32009")
                    || msg.to_lowercase().contains("skipped")
                {
                    return Ok(Block {
                        skipped: true,
                        signatures: vec![],
                        producer: None,
                    });
                }
                if max_version == 1
                    && (msg.contains("-32602") || msg.contains("maxSupportedTransactionVersion"))
                {
                    max_version = 0;
                    continue;
                }
                if attempt == 4 {
                    return Err(e).with_context(|| format!("getBlock {slot}"));
                }
                tokio::time::sleep(Duration::from_millis(800 * (attempt + 1))).await;
            }
        }
    }
    unreachable!()
}

pub async fn collect_session(
    rpc_url: &str,
    out_dir: &Path,
    session: &str,
    rps: f64,
    tx_details: bool,
) -> Result<()> {
    let dir = out_dir.join(session);
    let sends: Vec<SendRecord> =
        util::read_jsonl(&dir.join("sends.jsonl")).context("sends.jsonl")?;
    let windows: Vec<WindowPlan> =
        util::read_jsonl(&dir.join("windows.jsonl")).context("windows.jsonl")?;
    if sends.iter().all(|s| s.dry_run) {
        println!("session {session} is a dry run: nothing was sent, nothing to collect");
        return Ok(());
    }
    let rpc = RpcClient::new_with_commitment(rpc_url.to_string(), CommitmentConfig::confirmed());
    let mut th = Throttle::new(rps);

    // 1. Landing slot of every probe.
    let sigs: Vec<Signature> = sends
        .iter()
        .map(|s| Signature::from_str(&s.signature))
        .collect::<Result<_, _>>()?;
    let mut status: HashMap<String, (u64, Option<String>)> = HashMap::new();
    for chunk in sigs.chunks(200) {
        th.wait().await;
        let r = rpc
            .get_signature_statuses_with_history(chunk)
            .await
            .context("getSignatureStatuses")?;
        for (sig, st) in chunk.iter().zip(r.value) {
            if let Some(st) = st {
                status.insert(sig.to_string(), (st.slot, st.err.map(|e| format!("{e:?}"))));
            }
        }
    }
    println!("{} of {} probes landed", status.len(), sends.len());

    // 2. Blocks around every window, plus any other slot a probe landed in.
    let mut slots: BTreeMap<u64, (u64, String)> = BTreeMap::new(); // slot -> (window, scheduled leader)
    for w in &windows {
        for s in w.first_slot.saturating_sub(2)..=w.last_slot + 4 {
            let leader = if (w.first_slot..=w.last_slot).contains(&s) {
                w.leader.clone()
            } else {
                String::new()
            };
            slots.entry(s).or_insert((w.window_id, leader));
        }
    }
    let window_of: HashMap<String, u64> = sends
        .iter()
        .map(|s| (s.signature.clone(), s.window_id))
        .collect();
    for (sig, (slot, _)) in &status {
        slots
            .entry(*slot)
            .or_insert((window_of[sig], String::new()));
    }
    let mut blocks: HashMap<u64, Block> = HashMap::new();
    let mut block_rows = Vec::new();
    for (i, (slot, (wid, leader))) in slots.iter().enumerate() {
        let b = fetch_block(&rpc, *slot, &mut th).await?;
        block_rows.push(BlockRecord {
            session: session.to_string(),
            window_id: *wid,
            slot: *slot,
            scheduled_leader: leader.clone(),
            block_producer: b.producer.clone(),
            tx_count: b.signatures.len(),
            skipped: b.skipped,
        });
        blocks.insert(*slot, b);
        if i % 50 == 0 {
            println!("  blocks {}/{}", i + 1, slots.len());
        }
    }

    // 3. Position of every landed probe, and optionally its fee and compute units.
    let mut land_rows = Vec::with_capacity(sends.len());
    for s in &sends {
        let mut row = LandRecord {
            session: session.to_string(),
            window_id: s.window_id,
            signature: s.signature.clone(),
            landed: false,
            slot: None,
            index_in_block: None,
            block_tx_count: None,
            block_producer: None,
            fee_lamports: None,
            compute_units_consumed: None,
            tx_error: None,
        };
        if let Some((slot, err)) = status.get(&s.signature) {
            row.landed = true;
            row.slot = Some(*slot);
            row.tx_error = err.clone();
            if let Some(b) = blocks.get(slot) {
                row.index_in_block = b.signatures.iter().position(|x| x == &s.signature);
                row.block_tx_count = Some(b.signatures.len());
                row.block_producer = b.producer.clone();
            }
            if tx_details {
                th.wait().await;
                let cfg = json!({"encoding": "json", "commitment": "confirmed", "maxSupportedTransactionVersion": 0});
                if let Ok(v) = rpc
                    .send::<Value>(RpcRequest::GetTransaction, json!([s.signature, cfg]))
                    .await
                {
                    row.fee_lamports = v["meta"]["fee"].as_u64();
                    row.compute_units_consumed = v["meta"]["computeUnitsConsumed"].as_u64();
                }
            }
        }
        land_rows.push(row);
    }

    let _ = std::fs::remove_file(dir.join("landed.jsonl"));
    let _ = std::fs::remove_file(dir.join("blocks.jsonl"));
    util::append_jsonl(&dir.join("landed.jsonl"), &land_rows)?;
    util::append_jsonl(&dir.join("blocks.jsonl"), &block_rows)?;
    let landed = land_rows.iter().filter(|r| r.landed).count();
    let fees: u64 = land_rows.iter().filter_map(|r| r.fee_lamports).sum();
    println!(
        "collected: {landed} landed, {} blocks, fees paid {fees} lamports",
        block_rows.len()
    );
    println!("files: {}/landed.jsonl, blocks.jsonl", dir.display());
    Ok(())
}
