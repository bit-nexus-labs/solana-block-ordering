//! Who to measure and with which experiment.
//!
//! Selection works on the live leader schedule: the probe looks a few hundred slots ahead,
//! groups slots into leader windows and picks the next window whose client type still
//! needs data. The experiment arm of each window is drawn at random from a seeded RNG,
//! independently of who the leader is, so arm effects and leader effects are not confounded.

use {
    crate::{
        model::{Arm, Contention, FeeDesign, NodeInfo, Phase, SendPath, WindowPlan},
        txbuild::FEE_LEVELS,
    },
    rand::{seq::SliceRandom, Rng},
    std::collections::{BTreeMap, HashMap},
};

/// Slots per leader window (NUM_CONSECUTIVE_LEADER_SLOTS in Agave).
pub const WINDOW_SLOTS: u64 = 4;

/// Client label for a node. `clientId` from gossip is preferred. Older RPC nodes do not
/// return it; then the version string is the only hint and the label says so.
pub fn client_label(node: &NodeInfo) -> String {
    if let Some(id) = node.client_id.as_deref() {
        // Numeric codes that the RPC node cannot name are grouped, so one label stays stable.
        if id.starts_with("Unknown") {
            return "Unknown".to_string();
        }
        if !id.is_empty() {
            return id.to_string();
        }
    }
    let major = node
        .version
        .as_deref()
        .and_then(|v| v.split('.').next()?.parse::<u32>().ok());
    match node.version.as_deref() {
        // Firedancer versions start with 0. (Frankendancer) or with a two-digit year.
        Some(_) if matches!(major, Some(0) | Some(20..)) => {
            "Firedancer-family(version-only)".to_string()
        }
        Some(_) => "Agave-family(version-only)".to_string(),
        None => "Unknown".to_string(),
    }
}

#[derive(Clone, Debug)]
pub struct Window {
    pub leader: String,
    pub first_slot: u64,
    pub last_slot: u64,
}

/// Groups a run of slot leaders (index 0 = `start_slot`) into windows aligned to
/// `WINDOW_SLOTS`. Partial windows at the edges are dropped.
pub fn group_windows(start_slot: u64, leaders: &[String]) -> Vec<Window> {
    let mut out = Vec::new();
    let mut s = start_slot.div_ceil(WINDOW_SLOTS) * WINDOW_SLOTS;
    let end = start_slot + leaders.len() as u64;
    while s + WINDOW_SLOTS <= end {
        let i = (s - start_slot) as usize;
        let leader = &leaders[i];
        let same = leaders[i..i + WINDOW_SLOTS as usize]
            .iter()
            .all(|l| l == leader);
        if same {
            out.push(Window {
                leader: leader.clone(),
                first_slot: s,
                last_slot: s + WINDOW_SLOTS - 1,
            });
        }
        s += WINDOW_SLOTS;
    }
    out
}

#[derive(Clone, Debug)]
pub struct Quotas {
    /// Windows wanted per client label.
    pub per_client: usize,
    /// Windows at most per validator, so one big validator does not stand for its client.
    pub per_validator: usize,
    /// Distinct validators wanted per client label.
    pub validators_per_client: usize,
}

#[derive(Default, Debug)]
pub struct Progress {
    pub by_client: HashMap<String, usize>,
    pub by_validator: HashMap<String, usize>,
    pub validators_by_client: HashMap<String, Vec<String>>,
}

impl Progress {
    pub fn record(&mut self, client: &str, leader: &str) {
        *self.by_client.entry(client.to_string()).or_default() += 1;
        *self.by_validator.entry(leader.to_string()).or_default() += 1;
        let v = self
            .validators_by_client
            .entry(client.to_string())
            .or_default();
        if !v.iter().any(|x| x == leader) {
            v.push(leader.to_string());
        }
    }

    /// How much a window of this leader would still help, 0 = not at all.
    pub fn need(&self, q: &Quotas, client: &str, leader: &str) -> f64 {
        let per_v = self.by_validator.get(leader).copied().unwrap_or(0);
        if per_v >= q.per_validator {
            return 0.0;
        }
        let per_c = self.by_client.get(client).copied().unwrap_or(0);
        if per_c >= q.per_client {
            return 0.0;
        }
        let vals = self.validators_by_client.get(client);
        let is_new = vals.map(|v| !v.iter().any(|x| x == leader)).unwrap_or(true);
        let n_vals = vals.map(|v| v.len()).unwrap_or(0);
        if is_new && n_vals >= q.validators_per_client {
            return 0.0;
        }
        let client_gap = 1.0 - per_c as f64 / q.per_client as f64;
        let new_bonus = if is_new { 0.5 } else { 0.0 };
        client_gap + new_bonus
    }

    pub fn done(&self, q: &Quotas, clients: &[String]) -> bool {
        clients
            .iter()
            .all(|c| self.by_client.get(c).copied().unwrap_or(0) >= q.per_client)
    }
}

/// Picks the window to probe next among `windows` (already filtered to be far enough ahead).
/// Prefers the client that is furthest from its quota; among equals, the earliest window.
pub fn pick_window<'a>(
    windows: &'a [Window],
    nodes: &HashMap<String, NodeInfo>,
    wanted_clients: &[String],
    q: &Quotas,
    p: &Progress,
    horizon_slots: u64,
) -> Option<&'a Window> {
    let first = windows.first()?.first_slot;
    let mut best: Option<(&Window, f64)> = None;
    for w in windows
        .iter()
        .take_while(|w| w.first_slot <= first + horizon_slots)
    {
        let Some(node) = nodes.get(&w.leader) else {
            continue;
        };
        let client = client_label(node);
        if !wanted_clients.iter().any(|c| c == &client) {
            continue;
        }
        let need = p.need(q, &client, &w.leader);
        if need <= 0.0 {
            continue;
        }
        if best.map(|(_, b)| need > b + 1e-9).unwrap_or(true) {
            best = Some((w, need));
        }
    }
    best.map(|(w, _)| w)
}

#[derive(Clone, Debug)]
pub struct ArmConfig {
    pub shared_n_tx: usize,
    pub independent_payers: usize,
    pub burst_spacing_ms: u64,
    pub spaced_spacing_ms: u64,
    pub equal_fee_share: f64,
    pub allow_tpu: bool,
    pub allow_rpc: bool,
    /// Share of windows sent inside the window instead of just before it.
    pub inside_share: f64,
}

/// Draws the experiment for one window.
pub fn draw_arm<R: Rng>(rng: &mut R, cfg: &ArmConfig, leader_has_tpu: bool) -> Arm {
    let contention = if cfg.independent_payers > 0 && rng.gen_bool(0.5) {
        Contention::Independent
    } else {
        Contention::Shared
    };
    let fees = if rng.gen_bool(cfg.equal_fee_share) {
        FeeDesign::Equal
    } else {
        FeeDesign::Mixed
    };
    let can_tpu = cfg.allow_tpu && leader_has_tpu;
    let path = match (cfg.allow_rpc, can_tpu) {
        (true, true) => {
            if rng.gen_bool(0.5) {
                SendPath::Tpu
            } else {
                SendPath::Rpc
            }
        }
        (false, true) => SendPath::Tpu,
        _ => SendPath::Rpc,
    };
    let spacing_ms = if rng.gen_bool(0.5) {
        cfg.burst_spacing_ms
    } else {
        cfg.spaced_spacing_ms
    };
    let n_tx = match contention {
        Contention::Shared => cfg.shared_n_tx,
        Contention::Independent => cfg.independent_payers,
    };
    let phase = if rng.gen_bool(cfg.inside_share.clamp(0.0, 1.0)) {
        Phase::Inside
    } else {
        Phase::Pre
    };
    Arm {
        contention,
        fees,
        path,
        spacing_ms,
        n_tx,
        phase,
    }
}

/// Fee level of every transaction (by transaction index) and the send order
/// (send position -> transaction index). The two are drawn independently.
pub fn draw_fees_and_order<R: Rng>(rng: &mut R, arm: &Arm) -> (Vec<usize>, Vec<usize>) {
    let n = arm.n_tx;
    let levels: Vec<usize> = match arm.fees {
        FeeDesign::Equal => vec![3; n],
        FeeDesign::Mixed => {
            let mut v: Vec<usize> = (0..n).map(|i| i % FEE_LEVELS.len()).collect();
            v.shuffle(rng);
            v
        }
    };
    let mut order: Vec<usize> = (0..n).collect();
    order.shuffle(rng);
    (levels, order)
}

pub fn window_plan(
    session: &str,
    window_id: u64,
    w: &Window,
    node: &NodeInfo,
    arm: Arm,
) -> WindowPlan {
    WindowPlan {
        session: session.to_string(),
        window_id,
        leader: w.leader.clone(),
        client_id: client_label(node),
        version: node.version.clone(),
        tpu_quic: node.tpu_quic.clone(),
        first_slot: w.first_slot,
        last_slot: w.last_slot,
        arm,
        lead_slots: None,
        est_slot_at_fire: None,
        slot_ms_at_fire: None,
    }
}

/// Counts of client labels weighted by stake, for the report.
pub fn client_census(nodes: &HashMap<String, NodeInfo>) -> BTreeMap<String, (usize, u64)> {
    let mut m: BTreeMap<String, (usize, u64)> = BTreeMap::new();
    for n in nodes.values().filter(|n| n.activated_stake > 0) {
        let e = m.entry(client_label(n)).or_default();
        e.0 += 1;
        e.1 += n.activated_stake;
    }
    m
}

#[cfg(test)]
mod tests {
    use {super::*, rand::rngs::StdRng, rand::SeedableRng};

    fn node(id: &str, client: &str) -> NodeInfo {
        NodeInfo {
            identity: id.into(),
            client_id: Some(client.into()),
            version: Some("4.0.0".into()),
            tpu_quic: Some("1.2.3.4:8009".into()),
            activated_stake: 1,
        }
    }

    #[test]
    fn groups_aligned_windows_and_drops_partial() {
        let mut leaders = vec![];
        for l in ["Z", "Z", "A", "A", "A", "A", "B", "B", "B", "B", "C", "C"] {
            leaders.push(l.to_string());
        }
        // slots 102..113: 102-103 are the tail of Z's window, 112-113 a partial C window
        let ws = group_windows(102, &leaders);
        assert_eq!(ws.len(), 2);
        assert_eq!(ws[0].first_slot, 104);
        assert_eq!(ws[0].leader, "A");
        assert_eq!(ws[1].first_slot, 108);
        assert_eq!(ws[1].leader, "B");
    }

    #[test]
    fn picks_client_furthest_from_quota() {
        let mut nodes = HashMap::new();
        nodes.insert("A".into(), node("A", "Agave"));
        nodes.insert("F".into(), node("F", "Firedancer"));
        let ws = vec![
            Window {
                leader: "A".into(),
                first_slot: 100,
                last_slot: 103,
            },
            Window {
                leader: "F".into(),
                first_slot: 104,
                last_slot: 107,
            },
        ];
        let q = Quotas {
            per_client: 4,
            per_validator: 2,
            validators_per_client: 3,
        };
        let mut p = Progress::default();
        p.record("Agave", "A");
        p.record("Agave", "A2");
        let clients = vec!["Agave".to_string(), "Firedancer".to_string()];
        let w = pick_window(&ws, &nodes, &clients, &q, &p, 50).unwrap();
        assert_eq!(w.leader, "F");
    }

    #[test]
    fn respects_per_validator_cap() {
        let mut nodes = HashMap::new();
        nodes.insert("A".into(), node("A", "Agave"));
        let ws = vec![Window {
            leader: "A".into(),
            first_slot: 100,
            last_slot: 103,
        }];
        let q = Quotas {
            per_client: 10,
            per_validator: 1,
            validators_per_client: 5,
        };
        let mut p = Progress::default();
        p.record("Agave", "A");
        assert!(pick_window(&ws, &nodes, &["Agave".to_string()], &q, &p, 50).is_none());
    }

    #[test]
    fn fees_are_balanced_and_order_is_a_permutation() {
        let mut rng = StdRng::seed_from_u64(7);
        let arm = Arm {
            contention: Contention::Shared,
            fees: FeeDesign::Mixed,
            path: SendPath::Rpc,
            spacing_ms: 2,
            n_tx: 12,
            phase: Phase::Pre,
        };
        let (levels, order) = draw_fees_and_order(&mut rng, &arm);
        for l in 0..FEE_LEVELS.len() {
            assert_eq!(levels.iter().filter(|&&x| x == l).count(), 2);
        }
        let mut sorted = order.clone();
        sorted.sort();
        assert_eq!(sorted, (0..12).collect::<Vec<_>>());
    }

    #[test]
    fn version_only_label() {
        let mut n = node("X", "");
        n.client_id = None;
        n.version = Some("0.708.20306".into());
        assert_eq!(client_label(&n), "Firedancer-family(version-only)");
    }
}
