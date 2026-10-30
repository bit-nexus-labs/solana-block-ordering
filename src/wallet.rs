//! Balances, funding of the extra payers, and sweeping them back.
//! Funding and sweeping only send when `--live` is given.

use {
    crate::{keys, txbuild::LAMPORTS_PER_SIGNATURE},
    anyhow::{bail, Result},
    solana_commitment_config::CommitmentConfig,
    solana_keypair::Keypair,
    solana_message::Message,
    solana_rpc_client::nonblocking::rpc_client::RpcClient,
    solana_signer::Signer,
    solana_system_interface::instruction as system_instruction,
    solana_transaction::Transaction,
    std::path::Path,
};

pub async fn balances(rpc_url: &str, keypair: &Path, payers: u32) -> Result<()> {
    let rpc = RpcClient::new_with_commitment(rpc_url.to_string(), CommitmentConfig::confirmed());
    let main = keys::load_main(keypair)?;
    let b = rpc.get_balance(&main.pubkey()).await?;
    println!("main    {}  {:>12} lamports ({:.6} SOL)", main.pubkey(), b, b as f64 / 1e9);
    let mut total = b;
    for (i, p) in keys::derive_payers(&main, payers)?.iter().enumerate() {
        let b = rpc.get_balance(&p.pubkey()).await?;
        total += b;
        println!("payer{i:<2} {}  {:>12} lamports", p.pubkey(), b);
    }
    println!("total   {:>57} lamports ({:.6} SOL)", total, total as f64 / 1e9);
    Ok(())
}

pub async fn fund(rpc_url: &str, keypair: &Path, payers: u32, each_lamports: u64, live: bool) -> Result<()> {
    let rpc = RpcClient::new_with_commitment(rpc_url.to_string(), CommitmentConfig::confirmed());
    let main = keys::load_main(keypair)?;
    let rent = rpc.get_minimum_balance_for_rent_exemption(0).await?;
    if each_lamports < rent {
        bail!("each payer needs at least the rent-exempt minimum {rent} lamports");
    }
    let mut ixs = Vec::new();
    for p in keys::derive_payers(&main, payers)? {
        let have = rpc.get_balance(&p.pubkey()).await?;
        if have < each_lamports {
            ixs.push(system_instruction::transfer(&main.pubkey(), &p.pubkey(), each_lamports - have));
        }
    }
    if ixs.is_empty() {
        println!("all {payers} payers already hold at least {each_lamports} lamports");
        return Ok(());
    }
    let total: u64 = ixs.len() as u64 * each_lamports;
    println!("funding {} payers, up to {} lamports in total", ixs.len(), total);
    if !live {
        println!("dry-run: nothing sent. Add --live to fund.");
        return Ok(());
    }
    let bh = rpc.get_latest_blockhash().await?;
    let tx = Transaction::new(&[&main], Message::new(&ixs, Some(&main.pubkey())), bh);
    let sig = rpc.send_and_confirm_transaction(&tx).await?;
    println!("funded: {sig}");
    Ok(())
}

pub async fn sweep(rpc_url: &str, keypair: &Path, payers: u32, live: bool) -> Result<()> {
    let rpc = RpcClient::new_with_commitment(rpc_url.to_string(), CommitmentConfig::confirmed());
    let main = keys::load_main(keypair)?;
    let derived: Vec<Keypair> = keys::derive_payers(&main, payers)?;
    for p in &derived {
        let have = rpc.get_balance(&p.pubkey()).await?;
        if have <= LAMPORTS_PER_SIGNATURE {
            continue;
        }
        let amount = have - LAMPORTS_PER_SIGNATURE;
        println!("sweep {} -> main: {amount} lamports", p.pubkey());
        if !live {
            continue;
        }
        let bh = rpc.get_latest_blockhash().await?;
        let ix = system_instruction::transfer(&p.pubkey(), &main.pubkey(), amount);
        let tx = Transaction::new(&[p], Message::new(&[ix], Some(&p.pubkey())), bh);
        match rpc.send_and_confirm_transaction(&tx).await {
            Ok(sig) => println!("  {sig}"),
            Err(e) => println!("  failed: {e}"),
        }
    }
    if !live {
        println!("dry-run: nothing sent. Add --live to sweep.");
    }
    Ok(())
}
