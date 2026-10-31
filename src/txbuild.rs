//! Probe transactions: a self-transfer with an explicit compute budget.
//!
//! Every probe transaction has the same instructions and the same cost, so the only
//! thing that differs between two of them is the compute-unit price (the priority fee)
//! and the lamport amount that makes the signature unique.

use {
    solana_compute_budget_interface::ComputeBudgetInstruction, solana_hash::Hash,
    solana_keypair::Keypair, solana_message::Message, solana_signer::Signer,
    solana_system_interface::instruction as system_instruction, solana_transaction::Transaction,
};

/// Signature fee charged per signature, in lamports.
pub const LAMPORTS_PER_SIGNATURE: u64 = 5_000;

/// Compute-unit limit requested by every probe transaction. A self-transfer plus two
/// compute budget instructions uses well under this.
pub const PROBE_CU_LIMIT: u32 = 1_000;

/// Compute-unit prices in micro-lamports per CU. Level 0 pays no priority fee.
pub const FEE_LEVELS: [u64; 6] = [0, 1_000, 10_000, 100_000, 1_000_000, 5_000_000];

/// Priority fee in lamports for a given price and limit, rounded up like the runtime does.
pub fn priority_fee_lamports(cu_price_micro_lamports: u64, cu_limit: u32) -> u64 {
    let micro = (cu_price_micro_lamports as u128) * (cu_limit as u128);
    micro.div_ceil(1_000_000) as u64
}

/// Upper bound on what one probe transaction can cost if it lands.
pub fn max_fee_lamports(cu_price_micro_lamports: u64, cu_limit: u32) -> u64 {
    LAMPORTS_PER_SIGNATURE + priority_fee_lamports(cu_price_micro_lamports, cu_limit)
}

/// Builds and signs one probe transaction.
///
/// `unique` is the lamport amount moved from the payer to itself. It changes the message,
/// so two probes with the same fee still get different signatures.
pub fn build_probe(
    payer: &Keypair,
    cu_price_micro_lamports: u64,
    unique: u64,
    blockhash: Hash,
) -> Transaction {
    let payer_key = payer.pubkey();
    let instructions = vec![
        ComputeBudgetInstruction::set_compute_unit_limit(PROBE_CU_LIMIT),
        ComputeBudgetInstruction::set_compute_unit_price(cu_price_micro_lamports),
        system_instruction::transfer(&payer_key, &payer_key, unique),
    ];
    let message = Message::new(&instructions, Some(&payer_key));
    Transaction::new(&[payer], message, blockhash)
}

/// A transaction that any leader drops before execution because its fee payer is an
/// unfunded random key. It costs nothing and is only used to open the QUIC connection
/// to a leader before the measured transactions are sent.
pub fn build_warmup(blockhash: Hash) -> Transaction {
    let throwaway = Keypair::new();
    build_probe(&throwaway, 0, 1, blockhash)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn priority_fee_rounds_up() {
        assert_eq!(priority_fee_lamports(0, PROBE_CU_LIMIT), 0);
        assert_eq!(priority_fee_lamports(1_000, PROBE_CU_LIMIT), 1);
        assert_eq!(priority_fee_lamports(1_001, PROBE_CU_LIMIT), 2);
        assert_eq!(priority_fee_lamports(5_000_000, PROBE_CU_LIMIT), 5_000);
    }

    #[test]
    fn max_fee_includes_signature() {
        assert_eq!(max_fee_lamports(0, PROBE_CU_LIMIT), 5_000);
        assert_eq!(max_fee_lamports(5_000_000, PROBE_CU_LIMIT), 10_000);
    }

    #[test]
    fn probes_with_same_fee_have_different_signatures() {
        let payer = Keypair::new();
        let hash = Hash::new_unique();
        let a = build_probe(&payer, 1_000, 1, hash);
        let b = build_probe(&payer, 1_000, 2, hash);
        assert_ne!(a.signatures[0], b.signatures[0]);
        assert_eq!(a.message.account_keys.len(), b.message.account_keys.len());
    }
}
