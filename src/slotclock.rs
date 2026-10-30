//! Local estimate of the cluster slot.
//!
//! The probe cannot afford to poll the RPC every few milliseconds, so it anchors on a
//! `getSlot` answer and extrapolates with the measured slot duration. A background task
//! re-anchors every `resync` interval. The estimate is only used to decide *when* to fire;
//! the real landing slot of every transaction is read back from the chain afterwards.

use {
    std::sync::{Arc, Mutex},
    std::time::{Duration, Instant},
};

#[derive(Clone, Debug)]
struct Anchor {
    slot: u64,
    at: Instant,
    slot_ms: f64,
}

#[derive(Clone, Debug)]
pub struct SlotClock {
    inner: Arc<Mutex<Anchor>>,
    /// Added to every estimate. `getSlot` with processed commitment usually trails the
    /// slot being produced, so a small positive bias is expected.
    bias_slots: f64,
}

impl SlotClock {
    pub fn new(slot: u64, nominal_slot_ms: f64, bias_slots: f64) -> Self {
        Self {
            inner: Arc::new(Mutex::new(Anchor { slot, at: Instant::now(), slot_ms: nominal_slot_ms })),
            bias_slots,
        }
    }

    /// Feeds a fresh observation. The slot duration is smoothed so one slow RPC answer
    /// does not throw the estimate off.
    pub fn observe(&self, slot: u64, at: Instant) {
        let mut a = self.inner.lock().unwrap();
        if slot > a.slot {
            let elapsed_ms = at.saturating_duration_since(a.at).as_secs_f64() * 1000.0;
            let per_slot = elapsed_ms / (slot - a.slot) as f64;
            if per_slot.is_finite() && (50.0..2000.0).contains(&per_slot) {
                a.slot_ms = 0.8 * a.slot_ms + 0.2 * per_slot;
            }
        }
        a.slot = slot;
        a.at = at;
    }

    pub fn slot_ms(&self) -> f64 {
        self.inner.lock().unwrap().slot_ms
    }

    pub fn estimate_at(&self, t: Instant) -> f64 {
        let a = self.inner.lock().unwrap();
        let dt_ms = if t >= a.at {
            t.duration_since(a.at).as_secs_f64() * 1000.0
        } else {
            -(a.at.duration_since(t).as_secs_f64() * 1000.0)
        };
        a.slot as f64 + dt_ms / a.slot_ms + self.bias_slots
    }

    pub fn estimate_now(&self) -> f64 {
        self.estimate_at(Instant::now())
    }

    /// Instant at which the estimate reaches `slot` (may be in the past).
    pub fn instant_of(&self, slot: f64) -> Instant {
        let a = self.inner.lock().unwrap();
        let slots_ahead = slot - self.bias_slots - a.slot as f64;
        let ms = slots_ahead * a.slot_ms;
        if ms >= 0.0 {
            a.at + Duration::from_secs_f64(ms / 1000.0)
        } else {
            a.at.checked_sub(Duration::from_secs_f64(-ms / 1000.0)).unwrap_or(a.at)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extrapolates_and_learns_slot_duration() {
        let t0 = Instant::now();
        let clock = SlotClock::new(1_000, 400.0, 0.0);
        clock.observe(1_000, t0);
        clock.observe(1_010, t0 + Duration::from_millis(2_000));
        assert!(clock.slot_ms() < 400.0 && clock.slot_ms() > 200.0);
        let est = clock.estimate_at(t0 + Duration::from_millis(2_000) + Duration::from_millis(1_000));
        assert!(est > 1_012.0 && est < 1_016.0);
    }

    #[test]
    fn instant_of_inverts_estimate() {
        let clock = SlotClock::new(500, 200.0, 1.0);
        let target = 520.0;
        let at = clock.instant_of(target);
        let back = clock.estimate_at(at);
        assert!((back - target).abs() < 0.01);
    }
}
