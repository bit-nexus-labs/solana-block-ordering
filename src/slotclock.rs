//! Local estimate of the cluster slot.
//!
//! The probe cannot afford to poll the RPC every few milliseconds, so it keeps the last few
//! `getSlot` answers and extrapolates from them. Two things make single answers unreliable:
//! a load-balanced RPC endpoint can answer from a node that lags behind, and network delay
//! varies per request. So the slot duration comes from a least-squares fit over all recent
//! answers, and the position of the line comes from the *most advanced* answer (an RPC node
//! can lag behind the cluster but cannot be ahead of it). The estimate is only used to decide
//! *when* to fire; the real landing slot of every transaction is read back from the chain.

use {
    std::collections::VecDeque,
    std::sync::{Arc, Mutex},
    std::time::{Duration, Instant},
};

const MAX_OBS: usize = 24;
/// The fitted slot duration is trusted only when the answers span at least this long.
const MIN_FIT_SPAN_MS: f64 = 3_000.0;

#[derive(Debug)]
struct State {
    origin: Instant,
    /// (milliseconds since origin, slot)
    obs: VecDeque<(f64, f64)>,
    slot_ms: f64,
    /// Slot at t = 0 ms on the fitted line, moved up to the most advanced answer.
    intercept: f64,
}

#[derive(Clone, Debug)]
pub struct SlotClock {
    inner: Arc<Mutex<State>>,
    /// Added to every estimate. `getSlot` with processed commitment trails the slot being
    /// produced, so a small positive bias is expected.
    bias_slots: f64,
}

impl SlotClock {
    pub fn new(slot: u64, nominal_slot_ms: f64, bias_slots: f64) -> Self {
        let origin = Instant::now();
        let mut obs = VecDeque::with_capacity(MAX_OBS);
        obs.push_back((0.0, slot as f64));
        Self {
            inner: Arc::new(Mutex::new(State {
                origin,
                obs,
                slot_ms: nominal_slot_ms,
                intercept: slot as f64,
            })),
            bias_slots,
        }
    }

    /// Feeds a fresh observation and refits the line.
    pub fn observe(&self, slot: u64, at: Instant) {
        let mut s = self.inner.lock().unwrap();
        let t = ms_between(s.origin, at);
        s.obs.push_back((t, slot as f64));
        while s.obs.len() > MAX_OBS {
            s.obs.pop_front();
        }
        let span = s.obs.back().unwrap().0 - s.obs.front().unwrap().0;
        if s.obs.len() >= 4 && span >= MIN_FIT_SPAN_MS {
            let pts: Vec<(f64, f64)> = s.obs.iter().copied().collect();
            if let Some((per_slot, icpt)) = fit(&pts) {
                // Refit without answers that lag the first line by more than a slot.
                let kept: Vec<(f64, f64)> = pts
                    .iter()
                    .copied()
                    .filter(|(t, y)| y - (icpt + t / per_slot) > -1.0)
                    .collect();
                let per_slot = if kept.len() >= 4 {
                    fit(&kept).map(|f| f.0).unwrap_or(per_slot)
                } else {
                    per_slot
                };
                if (50.0..2000.0).contains(&per_slot) {
                    s.slot_ms = per_slot;
                }
            }
        }
        let slot_ms = s.slot_ms;
        s.intercept = s
            .obs
            .iter()
            .map(|(t, y)| y - t / slot_ms)
            .fold(f64::MIN, f64::max);
    }

    pub fn slot_ms(&self) -> f64 {
        self.inner.lock().unwrap().slot_ms
    }

    pub fn estimate_at(&self, t: Instant) -> f64 {
        let s = self.inner.lock().unwrap();
        s.intercept + ms_between(s.origin, t) / s.slot_ms + self.bias_slots
    }

    pub fn estimate_now(&self) -> f64 {
        self.estimate_at(Instant::now())
    }

    /// Instant at which the estimate reaches `slot` (may be in the past).
    pub fn instant_of(&self, slot: f64) -> Instant {
        let s = self.inner.lock().unwrap();
        let ms = (slot - self.bias_slots - s.intercept) * s.slot_ms;
        if ms >= 0.0 {
            s.origin + Duration::from_secs_f64(ms / 1000.0)
        } else {
            s.origin
                .checked_sub(Duration::from_secs_f64(-ms / 1000.0))
                .unwrap_or(s.origin)
        }
    }
}

/// Least-squares line slot = icpt + t / per_slot. Returns (per_slot, icpt).
fn fit(pts: &[(f64, f64)]) -> Option<(f64, f64)> {
    let n = pts.len() as f64;
    let (mt, my) = pts
        .iter()
        .fold((0.0, 0.0), |(a, b), (t, y)| (a + t / n, b + y / n));
    let (mut sxy, mut sxx) = (0.0, 0.0);
    for (t, y) in pts {
        sxy += (t - mt) * (y - my);
        sxx += (t - mt) * (t - mt);
    }
    if sxx <= 0.0 || sxy <= 0.0 {
        return None;
    }
    let slope = sxy / sxx;
    Some((1.0 / slope, my - slope * mt))
}

fn ms_between(origin: Instant, t: Instant) -> f64 {
    if t >= origin {
        t.duration_since(origin).as_secs_f64() * 1000.0
    } else {
        -(origin.duration_since(t).as_secs_f64() * 1000.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn learns_slot_duration_from_a_series() {
        let clock = SlotClock::new(1_000, 400.0, 0.0);
        let t0 = clock.inner.lock().unwrap().origin;
        for i in 1..=10u64 {
            clock.observe(1_000 + i * 5, t0 + Duration::from_millis(i * 1_000));
        }
        assert!(
            (clock.slot_ms() - 200.0).abs() < 1.0,
            "slot_ms {}",
            clock.slot_ms()
        );
        let est = clock.estimate_at(t0 + Duration::from_millis(11_000));
        assert!((est - 1_055.0).abs() < 0.2, "est {est}");
    }

    #[test]
    fn a_lagging_answer_does_not_pull_the_estimate_back() {
        let clock = SlotClock::new(1_000, 200.0, 0.0);
        let t0 = clock.inner.lock().unwrap().origin;
        for i in 1..=8u64 {
            clock.observe(1_000 + i * 5, t0 + Duration::from_millis(i * 1_000));
        }
        // A lagging node answers 6 slots behind.
        clock.observe(1_039, t0 + Duration::from_millis(9_000));
        let est = clock.estimate_at(t0 + Duration::from_millis(9_000));
        assert!(est >= 1_044.9, "est {est}");
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
