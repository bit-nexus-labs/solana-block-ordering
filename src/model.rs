//! Records written by the probe. Every file is JSON Lines so a partial run is still usable.

use serde::{Deserialize, Serialize};

/// One validator as seen in gossip and in the vote account list at the start of a session.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct NodeInfo {
    pub identity: String,
    pub client_id: Option<String>,
    pub version: Option<String>,
    pub tpu_quic: Option<String>,
    pub activated_stake: u64,
}

/// How the transactions of one window are built and sent.
#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
pub enum Contention {
    /// All transactions share one fee payer, so they write-lock the same account.
    Shared,
    /// Every transaction has its own fee payer, so none of them conflict.
    Independent,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
pub enum FeeDesign {
    /// Fee levels drawn independently of the send order.
    Mixed,
    /// The same fee on every transaction, to see the arrival rule alone.
    Equal,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
pub enum SendPath {
    /// `sendTransaction` on the configured RPC provider.
    Rpc,
    /// QUIC directly to the leader's TPU port, unstaked identity.
    Tpu,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Arm {
    pub contention: Contention,
    pub fees: FeeDesign,
    pub path: SendPath,
    /// Gap between two consecutive sends in milliseconds.
    pub spacing_ms: u64,
    pub n_tx: usize,
}

/// One planned leader window and the experiment assigned to it.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct WindowPlan {
    pub session: String,
    pub window_id: u64,
    pub leader: String,
    pub client_id: String,
    pub version: Option<String>,
    pub tpu_quic: Option<String>,
    pub first_slot: u64,
    pub last_slot: u64,
    pub arm: Arm,
}

/// One transaction as it was built and sent.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SendRecord {
    pub session: String,
    pub window_id: u64,
    pub leader: String,
    pub client_id: String,
    pub first_slot: u64,
    pub tx_index: usize,
    /// Position in the send sequence (0 = sent first).
    pub send_rank: usize,
    pub signature: String,
    pub payer: String,
    pub fee_level: usize,
    pub cu_price_micro_lamports: u64,
    pub cu_limit: u32,
    pub max_fee_lamports: u64,
    pub path: SendPath,
    pub contention: Contention,
    pub fees: FeeDesign,
    pub spacing_ms: u64,
    /// Wall clock at the moment the send call was made, Unix nanoseconds.
    pub sent_at_unix_ns: i128,
    /// The probe's estimate of the cluster slot at that moment.
    pub est_slot_at_send: f64,
    pub dry_run: bool,
    pub send_error: Option<String>,
}

/// Where a sent transaction ended up.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct LandRecord {
    pub session: String,
    pub window_id: u64,
    pub signature: String,
    pub landed: bool,
    pub slot: Option<u64>,
    /// Index of the transaction in the block's transaction list.
    pub index_in_block: Option<usize>,
    pub block_tx_count: Option<usize>,
    /// Identity that received the block's fee reward, i.e. the real producer.
    pub block_producer: Option<String>,
    pub fee_lamports: Option<u64>,
    pub compute_units_consumed: Option<u64>,
    pub tx_error: Option<String>,
}

/// Summary of one block of a probed window, for the supplementary whole-block analysis.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct BlockRecord {
    pub session: String,
    pub window_id: u64,
    pub slot: u64,
    pub scheduled_leader: String,
    pub block_producer: Option<String>,
    pub tx_count: usize,
    pub skipped: bool,
}
