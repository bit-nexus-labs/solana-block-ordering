//! ordprobe: measures how Solana leaders order the probe's own transactions in their blocks.
//!
//! Safe by default: `run`, `fund` and `sweep` only build, sign and simulate unless `--live`
//! is given, and live sending is limited by a spend cap that holds across sessions.

mod collect;
mod keys;
mod model;
mod plan;
mod run;
mod sender;
mod slotclock;
mod spend;
mod txbuild;
mod util;
mod wallet;

use {
    anyhow::Result,
    clap::{Parser, Subcommand},
    solana_commitment_config::CommitmentConfig,
    solana_rpc_client::nonblocking::rpc_client::RpcClient,
    std::path::PathBuf,
};

#[derive(Parser, Debug)]
#[command(name = "ordprobe", version, about)]
struct Cli {
    /// RPC used for reading (cluster nodes, leader schedule, blocks).
    #[arg(long, env = "RPC_URL", default_value = "https://api.devnet.solana.com")]
    rpc_url: String,
    /// RPC used for the `rpc` send path. Defaults to RPC_URL.
    #[arg(long, env = "SEND_RPC_URL")]
    send_rpc_url: Option<String>,
    /// Main keypair file (solana-keygen JSON). Mounted, never committed.
    #[arg(long, env = "KEYPAIR_PATH", default_value = "/keys/probe.json")]
    keypair: PathBuf,
    /// Output directory for all session data.
    #[arg(long, env = "OUT_DIR", default_value = "data")]
    out: PathBuf,
    /// Number of extra fee payers used by the "independent" arm.
    #[arg(long, env = "INDEPENDENT_PAYERS", default_value_t = 8)]
    payers: u32,
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand, Debug)]
enum Cmd {
    /// List validators by client type with stake (no keys needed).
    Census {
        #[arg(long, default_value_t = 0)]
        min_stake_sol: u64,
    },
    /// Create a new main keypair file (refuses to overwrite an existing file).
    Keygen {
        #[arg(long, default_value = "/keys/probe.json")]
        out: PathBuf,
    },
    /// Show balances of the main payer and the derived payers.
    Balance,
    /// Request test SOL for the main payer (devnet/testnet/localnet only).
    Airdrop {
        #[arg(long, default_value_t = 1_000_000_000)]
        lamports: u64,
    },
    /// Fund the derived payers from the main payer (dry-run unless --live).
    Fund {
        #[arg(long, default_value_t = 2_000_000)]
        each_lamports: u64,
        #[arg(long)]
        live: bool,
    },
    /// Return the derived payers' balances to the main payer (dry-run unless --live).
    Sweep {
        #[arg(long)]
        live: bool,
    },
    /// Run a measurement session (dry-run unless --live).
    Run(RunArgs),
    /// Read back where the probes of a session landed.
    Collect {
        #[arg(long)]
        session: String,
        /// Request rate limit for the read RPC.
        #[arg(long, default_value_t = 8.0)]
        rps: f64,
        /// Skip getTransaction for every landed probe (fee, compute units).
        #[arg(long)]
        no_tx_details: bool,
    },
}

#[derive(clap::Args, Debug)]
struct RunArgs {
    /// Names of environment variables that hold extra send RPC URLs (comma separated), e.g.
    /// `RPCFAST_URL,DIGITALENERGY_URL`. Each `rpc` window then draws one provider at random.
    /// The URLs are read from the environment only; logs keep the host name.
    #[arg(long, value_delimiter = ',')]
    extra_send_rpc_env: Vec<String>,
    /// Actually send. Without it the session only builds, signs and simulates.
    #[arg(long)]
    live: bool,
    /// Total spend cap over all live sessions in OUT_DIR, in lamports (max fee of every sent probe).
    #[arg(long, env = "SPEND_CAP_LAMPORTS", default_value_t = 45_000_000)]
    spend_cap_lamports: u64,
    /// Client labels to measure (comma separated). Default: every client seen in gossip.
    #[arg(long, value_delimiter = ',')]
    clients: Option<Vec<String>>,
    /// Ignore validators with less activated stake than this.
    #[arg(long, default_value_t = 10_000)]
    min_stake_sol: u64,
    #[arg(long, default_value_t = 6)]
    windows_per_client: usize,
    #[arg(long, default_value_t = 3)]
    windows_per_validator: usize,
    #[arg(long, default_value_t = 4)]
    validators_per_client: usize,
    #[arg(long, default_value_t = 40)]
    max_windows: usize,
    #[arg(long, default_value_t = 60)]
    max_minutes: u64,
    #[arg(long, default_value_t = 12)]
    shared_n_tx: usize,
    #[arg(long, default_value_t = 2)]
    burst_spacing_ms: u64,
    #[arg(long, default_value_t = 25)]
    spaced_spacing_ms: u64,
    #[arg(long, default_value_t = 0.25)]
    equal_fee_share: f64,
    #[arg(long)]
    no_tpu: bool,
    #[arg(long)]
    no_rpc: bool,
    /// Fire the first send this many slots before the window starts.
    #[arg(long, default_value_t = 0.5)]
    lead_slots: f64,
    /// Extra lead for the `rpc` path. Devnet showed no extra delay against direct QUIC, so 0 by default.
    #[arg(long, default_value_t = 0.0)]
    rpc_extra_lead_slots: f64,
    /// Skip a window when, at firing time, the clock is more than this many slots past the planned point.
    #[arg(long, default_value_t = 1.0)]
    max_late_slots: f64,
    /// Share of windows sent inside the window (about one slot in) instead of just before it.
    #[arg(long, default_value_t = 0.5)]
    inside_share: f64,
    /// Where the first send of an `inside` window is aimed, in slots after the window start.
    #[arg(long, default_value_t = 1.0)]
    inside_offset_slots: f64,
    #[arg(long, default_value_t = 12)]
    min_prep_slots: u64,
    #[arg(long, default_value_t = 160)]
    horizon_slots: u64,
    /// Nominal slot duration used until the clock has learned the real one.
    #[arg(long, default_value_t = 200.0)]
    slot_ms: f64,
    #[arg(long, default_value_t = 1.0)]
    clock_bias_slots: f64,
    /// RNG seed for arm assignment, fee levels and send order. Default: from the clock.
    #[arg(long)]
    seed: Option<u64>,
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.cmd {
        Cmd::Census { min_stake_sol } => {
            let rpc =
                RpcClient::new_with_commitment(cli.rpc_url.clone(), CommitmentConfig::confirmed());
            let mut nodes = run::snapshot_nodes(&rpc).await?;
            nodes.retain(|_, n| n.activated_stake >= min_stake_sol * 1_000_000_000);
            let with_id = nodes.values().filter(|n| n.client_id.is_some()).count();
            let staked = nodes.values().filter(|n| n.activated_stake > 0).count();
            println!(
                "{} gossip nodes ({} report clientId), {} of them staked validators (listed below)",
                nodes.len(),
                with_id,
                staked
            );
            for (c, (n, s)) in plan::client_census(&nodes) {
                println!("{c:<34} {n:>6} validators {:>14} SOL", s / 1_000_000_000);
            }
        }
        Cmd::Keygen { out } => {
            if out.exists() {
                anyhow::bail!("{} already exists, not overwriting", out.display());
            }
            if let Some(parent) = out.parent() {
                std::fs::create_dir_all(parent)?;
            }
            let kp = solana_keypair::Keypair::new();
            solana_keypair::write_keypair_file(&kp, &out).map_err(|e| anyhow::anyhow!("{e}"))?;
            println!("new keypair written to {}", out.display());
            println!("address: {}", solana_signer::Signer::pubkey(&kp));
        }
        Cmd::Airdrop { lamports } => {
            if cli.rpc_url.contains("mainnet") {
                anyhow::bail!("airdrop is not available on mainnet");
            }
            let rpc =
                RpcClient::new_with_commitment(cli.rpc_url.clone(), CommitmentConfig::confirmed());
            let kp = keys::load_main(&cli.keypair)?;
            let sig = rpc
                .request_airdrop(&solana_signer::Signer::pubkey(&kp), lamports)
                .await?;
            println!("airdrop requested: {sig}");
        }
        Cmd::Balance => wallet::balances(&cli.rpc_url, &cli.keypair, cli.payers).await?,
        Cmd::Fund {
            each_lamports,
            live,
        } => wallet::fund(&cli.rpc_url, &cli.keypair, cli.payers, each_lamports, live).await?,
        Cmd::Sweep { live } => wallet::sweep(&cli.rpc_url, &cli.keypair, cli.payers, live).await?,
        Cmd::Collect {
            session,
            rps,
            no_tx_details,
        } => {
            collect::collect_session(&cli.rpc_url, &cli.out, &session, rps, !no_tx_details).await?
        }
        Cmd::Run(a) => {
            let seed = a.seed.unwrap_or_else(|| util::unix_ns() as u64);
            let mut extra_send_rpcs = Vec::new();
            for name in &a.extra_send_rpc_env {
                let url = std::env::var(name)
                    .map_err(|_| anyhow::anyhow!("environment variable {name} is not set"))?;
                let label = name.trim_end_matches("_URL").to_lowercase();
                extra_send_rpcs.push((label, url));
            }
            let cfg = run::RunConfig {
                extra_send_rpcs,
                send_rpc_url: cli
                    .send_rpc_url
                    .clone()
                    .unwrap_or_else(|| cli.rpc_url.clone()),
                read_rpc_url: cli.rpc_url,
                keypair_path: cli.keypair,
                out_dir: cli.out,
                live: a.live,
                spend_cap_lamports: a.spend_cap_lamports,
                clients: a.clients,
                min_stake_sol: a.min_stake_sol,
                quotas: plan::Quotas {
                    per_client: a.windows_per_client,
                    per_validator: a.windows_per_validator,
                    validators_per_client: a.validators_per_client,
                },
                arms: plan::ArmConfig {
                    shared_n_tx: a.shared_n_tx,
                    independent_payers: cli.payers as usize,
                    burst_spacing_ms: a.burst_spacing_ms,
                    spaced_spacing_ms: a.spaced_spacing_ms,
                    equal_fee_share: a.equal_fee_share,
                    allow_tpu: !a.no_tpu,
                    allow_rpc: !a.no_rpc,
                    inside_share: a.inside_share,
                },
                max_windows: a.max_windows,
                max_minutes: a.max_minutes,
                lead_slots: a.lead_slots,
                rpc_extra_lead_slots: a.rpc_extra_lead_slots,
                max_late_slots: a.max_late_slots,
                inside_offset_slots: a.inside_offset_slots,
                min_prep_slots: a.min_prep_slots,
                horizon_slots: a.horizon_slots,
                nominal_slot_ms: a.slot_ms,
                clock_bias_slots: a.clock_bias_slots,
                seed,
            };
            run::run_session(cfg).await?
        }
    }
    Ok(())
}
