//! Property tests (rung 1, part B).
//!
//! Value is measured with the UN-floored basket total `sum(h_i * p_i)` against a
//! fixed price vector. Using the protocol's own `holdings*price/TOK` here would
//! inject a measurement floor of up to 4 value-units that has nothing to do with
//! the protocol's rounding, and would make the "never against the fund"
//! direction assertion false for reasons that are an artefact of the yardstick.
//! With the exact linear total the direction claims are provable and exact.

use permafrost_core::*;
use proptest::prelude::*;

const N: usize = 4;

/// Exact basket value, no flooring. `None` on u128 overflow (the generators keep
/// well inside u128; a skip is honest rather than a false pass).
fn raw_value(h: &[u128; N], p: &[u128; N]) -> Option<u128> {
    let mut t = 0u128;
    for i in 0..N {
        t = t.checked_add(h[i].checked_mul(p[i])?)?;
    }
    Some(t)
}

/// Cross-multiplied comparison of T0/S0 vs T1/S1, avoiding rationals.
/// Returns `(after_minus_before_numerator, ok)` where the numerator is
/// `T1*S0 - T0*S1` as an i128-ish signed value carried in i128 when it fits.
fn cross(t0: u128, s0: u128, t1: u128, s1: u128) -> Option<(u128, u128)> {
    Some((t1.checked_mul(s0)?, t0.checked_mul(s1)?))
}

prop_compose! {
    fn arb_holdings()(
        a in 1u128..=10_000_000_000u128,
        b in 1u128..=10_000_000_000u128,
        c in 1u128..=10_000_000_000u128,
        d in 1u128..=10_000_000_000u128,
    ) -> [u128; N] { [a, b, c, d] }
}

prop_compose! {
    fn arb_prices()(
        a in 1u128..=1_000_000_000u128,
        b in 1u128..=1_000_000_000u128,
        c in 1u128..=1_000_000_000u128,
        d in 1u128..=1_000_000_000u128,
    ) -> [u128; N] { [a, b, c, d] }
}

prop_compose! {
    fn arb_vault()(
        h in arb_holdings(),
        supply in 1_000_000u128..=1_000_000_000_000_000u128,
        entry_bp in 0u128..=MAX_ENTRY_BP,
        exit_bp in 0u128..=MAX_EXIT_BP,
        stream_bp in 0u128..=MAX_STREAM_BP,
    ) -> Vault {
        let mut v = Vault::new(h, supply);
        v.entry_bp = entry_bp;
        v.exit_bp = exit_bp;
        v.stream_bp = stream_bp;
        v.dead_units = supply / 1000 + 1;   // seed units, unspendable
        v.last_accrual = 1_767_225_600;
        v
    }
}

/// Delivered amounts scaled off the vault so the deposit is neither absurdly
/// large nor always dust.
prop_compose! {
    fn arb_delivered(h: [u128; N])(
        m0 in 0u128..=100_000u128,
        m1 in 0u128..=100_000u128,
        m2 in 0u128..=100_000u128,
        m3 in 0u128..=100_000u128,
    ) -> [u128; N] {
        let m = [m0, m1, m2, m3];
        let mut out = [0u128; N];
        for i in 0..N {
            // 0 .. 10x the leg, in ten-thousandths
            out[i] = h[i].saturating_mul(m[i]) / 10_000;
        }
        out
    }
}

/// Case count, overridable with `PF_CASES` so a long campaign can dial it up
/// without editing the source. Failure persistence is off: this crate has no
/// `src/main.rs` for proptest to sit its regression file beside, and a silent
/// warning on every run trains you to ignore the output.
fn cases() -> u32 {
    std::env::var("PF_CASES")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(4000)
}

fn cfg() -> ProptestConfig {
    ProptestConfig {
        cases: cases(),
        failure_persistence: None,
        ..ProptestConfig::default()
    }
}

proptest! {
    #![proptest_config(cfg())]

    /// I1 + I3 + I6 + I2(deposit direction and bound).
    #[test]
    fn deposit_properties(v0 in arb_vault(), seedm in 0u128..=100_000u128, prices in arb_prices()) {
        let delivered = {
            let mut out = [0u128; N];
            for i in 0..N {
                // deterministic spread off one parameter keeps shrinking useful
                let mul = seedm + (i as u128) * 7919 % 50_000;
                out[i] = v0.holdings[i].saturating_mul(mul) / 10_000;
            }
            out
        };
        let mut v = v0.clone();
        let before_h = v0.holdings;
        let before_s = v0.supply;
        match deposit(&mut v, delivered) {
            Err(PfError::DustDeposit) => {
                // I1/I3: a rejected deposit must not have moved anything.
                prop_assert_eq!(v.holdings, before_h);
                prop_assert_eq!(v.supply, before_s);
            }
            Err(PfError::Overflow) => {}
            Err(e) => prop_assert!(false, "unexpected deposit error {:?}", e),
            Ok(out) => {
                // I1
                prop_assert!(v.supply > 0);
                prop_assert!(v.supply > before_s);
                // I3
                for i in 0..N { prop_assert!(v.holdings[i] > 0); }
                // refunds are non-negative by construction and delivered is conserved
                for i in 0..N {
                    prop_assert_eq!(out.required[i] + out.refunds[i], delivered[i]);
                    prop_assert_eq!(v.holdings[i], before_h[i] + out.required[i]);
                }
                // I6: units minted are never more than the retained tokens justify.
                // gross/S0 <= required_i/h_i for every leg.
                for i in 0..N {
                    let lhs = out.gross.checked_mul(before_h[i]);
                    let rhs = before_s.checked_mul(out.required[i]);
                    if let (Some(l), Some(r)) = (lhs, rhs) {
                        prop_assert!(l <= r, "I6 violated on leg {}: {} > {}", i, l, r);
                    }
                }
                // fee split is exact
                prop_assert_eq!(out.to_depositor + out.to_creator, out.gross);
                prop_assert_eq!(v.creator_units, v0.creator_units + out.to_creator);

                // I2: unit value unchanged up to rounding, never against the fund.
                if let (Some(t0), Some(t1)) =
                    (raw_value(&before_h, &prices), raw_value(&v.holdings, &prices))
                {
                    if let Some((lhs, rhs)) = cross(t0, before_s, t1, v.supply) {
                        prop_assert!(lhs >= rhs,
                            "I2 deposit moved value AGAINST the fund: {} < {}", lhs, rhs);
                        // Bound the dust: the drift comes from flooring `gross`
                        // (<=1 unit) plus the per-leg spread in `required`
                        // (<= S0/h_min in ratio terms).
                        let h_min = before_h.iter().copied().min().unwrap();
                        if let Some(bound) = t0
                            .checked_mul(before_s / h_min + 2)
                        {
                            prop_assert!(lhs - rhs <= bound,
                                "I2 deposit dust too large: {} > {}", lhs - rhs, bound);
                        }
                    }
                }
            }
        }
    }

    /// I1 + I2 + I3 for redeem, restricted to the spendable supply (the seed
    /// units live at an unspendable address, so `supply - dead_units` is the
    /// most anyone can ever burn).
    #[test]
    fn redeem_properties(v0 in arb_vault(), frac in 1u128..=10_000u128, prices in arb_prices()) {
        let spendable = v0.supply - v0.dead_units;
        prop_assume!(spendable > 0);
        let units_in = (spendable * frac / 10_000).max(1);
        let mut v = v0.clone();
        let before_h = v0.holdings;
        let before_s = v0.supply;
        let out = match redeem(&mut v, units_in) {
            Ok(o) => o,
            Err(PfError::Overflow) => return Ok(()),
            Err(e) => { prop_assert!(false, "unexpected redeem error {:?}", e); unreachable!() }
        };
        // I1
        prop_assert!(v.supply > 0);
        prop_assert!(v.supply >= v.dead_units);
        // I3
        for i in 0..N {
            prop_assert!(v.holdings[i] > 0, "I3 violated: leg {} hit zero", i);
            prop_assert_eq!(v.holdings[i] + out.payouts[i], before_h[i]);
        }
        prop_assert_eq!(out.burned + out.to_creator, units_in);
        prop_assert_eq!(v.supply, before_s - out.burned);

        // I2
        if let (Some(t0), Some(t1)) =
            (raw_value(&before_h, &prices), raw_value(&v.holdings, &prices))
        {
            if let Some((lhs, rhs)) = cross(t0, before_s, t1, v.supply) {
                prop_assert!(lhs >= rhs,
                    "I2 redeem moved value AGAINST the fund: {} < {}", lhs, rhs);
                let psum: u128 = prices.iter().sum();
                if let Some(bound) = before_s.checked_mul(psum) {
                    prop_assert!(lhs - rhs <= bound,
                        "I2 redeem dust too large: {} > {}", lhs - rhs, bound);
                }
            }
        }
    }

    /// I7: a deposit followed immediately by redeeming exactly the units it
    /// minted must never return more of any leg than was retained.
    #[test]
    fn i7_round_trip_never_profits(v0 in arb_vault(), seedm in 1u128..=100_000u128) {
        let mut v = v0.clone();
        v.entry_bp = 0;
        v.exit_bp = 0;
        let mut delivered = [0u128; N];
        for i in 0..N {
            let mul = seedm + (i as u128) * 7919 % 50_000;
            delivered[i] = v.holdings[i].saturating_mul(mul) / 10_000;
        }
        let out = match deposit(&mut v, delivered) {
            Ok(o) => o,
            Err(_) => return Ok(()),
        };
        let back = match redeem(&mut v, out.to_depositor) {
            Ok(r) => r,
            Err(_) => return Ok(()),
        };
        for i in 0..N {
            prop_assert!(back.payouts[i] <= out.required[i],
                "I7 violated on leg {}: got {} back for {} retained",
                i, back.payouts[i], out.required[i]);
            // and never more than was delivered in the first place
            prop_assert!(back.payouts[i] + out.refunds[i] <= delivered[i]);
        }
    }

    /// I1 for accrue, plus the guard on an over-long window.
    #[test]
    fn accrue_properties(v0 in arb_vault(), dt in -1000i64..=200_000_000i64) {
        let mut v = v0.clone();
        let now = v0.last_accrual + dt;
        match accrue(&mut v, now) {
            Ok(minted) => {
                prop_assert!(v.supply > 0);
                prop_assert_eq!(v.supply, v0.supply + minted);
                prop_assert_eq!(v.creator_units, v0.creator_units + minted);
                prop_assert_eq!(v.holdings, v0.holdings);
                if dt <= 0 || v0.stream_bp == 0 {
                    prop_assert_eq!(minted, 0);
                } else {
                    // the window must have been inside the guard
                    prop_assert!(v0.stream_bp * (dt as u128) < BP * YEAR);
                }
            }
            Err(PfError::AccrualWindowTooLong) => {
                prop_assert!(v0.stream_bp * (dt as u128) >= BP * YEAR);
                // must not have mutated
                prop_assert_eq!(v.supply, v0.supply);
                prop_assert_eq!(v.last_accrual, v0.last_accrual);
            }
            Err(e) => prop_assert!(false, "unexpected accrue error {:?}", e),
        }
    }

    /// Rebalance: after `apply_plan` the breaching leg is at or below the
    /// target, and no recipient exceeds the 40% cap unless it was already above
    /// it beforehand.
    #[test]
    fn rebalance_properties(h in arb_holdings(), prices in arb_prices()) {
        let w0 = match weights_bp(&h, &prices) { Ok(w) => w, Err(_) => return Ok(()) };
        let plan = match rebalance_plan(&h, &prices) {
            Ok(Some(p)) => p,
            Ok(None) => {
                // no leg above the trigger
                for i in 0..N { prop_assert!(w0.w[i] <= TRIGGER_BP); }
                return Ok(());
            }
            Err(_) => return Ok(()),
        };
        prop_assert!(w0.w[plan.leg] > TRIGGER_BP);
        // the allocation never exceeds what was actually sold
        let alloc_sum: u128 = plan.alloc_value.iter().sum();
        prop_assert_eq!(alloc_sum, plan.sell_value_placed);
        prop_assert!(plan.sell_value_placed <= plan.sell_value_wanted);
        prop_assert_eq!(plan.capped, plan.sell_value_placed < plan.sell_value_wanted);
        prop_assert_eq!(plan.alloc_value[plan.leg], 0);

        let after = match apply_plan(&h, &plan) { Ok(a) => a, Err(_) => return Ok(()) };
        for i in 0..N { prop_assert!(after[i] > 0, "rebalance emptied leg {}", i); }
        let w1 = match weights_bp(&after, &prices) { Ok(w) => w, Err(_) => return Ok(()) };

        let target_value = w0.total * TARGET_BP / BP;
        let cap_value = w0.total * RECIPIENT_CAP_BP / BP;

        // Breaching leg at or below target, in VALUE terms. The slack is the
        // two floors on the way there: `sell_tokens` floors (losing < 1 token,
        // worth < price/TOK) and `vals` floors again (< 1).
        if !plan.capped {
            let slack = prices[plan.leg] / TOK + 2;
            prop_assert!(w1.vals[plan.leg] <= target_value + slack,
                "breaching leg above target: {} > {} (+{})",
                w1.vals[plan.leg], target_value, slack);
        }
        // It must at minimum no longer breach the trigger when uncapped.
        if !plan.capped {
            prop_assert!(w1.w[plan.leg] <= TRIGGER_BP);
        }
        // No recipient above the 40% cap unless it already was.
        for i in 0..N {
            if i == plan.leg { continue; }
            let slack = prices[i] / TOK + 2;
            let ceiling = w0.vals[i].max(cap_value);
            prop_assert!(w1.vals[i] <= ceiling + slack,
                "recipient {} ended above the cap: {} > {} (+{}); before {}",
                i, w1.vals[i], ceiling, slack, w0.vals[i]);
        }
    }

    /// Auction arithmetic: the offered ratio never rounds against the vault and
    /// decays monotonically.
    #[test]
    fn auction_properties(
        r0 in 0u128..=1_000_000_000u128,
        r1 in 0u128..=1_000_000_000u128,
        r2 in 0u128..=1_000_000_000u128,
        t in -100i64..=300_000i64,
    ) {
        let fair = [0u128, r0, r1, r2];
        let m = margin_bp(t);
        prop_assert!(m <= AUCTION_START_BP && m >= AUCTION_FLOOR_BP);
        let offered = offered_ratio(&fair, t).unwrap();
        for i in 0..N {
            // ceiling: offered*BP >= fair*(BP+m)
            let want = fair[i] * ((BP as i128 + m) as u128);
            prop_assert!(offered[i] * BP >= want, "offered_ratio rounded DOWN on leg {}", i);
            prop_assert!(offered[i] * BP < want + BP, "offered_ratio rounded up too far");
        }
        // monotone non-increasing in time
        if t > 0 {
            prop_assert!(margin_bp(t - 1) >= m);
        }
        let req = fill_required(&fair, TOK, t).unwrap();
        prop_assert_eq!(req, offered, "one whole sell unit must require exactly the ratio");
    }

    /// `normalise` matches the mathematical definition in both directions,
    /// including for negative prices (price - conf can go below zero).
    #[test]
    fn normalise_properties(price in -1_000_000_000_000i128..=1_000_000_000_000i128, expo in -12i32..=6i32) {
        let lo = normalise(price, expo, false).unwrap();
        let hi = normalise(price, expo, true).unwrap();
        prop_assert!(lo <= hi);
        prop_assert!(hi - lo <= 1);
        let shift = expo - USDC_EXPO;
        if shift < 0 {
            let d = 10i128.pow((-shift) as u32);
            prop_assert!(lo * d <= price, "floor direction wrong");
            prop_assert!((lo + 1) * d > price);
            prop_assert!(hi * d >= price, "ceil direction wrong");
            prop_assert!((hi - 1) * d < price);
        } else {
            prop_assert_eq!(lo, hi);
            prop_assert_eq!(lo, price * 10i128.pow(shift as u32));
        }
    }
}

/// A supply of zero must be rejected rather than dividing by zero.
#[test]
fn zero_supply_rejected() {
    let mut v = Vault::new([1, 1, 1, 1], 0);
    assert_eq!(deposit(&mut v, [1, 1, 1, 1]), Err(PfError::UnseededVault));
    assert_eq!(redeem(&mut v, 1), Err(PfError::BadRedeemAmount));
}

#[test]
fn empty_leg_rejected() {
    let mut v = Vault::new([1, 0, 1, 1], 1000);
    assert_eq!(deposit(&mut v, [1, 1, 1, 1]), Err(PfError::EmptyLeg));
}

#[test]
fn seed_rejects_empty_and_zero_units() {
    assert_eq!(seed([1, 0, 1, 1], 10, 0), Err(PfError::EmptySeedLeg));
    assert_eq!(seed([1, 1, 1, 1], 0, 0), Err(PfError::ZeroInitialUnits));
}

#[test]
fn accrue_window_guard() {
    let mut v = Vault::new([1, 1, 1, 1], 1000);
    v.stream_bp = 100;
    v.last_accrual = 0;
    // 100 bp * elapsed >= 10_000 * YEAR  =>  elapsed >= 100 years
    assert_eq!(
        accrue(&mut v, (BP * YEAR / 100) as i64),
        Err(PfError::AccrualWindowTooLong)
    );
    assert!(accrue(&mut v, (BP * YEAR / 100) as i64 - 1).is_ok());
}

#[test]
fn zero_basket_value_rejected() {
    assert_eq!(
        weights_bp(&[1, 1, 1, 1], &[0, 0, 0, 0]),
        Err(PfError::ZeroBasketValue)
    );
}
