//! Adversarial tests: can a caller extract value from the fund?
//!
//! `properties.rs` checks each instruction against its own contract. This file
//! asks the economic question instead — for every sequence a user can actually
//! submit on-chain, does the value backing one unit ever fall? An immutable
//! contract cannot be patched after launch, so the only defence is that no such
//! sequence exists.
//!
//! Value is measured RAW (`sum(holdings_i * price_i)`, no division), because the
//! floored `basket_value` would blur a genuine leak of a few base units into
//! rounding noise. Comparisons of value-per-unit are cross-multiplied so nothing
//! is decided in floating point. Generator bounds are chosen so those products
//! stay inside u128; overflow is treated as a skipped case, never a pass.

use permafrost_core::*;
use proptest::prelude::*;

const N: usize = NLEGS;

/// Exact, unfloored basket value. `None` if it would not fit in u128.
fn raw(h: &[u128; N], p: &[u128; N]) -> Option<u128> {
    let mut t = 0u128;
    for i in 0..N {
        t = t.checked_add(h[i].checked_mul(p[i])?)?;
    }
    Some(t)
}

/// True when `v1/s1 >= v0/s0`, decided by cross-multiplication.
/// `None` if the comparison overflows.
fn per_unit_not_worse(v0: u128, s0: u128, v1: u128, s1: u128) -> Option<bool> {
    let lhs = v1.checked_mul(s0)?;
    let rhs = v0.checked_mul(s1)?;
    Some(lhs >= rhs)
}

prop_compose! {
    /// Bounded so that value * supply stays inside u128 (see the module note).
    fn arb_holdings()(
        a in 1u128..=1_000_000_000_000u128,
        b in 1u128..=1_000_000_000_000u128,
        c in 1u128..=1_000_000_000_000u128,
        d in 1u128..=1_000_000_000_000u128,
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
        supply in 1u128..=1_000_000_000_000u128,
        entry in 0u128..=MAX_ENTRY_BP,
        exit in 0u128..=MAX_EXIT_BP,
        stream in 0u128..=MAX_STREAM_BP,
    ) -> Vault {
        let mut v = seed(h, supply, 0).unwrap();
        v.entry_bp = entry;
        v.exit_bp = exit;
        v.stream_bp = stream;
        v
    }
}

/// Delivered amounts scaled off the vault, jittered per leg so the ratio rarely
/// matches — an exactly proportional deposit is the easy case, not the risky one.
fn delivered_for(h: &[u128; N], num: u128, j: &[u128; N]) -> [u128; N] {
    let mut d = [0u128; N];
    for i in 0..N {
        d[i] = h[i].saturating_mul(num) / 1_000_000;
        d[i] = d[i].saturating_mul(j[i]) / 10_000;
    }
    d
}

/// Case count, overridable with `PF_CASES` so a long campaign can dial it up
/// without editing the source. Failure persistence is off: this crate has no
/// `src/main.rs` for proptest to sit its regression file beside, and a silent
/// warning on every run trains you to ignore the output.
fn cases() -> u32 {
    std::env::var("PF_CASES")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(3000)
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

    // ------------------------------------------------------------------ A1
    /// A deposit must never dilute the existing holders.
    ///
    /// This is the invariant the `gross = supply * k / K` formulation broke: a
    /// dust deposit whose `required` floored to zero on every leg still minted
    /// units, so the value behind each existing unit fell. Any future change to
    /// `deposit` has to keep this green.
    #[test]
    fn a1_deposit_never_dilutes(
        v0 in arb_vault(),
        num in 1u128..=2_000_000u128,
        j in prop::array::uniform4(5_000u128..=20_000u128),
        prices in arb_prices(),
    ) {
        let mut v = v0.clone();
        let d = delivered_for(&v0.holdings, num, &j);
        if deposit(&mut v, d).is_ok() {
            let (Some(b0), Some(b1)) = (raw(&v0.holdings, &prices), raw(&v.holdings, &prices))
                else { return Ok(()) };
            if let Some(ok) = per_unit_not_worse(b0, v0.supply, b1, v.supply) {
                prop_assert!(ok, "deposit diluted: {:?} -> {:?}", v0, v);
            }
        }
    }

    // ------------------------------------------------------------------ A2
    /// A redemption must never dilute whoever stays in.
    #[test]
    fn a2_redeem_never_dilutes(
        v0 in arb_vault(),
        frac in 1u128..=9_999u128,
        prices in arb_prices(),
    ) {
        let mut v = v0.clone();
        let units = (v0.supply * frac / 10_000).max(1);
        if redeem(&mut v, units).is_ok() {
            let (Some(b0), Some(b1)) = (raw(&v0.holdings, &prices), raw(&v.holdings, &prices))
                else { return Ok(()) };
            if let Some(ok) = per_unit_not_worse(b0, v0.supply, b1, v.supply) {
                prop_assert!(ok, "redeem diluted: {:?} -> {:?}", v0, v);
            }
        }
    }

    // ------------------------------------------------------------------ A3
    /// Splitting a deposit into P pieces may mint at most P extra base units.
    ///
    /// It is not exactly zero, and pretending otherwise would be dishonest: the
    /// entry fee `gross * entry_bp / BP` floors once per call, so P calls floor
    /// P times and the attacker keeps the crumbs. The bound is what matters —
    /// one base unit of the unit mint is 1e-9 of a unit, so a thousand-way split
    /// gains about 1e-6 of a unit and costs a thousand Solana transactions to
    /// execute. The attack is real, quantified, and hopeless.
    #[test]
    fn a3_deposit_splitting_gains_at_most_one_base_unit_per_piece(
        v0 in arb_vault(),
        num in 1u128..=1_000_000u128,
        j in prop::array::uniform4(5_000u128..=20_000u128),
        pieces in 2u128..=64u128,
    ) {
        let d = delivered_for(&v0.holdings, num, &j);

        let mut whole = v0.clone();
        let single = deposit(&mut whole, d).map(|o| o.to_depositor).unwrap_or(0);

        // The remainder on an uneven division is simply not delivered, which can
        // only help the fund, never the attacker.
        let mut split = v0.clone();
        let mut got = 0u128;
        let piece: [u128; N] = std::array::from_fn(|i| d[i] / pieces);
        for _ in 0..pieces {
            if let Ok(o) = deposit(&mut split, piece) {
                got += o.to_depositor;
            }
        }
        prop_assert!(got <= single + pieces,
            "splitting {} ways minted {} vs {} — more than the {}-base-unit rounding bound",
            pieces, got, single, pieces);
    }

    // ------------------------------------------------------------------ A4
    /// Splitting a redemption may avoid at most P base units of exit fee.
    ///
    /// Measured on units burnt, not on tokens paid out: the payout is the burnt
    /// amount scaled by holdings/supply, so a vault holding a trillion tokens
    /// against a supply of a hundred would turn one base unit into a headline
    /// number and tell us nothing about the actual leak. The leak is the fee,
    /// and the fee floors once per call.
    #[test]
    fn a4_redeem_splitting_avoids_at_most_one_base_unit_of_fee_per_piece(
        v0 in arb_vault(),
        frac in 2u128..=9_999u128,
        pieces in 2u128..=64u128,
    ) {
        let units = (v0.supply * frac / 10_000).max(pieces);

        let mut whole = v0.clone();
        let Ok(single) = redeem(&mut whole, units) else { return Ok(()) };

        let mut split = v0.clone();
        let mut burned = 0u128;
        let piece = units / pieces;
        if piece == 0 { return Ok(()); }
        for _ in 0..pieces {
            if let Ok(o) = redeem(&mut split, piece) {
                burned += o.burned;
            }
        }
        prop_assert!(burned <= single.burned + pieces,
            "splitting {} ways burnt {} vs {} — beyond the {}-base-unit fee-rounding bound",
            pieces, burned, single.burned, pieces);
    }

    // ------------------------------------------------------------------ A5
    /// Deposit then immediately redeem everything received: the round trip must
    /// never return more of any leg than was put in.
    #[test]
    fn a5_round_trip_never_profits(
        v0 in arb_vault(),
        num in 1u128..=1_000_000u128,
        j in prop::array::uniform4(5_000u128..=20_000u128),
    ) {
        let mut v = v0.clone();
        let d = delivered_for(&v0.holdings, num, &j);
        let Ok(dep) = deposit(&mut v, d) else { return Ok(()) };
        // What the depositor actually parted with is delivered minus refunds.
        let paid: [u128; N] = std::array::from_fn(|i| d[i] - dep.refunds[i]);
        if dep.to_depositor == 0 { return Ok(()); }
        let Ok(red) = redeem(&mut v, dep.to_depositor) else { return Ok(()) };
        for i in 0..N {
            prop_assert!(red.payouts[i] <= paid[i],
                "round trip profited on leg {}: paid {} got {}", i, paid[i], red.payouts[i]);
        }
    }

    // ------------------------------------------------------------------ A6
    /// A depositor cannot sandwich the streaming-fee crank. Cranking `accrue`
    /// mints units to the creator and so DOES dilute — but it must dilute by no
    /// more than the fee, and it must never change the holdings at all.
    #[test]
    fn a6_accrue_touches_no_holdings(
        v0 in arb_vault(),
        dt in 0i64..=200_000_000i64,
    ) {
        let mut v = v0.clone();
        let before = v0.holdings;
        let _ = accrue(&mut v, v0.last_accrual + dt);
        prop_assert_eq!(before, v.holdings, "accrue moved tokens");
        prop_assert!(v.supply >= v0.supply, "accrue burned supply");
        // Everything minted is the creator's, and nobody else's balance moved.
        prop_assert_eq!(v.supply - v0.supply, v.creator_units - v0.creator_units);
    }

    // ------------------------------------------------------------------ A7
    /// Cranking the streaming fee often must never mint more than cranking once.
    ///
    /// `m = S*f/(1-f)` compounds, so frequent cranking is if anything slightly
    /// cheaper for holders; what must never happen is the reverse.
    #[test]
    fn a7_crank_frequency_does_not_pay_the_creator(
        v0 in arb_vault(),
        dt in 1i64..=20_000_000i64,
        pieces in 2i64..=12i64,
    ) {
        let mut once = v0.clone();
        let _ = accrue(&mut once, v0.last_accrual + dt);

        let mut many = v0.clone();
        for p in 1..=pieces {
            let _ = accrue(&mut many, v0.last_accrual + dt * p / pieces);
        }
        prop_assert!(many.creator_units <= once.creator_units,
            "cranking {} times minted more: {} > {}", pieces, many.creator_units, once.creator_units);
    }

    // ------------------------------------------------------------------ A8
    /// An auction fill must never cost the fund more than the floor margin.
    ///
    /// The counterparty picks the moment, so assume the worst: the last second
    /// of the window, where the offered ratio is 150bp below fair. Value in must
    /// still be at least 98.5% of value out, measured at the same prices the
    /// plan was built from.
    #[test]
    fn a8_auction_never_gives_away_more_than_the_floor(
        h in arb_holdings(),
        prices in arb_prices(),
        elapsed in -3_600i64..=(AUCTION_WINDOW + 3_600),
        frac in 1u128..=10_000u128,
    ) {
        let Ok(Some(plan)) = rebalance_plan(&h, &prices) else { return Ok(()) };
        let Ok(fair) = fair_ratio(&plan) else { return Ok(()) };
        let amt = (plan.sell_tokens * frac / 10_000).max(1);
        let Ok(need) = fill_required(&fair, amt, elapsed) else { return Ok(()) };

        // Value leaving: `amt` tokens of the sold leg. Value arriving: `need`.
        let Some(out) = amt.checked_mul(prices[plan.leg]) else { return Ok(()) };
        let mut inn = 0u128;
        for i in 0..N {
            if i == plan.leg { continue; }
            let Some(x) = need[i].checked_mul(prices[i]) else { return Ok(()) };
            let Some(s) = inn.checked_add(x) else { return Ok(()) };
            inn = s;
        }

        // The fair ratio itself is floored twice before it gets here, so allow
        // the floor margin plus a slack that scales with how coarse the parcel
        // is: with a tiny `sell_tokens` the ratio's own rounding dominates.
        let Some(lhs) = inn.checked_mul(10_000) else { return Ok(()) };
        let floor_ok = 10_000u128 - 150;
        let Some(rhs) = out.checked_mul(floor_ok) else { return Ok(()) };
        // `fair_ratio` floors `buy_tokens * SELL_UNIT / sell_tokens`, losing up to
        // one base unit of ratio per leg. That error is per SELL_UNIT sold, so it
        // scales with the parcel: allow `amt / SELL_UNIT + 1` base units on each
        // bought leg, valued at that leg's price. (The final ceil in
        // `fill_required` hands at most one of those back.)
        let scale = amt / SELL_UNIT + 1;
        let mut slack = 0u128;
        for i in 0..N {
            if i != plan.leg {
                slack = slack.saturating_add(prices[i].saturating_mul(scale));
            }
        }
        let slack = slack.saturating_mul(10_000);

        prop_assert!(lhs + slack >= rhs,
            "auction gave away more than the floor at elapsed={}: in {} out {} (margin {}bp)",
            elapsed, inn, out, margin_bp(elapsed));
    }

    // ------------------------------------------------------------------ A9
    /// A rebalance must not change the basket's value at the oracle's own prices
    /// by more than the flooring in the plan. This is the sanity check on the
    /// plan itself, before any counterparty is involved.
    #[test]
    fn a9_plan_is_value_neutral_at_oracle_prices(
        h in arb_holdings(),
        prices in arb_prices(),
    ) {
        let Ok(Some(plan)) = rebalance_plan(&h, &prices) else { return Ok(()) };
        let Ok(after) = apply_plan(&h, &plan) else { return Ok(()) };
        let (Some(b0), Some(b1)) = (raw(&h, &prices), raw(&after, &prices)) else { return Ok(()) };
        // Both `sell_tokens` and `buy_tokens` floor a value into a token count,
        // so the basket can drift EITHER way by up to one whole token's worth on
        // each leg — selling a hair less than intended shows up as a gain. The
        // point of the test is that it cannot drift further, in either direction.
        let mut tol = 0u128;
        for i in 0..N {
            tol = tol.saturating_add(prices[i].saturating_mul(TOK));
        }
        prop_assert!(b1 <= b0 + tol, "rebalance created value: {} -> {} (tol {})", b0, b1, tol);
        prop_assert!(b1 + tol >= b0, "rebalance destroyed value: {} -> {} (tol {})", b0, b1, tol);
    }

    // ------------------------------------------------------------------ A10
    /// Supply may never reach zero, whatever the caller does. A zero supply
    /// permanently bricks minting — `deposit` divides by it — and in an
    /// immutable contract that is unrecoverable. This is why the seed units go
    /// to an unspendable address rather than being burnt.
    #[test]
    fn a10_supply_never_reaches_zero(
        v0 in arb_vault(),
        frac in 1u128..=10_000u128,
    ) {
        let mut v = v0.clone();
        // The most hostile redemption a caller can submit: everything they could
        // possibly hold, i.e. supply minus the unspendable seed.
        let float = v.supply.saturating_sub(v.dead_units);
        if float > 0 {
            let units = (float * frac / 10_000).max(1);
            let _ = redeem(&mut v, units);
        }
        prop_assert!(v.supply > 0);
        prop_assert!(invariants(&v));
    }
}

// =========================================================== discount mode
//
// A11..A15 are the five properties `discount_fuzz.py` established against the
// Python reference, restated here against the Rust core. P1 and P2 were asked
// for by name; P3..P5 are the structural checks that would each be a quiet
// disaster on chain.
//
// The fuzz drives them with a random walk. Proptest gives the same thing with
// shrinking, so a failure arrives as the shortest sequence that still breaks —
// which is the difference between a bug report and a haystack.

/// One step of an adversarial discount-mode sequence.
#[derive(Debug, Clone)]
enum DOp {
    /// Creator tops the pool up.
    Fund(u128),
    /// Creator takes units back out.
    Withdraw(u128),
    /// Creator moves the dials, including while positions are open.
    Dial {
        on: bool,
        dbp: u128,
        lbp: u128,
        cap: u128,
    },
    /// Liquid discounted deposit.
    Liquid { num: u128, j: [u128; N] },
    /// Locked deposit.
    Locked { num: u128, j: [u128; N] },
    /// Close an open position. `early` picks the side of maturity.
    Close { which: usize, early: bool, dt: i64 },
    /// Time passes and the streaming fee is cranked.
    Accrue(i64),
    /// An ordinary redemption, to prove the pool survives supply moving.
    Redeem(u128),
}

fn arb_dop() -> impl Strategy<Value = DOp> {
    prop_oneof![
        (1u128..=1_000_000_000_000u128).prop_map(DOp::Fund),
        (1u128..=1_000_000_000_000u128).prop_map(DOp::Withdraw),
        (
            any::<bool>(),
            0u128..=MAX_DISCOUNT_BP,
            0u128..=MAX_LOCKED_DISCOUNT_BP,
            0u128..=1_000_000_000_000u128,
        )
            .prop_map(|(on, dbp, lbp, cap)| DOp::Dial { on, dbp, lbp, cap }),
        (
            1u128..=1_000_000u128,
            prop::array::uniform4(5_000u128..=20_000u128)
        )
            .prop_map(|(num, j)| DOp::Liquid { num, j }),
        (
            1u128..=1_000_000u128,
            prop::array::uniform4(5_000u128..=20_000u128)
        )
            .prop_map(|(num, j)| DOp::Locked { num, j }),
        (any::<usize>(), any::<bool>(), 1i64..=1_000_000i64)
            .prop_map(|(which, early, dt)| DOp::Close { which, early, dt }),
        (0i64..=40 * 24 * 60 * 60i64).prop_map(DOp::Accrue),
        (1u128..=10_000u128).prop_map(DOp::Redeem),
    ]
}

/// What the sequence did, and every way it could have gone wrong.
#[derive(Debug, Default)]
struct Report {
    // P1's four-column ledger.
    funded: u128,
    withdrawn: u128,
    paid_out: u128,
    returned: u128,
    /// P1: a bonus was paid that was larger than the pool held at the time.
    overdrawn: bool,
    /// P2: `(returned, expected)` for the first early exit that shortchanged
    /// the depositor.
    short_exit: Option<(u128, u128)>,
    /// P3: a pool-only operation moved vault holdings or total supply.
    vault_moved: bool,
    /// P4: `subsidy + escrow` exceeded the minted supply at some point.
    unbacked: bool,
    /// P5: some withdrawal reached units that were sitting in escrow.
    reached_escrow: bool,
    /// P5: a withdrawal of more than the pool holds was accepted.
    overwithdrew: bool,
    /// Ops that actually did something, so a test can skip vacuous sequences.
    liquid_paid: u32,
    locked_opened: u32,
    early_exits: u32,
    claims: u32,
}

/// Run an adversarial sequence and record everything the five properties need.
///
/// The pool starts empty and every unit that enters it is counted, so P1's
/// ledger is closed: nothing arrives from outside the trace.
fn run_discount_sequence(v0: &Vault, ops: &[DOp]) -> (Vault, Report) {
    let mut v = v0.clone();
    v.subsidy_units = 0;
    v.escrow_units = 0;
    v.discount_on = false;
    v.discount_bp = 0;
    v.locked_discount_bp = 0;
    v.max_bonus_units = 0;

    let mut r = Report::default();
    let mut clock: i64 = 0;
    // The depositor's own units, recorded at the moment the position opened, so
    // P2 is checked against what was actually put in rather than against what
    // the position says now.
    let mut principal_at_open: std::collections::BTreeMap<u64, u128> = Default::default();

    for op in ops {
        let before_holdings = v.holdings;
        let before_supply = v.supply;
        // Only deposits and the crank may move the vault; everything else is a
        // pool operation and must leave holdings and supply exactly alone.
        let may_move = matches!(
            op,
            DOp::Liquid { .. } | DOp::Locked { .. } | DOp::Accrue(_) | DOp::Redeem(_)
        );

        match op {
            DOp::Fund(amt) => {
                // Clamp to the headroom: funding beyond it is refused by design
                // and would just make most of the sequence a no-op.
                let headroom = v
                    .supply
                    .saturating_sub(v.subsidy_units)
                    .saturating_sub(v.escrow_units);
                let amt = (*amt).min(headroom);
                if amt > 0 && fund_subsidy(&mut v, amt).is_ok() {
                    r.funded += amt;
                }
            }
            DOp::Withdraw(amt) => {
                let escrow_before = v.escrow_units;
                let positions_before = v.positions.clone();
                // P5, asked directly: anything above the pool must be refused,
                // whatever is sitting in escrow next to it.
                if withdraw_subsidy(&mut v.clone(), v.subsidy_units + 1).is_ok() {
                    r.overwithdrew = true;
                }
                let amt = (*amt).min(v.subsidy_units);
                if amt > 0 && withdraw_subsidy(&mut v, amt).is_ok() {
                    r.withdrawn += amt;
                }
                if v.escrow_units != escrow_before || v.positions != positions_before {
                    r.reached_escrow = true;
                }
            }
            DOp::Dial { on, dbp, lbp, cap } => {
                v.discount_on = *on;
                let _ = set_discount(&mut v, *dbp, *lbp, *cap);
            }
            DOp::Liquid { num, j } => {
                let d = delivered_for(&v.holdings, *num, j);
                let pool_before = v.subsidy_units;
                if let Ok(o) = deposit_discounted(&mut v, d) {
                    if o.bonus > pool_before {
                        r.overdrawn = true;
                    }
                    r.paid_out += o.bonus;
                    if o.bonus > 0 {
                        r.liquid_paid += 1;
                    }
                }
            }
            DOp::Locked { num, j } => {
                let d = delivered_for(&v.holdings, *num, j);
                let pool_before = v.subsidy_units;
                if let Ok(o) = deposit_locked(&mut v, d, clock) {
                    if o.bonus > pool_before {
                        r.overdrawn = true;
                    }
                    r.paid_out += o.bonus;
                    principal_at_open.insert(o.position_id, o.principal);
                    r.locked_opened += 1;
                }
            }
            DOp::Close { which, early, dt } => {
                if v.positions.is_empty() {
                    continue;
                }
                let keys: Vec<u64> = v.positions.keys().copied().collect();
                let pid = keys[which % keys.len()];
                let p = v.positions[&pid].clone();
                if *early {
                    // Strictly before maturity, so the early path is genuinely
                    // taken rather than silently skipped.
                    let when = p.unlock_at - 1;
                    let expected = principal_at_open[&pid];
                    if let Ok(out) = exit_locked_early(&mut v, pid, when) {
                        r.returned += p.bonus;
                        r.early_exits += 1;
                        if out != expected && r.short_exit.is_none() {
                            r.short_exit = Some((out, expected));
                        }
                    }
                } else {
                    let when = p.unlock_at + dt;
                    if claim_locked(&mut v, pid, when).is_ok() {
                        r.claims += 1;
                    }
                }
            }
            DOp::Accrue(dt) => {
                clock += dt;
                let when = v.last_accrual + dt;
                let _ = accrue(&mut v, when);
            }
            DOp::Redeem(frac) => {
                // Only the float is presentable. The seed units, the pool and
                // the escrow are all balances no caller holds.
                let float = v
                    .supply
                    .saturating_sub(v.dead_units)
                    .saturating_sub(v.subsidy_units)
                    .saturating_sub(v.escrow_units);
                if float > 0 {
                    let units = (float * frac / 10_000).max(1);
                    let _ = redeem(&mut v, units);
                }
            }
        }

        if !may_move && (v.holdings != before_holdings || v.supply != before_supply) {
            r.vault_moved = true;
        }
        if v.subsidy_units.saturating_add(v.escrow_units) > v.supply {
            r.unbacked = true;
        }
    }
    (v, r)
}

prop_compose! {
    fn arb_discount_case()(
        v in arb_vault(),
        ops in prop::collection::vec(arb_dop(), 1..14),
    ) -> (Vault, Vec<DOp>) { (v, ops) }
}

proptest! {
    #![proptest_config(cfg())]

    // ----------------------------------------------------------------- A11
    /// P1 — the pool is never overdrawn and the books balance.
    ///
    /// The creator's exposure is exactly what they funded, whatever order the
    /// operations arrive in and wherever the dials are set. The ledger is
    /// closed: every unit in the pool arrived through `fund_subsidy` or came
    /// back from a forfeited bonus, and every unit that left went out as a
    /// bonus or a withdrawal.
    #[test]
    fn a11_pool_is_never_overdrawn_and_the_books_balance(
        (v0, ops) in arb_discount_case(),
    ) {
        let (v, r) = run_discount_sequence(&v0, &ops);
        prop_assert!(!r.overdrawn, "a bonus larger than the pool was paid: {:?}", r);
        let credits = r.funded + r.returned;
        let debits = r.withdrawn + r.paid_out;
        prop_assert!(credits >= debits, "pool went negative: {:?}", r);
        prop_assert_eq!(credits - debits, v.subsidy_units,
            "pool accounting drifted: {:?}", r);
    }

    // ----------------------------------------------------------------- A12
    /// P2 — an early exit returns the depositor's own units IN FULL, regardless
    /// of how the creator moved the dials while the position was open.
    ///
    /// This is the promise the whole locked path rests on. `exit_locked_early`
    /// reads no dial, applies no penalty and does not touch the principal; the
    /// bonus is the creator's to reclaim and the principal never is.
    #[test]
    fn a12_early_exit_returns_principal_in_full(
        (v0, ops) in arb_discount_case(),
    ) {
        let (_, r) = run_discount_sequence(&v0, &ops);
        prop_assert!(r.short_exit.is_none(),
            "early exit shortchanged the depositor: {:?}", r.short_exit);
    }

    // ----------------------------------------------------------------- A13
    /// P3 — no pool-only operation moves vault holdings or total supply.
    ///
    /// The bonus is a transfer out of a pool the creator funded, not a mint, so
    /// nobody is diluted by any of it. If funding, withdrawing, dialling,
    /// claiming or exiting ever changed `supply`, discount mode would be issuing
    /// units against the same basket and every existing holder would be paying
    /// for the subsidy.
    #[test]
    fn a13_pool_operations_never_touch_the_vault(
        (v0, ops) in arb_discount_case(),
    ) {
        let (_, r) = run_discount_sequence(&v0, &ops);
        prop_assert!(!r.vault_moved, "a pool operation moved the vault: {:?}", r);
    }

    // ----------------------------------------------------------------- A14
    /// P4 — the pool and the escrow are always backed by units that exist.
    ///
    /// Both are balances of the unit mint, so `subsidy + escrow` can never
    /// exceed `supply`. The Python reference gained this check after
    /// `fund_subsidy` was found crediting the pool with units that were never
    /// minted; on chain the transfer would fail, and the core has to refuse it
    /// for the same reason.
    #[test]
    fn a14_pool_and_escrow_are_always_backed(
        (v0, ops) in arb_discount_case(),
    ) {
        let (v, r) = run_discount_sequence(&v0, &ops);
        prop_assert!(!r.unbacked, "pool + escrow exceeded supply: {:?}", r);
        prop_assert!(invariants(&v), "invariants broken: {:?}", v);
    }

    // ----------------------------------------------------------------- A15
    /// P5 — the creator can never reach units sitting in escrow.
    ///
    /// Checked twice over: every withdrawal of more than the pool holds is
    /// refused even when escrow sits next to it, and no accepted withdrawal
    /// changes the escrow total or any open position.
    #[test]
    fn a15_creator_cannot_reach_escrowed_units(
        (v0, ops) in arb_discount_case(),
    ) {
        let (v, r) = run_discount_sequence(&v0, &ops);
        prop_assert!(!r.overwithdrew, "a withdrawal beyond the pool was accepted");
        prop_assert!(!r.reached_escrow, "a withdrawal moved escrowed units");
        // And the escrow still adds up to exactly the open positions.
        let sum: u128 = v.positions.values().map(|p| p.principal + p.bonus).sum();
        prop_assert_eq!(sum, v.escrow_units, "escrow counter drifted from the positions");
    }
}

/// A11..A15 all pass trivially against a sequence where nothing ever happens —
/// no bonus paid, no position opened, no early exit taken. That failure mode is
/// silent, and it is exactly what a future change to the generator or to
/// `bonus_for` would cause. So sample the same strategy and assert the
/// interesting paths are actually being reached.
///
/// The thresholds are deliberately far below the observed rates; this is a
/// tripwire for a collapse, not a measurement.
#[test]
fn a11_to_a15_actually_exercise_the_paths_they_claim_to() {
    use proptest::strategy::ValueTree;
    use proptest::test_runner::{Config, RngAlgorithm, TestRng, TestRunner};

    // Seeded, so the numbers below are a fact about the strategy rather than a
    // coin flip that fails one run in fifty.
    let mut runner = TestRunner::new_with_rng(
        Config {
            cases: 1,
            failure_persistence: None,
            ..Config::default()
        },
        TestRng::deterministic_rng(RngAlgorithm::ChaCha),
    );
    let strat = arb_discount_case();

    let (mut paid, mut opened, mut early, mut claimed) = (0u32, 0u32, 0u32, 0u32);
    let trials = 10_000;
    for _ in 0..trials {
        let (v0, ops) = strat.new_tree(&mut runner).unwrap().current();
        let (_, r) = run_discount_sequence(&v0, &ops);
        paid += r.liquid_paid;
        opened += r.locked_opened;
        early += r.early_exits;
        claimed += r.claims;
    }
    assert!(paid > trials / 20, "liquid bonuses paid: {paid} in {trials} sequences");
    assert!(opened > trials / 5, "locked positions opened: {opened}");
    assert!(early > trials / 40, "early exits taken: {early}");
    assert!(claimed > trials / 40, "matured claims: {claimed}");
    eprintln!(
        "coverage over {trials} sequences: {paid} liquid bonuses, {opened} positions opened, \
         {early} early exits, {claimed} claims"
    );
}
