//! Spend cap. Every transaction is charged at its maximum possible fee *before* it is sent,
//! so the cap holds even if every probe lands.

use anyhow::{bail, Result};

#[derive(Debug)]
pub struct SpendCap {
    cap_lamports: u64,
    committed_lamports: u64,
}

impl SpendCap {
    #[allow(dead_code)]
    pub fn new(cap_lamports: u64) -> Self {
        Self { cap_lamports, committed_lamports: 0 }
    }

    /// Restores the amount already committed by earlier sessions (read from their logs).
    pub fn with_committed(cap_lamports: u64, committed_lamports: u64) -> Self {
        Self { cap_lamports, committed_lamports }
    }

    pub fn committed(&self) -> u64 {
        self.committed_lamports
    }

    #[allow(dead_code)]
    pub fn remaining(&self) -> u64 {
        self.cap_lamports.saturating_sub(self.committed_lamports)
    }

    /// Reserves `lamports` or refuses if that would cross the cap.
    pub fn reserve(&mut self, lamports: u64) -> Result<()> {
        let next = self.committed_lamports.saturating_add(lamports);
        if next > self.cap_lamports {
            bail!(
                "spend cap reached: committed {} + {} > cap {} lamports",
                self.committed_lamports,
                lamports,
                self.cap_lamports
            );
        }
        self.committed_lamports = next;
        Ok(())
    }

    /// Returns `true` if a whole window of `lamports` still fits.
    pub fn fits(&self, lamports: u64) -> bool {
        self.committed_lamports.saturating_add(lamports) <= self.cap_lamports
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn refuses_past_cap() {
        let mut cap = SpendCap::new(10_000);
        cap.reserve(6_000).unwrap();
        assert!(cap.fits(4_000));
        assert!(!cap.fits(4_001));
        assert!(cap.reserve(4_001).is_err());
        assert_eq!(cap.committed(), 6_000);
        cap.reserve(4_000).unwrap();
        assert_eq!(cap.remaining(), 0);
    }

    #[test]
    fn carries_over_between_sessions() {
        let cap = SpendCap::with_committed(50_000_000, 49_999_000);
        assert!(!cap.fits(5_000));
    }
}
