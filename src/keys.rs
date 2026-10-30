//! Keys. The main keypair is read from a file given by path (mounted into the container),
//! never from the repository. Extra fee payers for the "independent" arm are derived from
//! the main secret, so they can always be recreated to sweep their balance back.

use {
    anyhow::{anyhow, Context, Result},
    sha2::{Digest, Sha256},
    solana_keypair::{keypair_from_seed, read_keypair_file, Keypair},
    std::path::Path,
};

pub fn load_main(path: &Path) -> Result<Keypair> {
    read_keypair_file(path)
        .map_err(|e| anyhow!("{e}"))
        .with_context(|| format!("cannot read keypair file {}", path.display()))
}

/// Deterministic extra payer number `index` (0-based).
pub fn derive_payer(main: &Keypair, index: u32) -> Result<Keypair> {
    let mut h = Sha256::new();
    h.update(b"ordprobe/independent-payer/v1");
    h.update(main.secret_bytes());
    h.update(index.to_le_bytes());
    let seed = h.finalize();
    keypair_from_seed(&seed).map_err(|e| anyhow!("{e}"))
}

pub fn derive_payers(main: &Keypair, count: u32) -> Result<Vec<Keypair>> {
    (0..count).map(|i| derive_payer(main, i)).collect()
}

#[cfg(test)]
mod tests {
    use {super::*, solana_signer::Signer};

    #[test]
    fn derivation_is_stable_and_distinct() {
        let main = Keypair::new();
        let a0 = derive_payer(&main, 0).unwrap();
        let a0b = derive_payer(&main, 0).unwrap();
        let a1 = derive_payer(&main, 1).unwrap();
        assert_eq!(a0.pubkey(), a0b.pubkey());
        assert_ne!(a0.pubkey(), a1.pubkey());
        assert_ne!(a0.pubkey(), main.pubkey());
    }
}
