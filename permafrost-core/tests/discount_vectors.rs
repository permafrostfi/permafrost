//! Port of every case in `permafrost_discount_vectors.json`.
//!
//! The vectors were produced by `gen_discount_vectors.py` running the Python
//! reference, so this file rebuilds the same starting vault and walks each case
//! through the same calls, asserting every recorded expectation.
//!
//! `permafrost_test_vectors.json` and its `vectors.rs` are untouched: those are
//! the frozen V1..V16 chain, and making room in them for a new feature is how a
//! test suite quietly becomes a rubber stamp. Discount mode gets its own chain.
//!
//! No expected value in this file has been adjusted to make a test pass.

use permafrost_core::*;

const USDC: u128 = 1_000_000;
const NOW: i64 = 1_767_225_600;
const ENTRY_BP: u128 = 30;
const POOL: u128 = 100 * UNIT;

fn prices() -> [u128; 4] {
    [220 * USDC, 340 * USDC, 510 * USDC, 575 * USDC]
}

/// `gen_discount_vectors.buy_ratio` — delivered amounts costing `budget_usdc`
/// at `prices()`, in the vault's own ratio.
fn buy_ratio(v: &Vault, budget_usdc: u128) -> [u128; 4] {
    let p = prices();
    let total: u128 = (0..4).map(|i| v.holdings[i] * p[i] / TOK).sum();
    let k = budget_usdc * K / total;
    let mut out = [0u128; 4];
    for i in 0..4 {
        out[i] = v.holdings[i] * k / K;
    }
    out
}

// ------------------------------------------------------------------ D0 base

/// One seeded vault plus one ordinary $2,000 deposit. Every case forks from
/// exactly this state, so a divergence here would surface as a failure in all
/// six rather than as a plausible-looking wrong answer in one.
fn base() -> Vault {
    let p = prices();
    let per_leg = 25 * USDC / 4;
    let mut seed_tokens = [0u128; 4];
    for i in 0..4 {
        seed_tokens[i] = per_leg * TOK / p[i];
    }
    let mut v = seed(seed_tokens, 25 * UNIT, NOW).expect("seed");
    v.entry_bp = ENTRY_BP;
    let d = buy_ratio(&v, 2_000 * USDC);
    deposit(&mut v, d).expect("base deposit");
    v
}

/// `gen_discount_vectors.fork`.
fn fork(pool: u128, on: bool, dbp: u128, lbp: u128, cap: u128) -> Vault {
    let mut v = base();
    fund_subsidy(&mut v, pool).expect("fund");
    v.discount_on = on;
    set_discount(&mut v, dbp, lbp, cap).expect("set_discount");
    assert!(invariants(&v));
    v
}

#[test]
fn d0_shared_starting_state() {
    let v = base();
    assert_eq!(
        v.holdings,
        [230_113_681, 148_897_068, 99_264_712, 88_043_455],
        "D0 holdings"
    );
    assert_eq!(v.supply, 2_025_000_437_000, "D0 supply");
    assert_eq!(v.creator_units, 6_000_001_311, "D0 creator_units");
    assert!(invariants(&v));
}

// ------------------------------------------------------------ D1 rate binds

/// The liquid path with room to spare in both the cap and the pool, so the RATE
/// is the binding ceiling. 50bp is the middle of the intended operating range
/// and half the immutable 100bp maximum.
#[test]
fn d1_liquid_deposit_rate_is_the_binding_ceiling() {
    let mut v = fork(POOL, true, 50, 0, 10 * UNIT);
    let d = buy_ratio(&v, 1_000 * USDC);
    assert_eq!(d, [113_636_384, 73_529_415, 49_019_610, 43_478_248], "D1 delivered");

    let supply_before = v.supply;
    let o = deposit_discounted(&mut v, d).expect("D1 deposit");

    assert_eq!(o.minted_to_depositor, 997_000_160_517, "D1 minted");
    assert_eq!(o.bonus, 4_985_000_802, "D1 bonus");
    assert_eq!(o.to_depositor, 1_001_985_161_319, "D1 to_depositor total");
    assert_eq!(o.to_creator, 3_000_000_482, "D1 to_creator");
    assert_eq!(o.refunds, [2, 2, 1, 1], "D1 refunds");
    assert_eq!(v.subsidy_units, 95_014_999_198, "D1 pool after");
    assert_eq!(v.supply, 3_025_000_597_999, "D1 supply");

    // The bonus is a transfer, not a mint: the whole supply move is the deposit.
    assert_eq!(
        v.supply - supply_before,
        o.minted_to_depositor + o.to_creator,
        "D1 the bonus must not have minted anything"
    );
    // And it is exactly the rate applied to the depositor's own units.
    assert_eq!(o.bonus, o.minted_to_depositor * 50 / BP, "D1 bonus formula");
    assert!(invariants(&v));
}

// -------------------------------------------------------------- D2 maturity

/// Held to maturity: the claim pays principal + bonus, and the pool does NOT
/// get the bonus back — that is what the creator bought.
#[test]
fn d2_locked_deposit_held_to_maturity() {
    let mut v = fork(POOL, true, 0, 2_500, 1_000 * UNIT);
    let d = buy_ratio(&v, 200 * USDC);
    assert_eq!(d, [22_727_276, 14_705_883, 9_803_922, 8_695_649], "D2 delivered");

    let o = deposit_locked(&mut v, d, NOW).expect("D2 deposit");
    assert_eq!(o.position_id, 1, "D2 position id");
    assert_eq!(o.principal, 199_400_000_000, "D2 principal");
    assert_eq!(o.bonus, 49_850_000_000, "D2 bonus");
    assert_eq!(o.unlock_at, NOW + LOCK_PERIOD, "D2 unlock_at");
    assert_eq!(o.unlock_at, 1_777_593_600, "D2 unlock_at absolute");
    assert_eq!(v.escrow_units, 249_250_000_000, "D2 escrow after open");
    assert!(invariants(&v));

    // One second early is still early — the boundary is `now >= unlock_at`.
    assert_eq!(
        claim_locked(&mut v, o.position_id, o.unlock_at - 1),
        Err(PfError::NotMatured),
        "D2 a claim one second early must be refused"
    );

    let claimed = claim_locked(&mut v, o.position_id, o.unlock_at).expect("D2 claim");
    assert_eq!(claimed, 249_250_000_000, "D2 claimed");
    assert_eq!(v.escrow_units, 0, "D2 escrow after claim");
    assert_eq!(v.subsidy_units, 50_150_000_000, "D2 pool after claim");
    assert!(v.positions.is_empty(), "D2 position closed");
    assert!(invariants(&v));
}

// ------------------------------------------------------------ D3 early exit

/// Exited one second before maturity: principal back IN FULL, bonus back to the
/// pool, no penalty. This is the promise the locked path rests on.
#[test]
fn d3_locked_deposit_exited_early() {
    let mut v = fork(POOL, true, 0, 2_500, 1_000 * UNIT);
    let d = buy_ratio(&v, 200 * USDC);
    let o = deposit_locked(&mut v, d, NOW).expect("D3 deposit");

    assert_eq!(o.principal, 199_400_000_000, "D3 principal");
    assert_eq!(o.bonus, 49_850_000_000, "D3 bonus");
    assert_eq!(v.subsidy_units, 50_150_000_000, "D3 pool after open");

    // At maturity exactly, the early path is closed.
    assert_eq!(
        exit_locked_early(&mut v, o.position_id, o.unlock_at),
        Err(PfError::AlreadyMatured),
        "D3 an early exit at maturity must be refused"
    );

    let returned =
        exit_locked_early(&mut v, o.position_id, o.unlock_at - 1).expect("D3 exit");
    assert_eq!(returned, 199_400_000_000, "D3 returned");
    assert_eq!(returned, o.principal, "D3 principal returned in full");
    assert_eq!(v.subsidy_units, 100_000_000_000, "D3 pool restored");
    assert_eq!(v.subsidy_units, POOL, "D3 the pool is exactly whole again");
    assert_eq!(v.escrow_units, 0, "D3 escrow after exit");
    assert!(v.positions.is_empty(), "D3 position closed");
    assert!(invariants(&v));
}

// --------------------------------------------------------- D4 pool runs dry

/// The pool holds 3 units against a 100% rate on a ~199-unit deposit, so the
/// POOL is the binding ceiling. The deposit must still succeed with a smaller
/// bonus — a nearly-empty pool degrades, it does not brick deposits for
/// everyone who arrives after it.
#[test]
fn d4_pool_runs_dry_mid_deposit() {
    let mut v = fork(3 * UNIT, true, 0, 10_000, 1_000 * UNIT);
    let d = buy_ratio(&v, 200 * USDC);

    let a = deposit_locked(&mut v, d, NOW).expect("D4 first deposit");
    assert_eq!(a.principal, 199_400_000_000, "D4 first principal");
    assert_eq!(a.bonus, 3_000_000_000, "D4 first bonus");
    assert_eq!(a.bonus, 3 * UNIT, "D4 the pool, not the rate, is the ceiling");
    assert_eq!(v.subsidy_units, 0, "D4 pool empty");
    assert!(invariants(&v));

    let b = deposit_locked(&mut v, d, NOW).expect("D4 second deposit");
    assert_eq!(b.principal, 199_400_000_000, "D4 second principal");
    assert_eq!(b.bonus, 0, "D4 second bonus");
    assert_eq!(v.subsidy_units, 0, "D4 pool still empty");
    assert_eq!(v.escrow_units, 401_800_000_000, "D4 escrow");
    assert_eq!(v.positions.len(), 2, "D4 both positions open");
    assert!(invariants(&v));
}

// ---------------------------------------------------------- D5 cap binds

/// The per-deposit cap is what stops the first large deposit taking the whole
/// pool. 100 units at a 100% rate would be gone in one $200 deposit; a 7-unit
/// cap makes the same pool reach fourteen depositors instead of one.
#[test]
fn d5_per_deposit_cap_binds() {
    let mut v = fork(POOL, true, 0, 10_000, 7 * UNIT);
    let d = buy_ratio(&v, 200 * USDC);

    let o = deposit_locked(&mut v, d, NOW).expect("D5 deposit");
    assert_eq!(o.principal, 199_400_000_000, "D5 principal");
    assert_eq!(o.bonus, 7_000_000_000, "D5 bonus");
    assert_eq!(o.bonus, 7 * UNIT, "D5 the cap, not the rate or the pool, binds");
    assert_eq!(v.subsidy_units, 93_000_000_000, "D5 pool after");
    assert_eq!(v.escrow_units, 206_400_000_000, "D5 escrow");
    // Without the cap this single deposit would have taken the lot.
    assert!(
        o.principal * 10_000 / BP > POOL,
        "D5 the uncapped bonus would have exceeded the whole pool"
    );
    assert!(invariants(&v));
}

// ------------------------------------------------- D6 dials move mid-position

/// The creator turns discount mode off entirely and zeroes every dial while a
/// position is open. The position must be completely unaffected: the bonus is
/// already escrowed, and `exit_locked_early` consults no dial.
#[test]
fn d6_dials_move_while_a_position_is_open() {
    let mut v = fork(POOL, true, 0, 5_000, 1_000 * UNIT);
    let d = buy_ratio(&v, 200 * USDC);

    let o = deposit_locked(&mut v, d, NOW).expect("D6 deposit");
    assert_eq!(o.principal, 199_400_000_000, "D6 principal");
    assert_eq!(o.bonus, 99_700_000_000, "D6 bonus");
    assert_eq!(v.escrow_units, 299_100_000_000, "D6 escrow at open");
    assert_eq!(v.subsidy_units, 300_000_000, "D6 pool at open");

    v.discount_on = false;
    set_discount(&mut v, 0, 0, 0).expect("D6 dials to zero");
    assert!(invariants(&v));

    assert_eq!(v.escrow_units, 299_100_000_000, "D6 escrow after dials");
    let p = &v.positions[&o.position_id];
    assert_eq!(p.principal, 199_400_000_000, "D6 position principal after dials");
    assert_eq!(p.bonus, 99_700_000_000, "D6 position bonus after dials");
    assert_eq!(p.unlock_at, 1_777_593_600, "D6 position unlock_at after dials");

    let returned = exit_locked_early(&mut v, o.position_id, NOW + 1).expect("D6 exit");
    assert_eq!(returned, 199_400_000_000, "D6 returned");
    assert_eq!(returned, o.principal, "D6 principal returned in full");
    assert_eq!(v.subsidy_units, 100_000_000_000, "D6 pool after exit");
    assert_eq!(v.subsidy_units, POOL, "D6 the pool is exactly whole again");
    assert!(invariants(&v));
}

// ------------------------------------------------------ the refusals, pinned

/// Everything the reference expresses as an `assert` in the discount section,
/// stated once as a boundary. These are not in the JSON — the reference raises
/// rather than returning a value — but an immutable contract's refusals are
/// part of its behaviour and belong under test.
#[test]
fn d7_refusals() {
    let mut v = base();

    // Funding with units that do not exist.
    assert_eq!(fund_subsidy(&mut v, 0), Err(PfError::ZeroFundAmount));
    let supply = v.supply;
    assert_eq!(
        fund_subsidy(&mut v, supply + 1),
        Err(PfError::UnbackedSubsidy)
    );
    assert_eq!(fund_subsidy(&mut v, supply), Ok(()), "the whole supply is fundable");
    assert_eq!(
        fund_subsidy(&mut v, 1),
        Err(PfError::UnbackedSubsidy),
        "and not one unit more"
    );

    // Withdrawing more than the pool holds.
    assert_eq!(withdraw_subsidy(&mut v, 0), Err(PfError::BadWithdrawAmount));
    let pool = v.subsidy_units;
    assert_eq!(
        withdraw_subsidy(&mut v, pool + 1),
        Err(PfError::BadWithdrawAmount)
    );

    // Rates above their immutable ceilings.
    assert_eq!(
        set_discount(&mut v, MAX_DISCOUNT_BP + 1, 0, 0),
        Err(PfError::DiscountRateTooHigh)
    );
    assert_eq!(
        set_discount(&mut v, 0, MAX_LOCKED_DISCOUNT_BP + 1, 0),
        Err(PfError::DiscountRateTooHigh)
    );
    // A rejected call changes nothing.
    assert_eq!(v.discount_bp, 0);
    assert_eq!(v.locked_discount_bp, 0);
    assert_eq!(set_discount(&mut v, MAX_DISCOUNT_BP, MAX_LOCKED_DISCOUNT_BP, 1), Ok(()));

    // Closing a position that does not exist.
    assert_eq!(claim_locked(&mut v, 42, NOW), Err(PfError::NoSuchPosition));
    assert_eq!(
        exit_locked_early(&mut v, 42, NOW),
        Err(PfError::NoSuchPosition)
    );

    assert!(invariants(&v));
}

/// The three ceilings in `bonus_for`, one at a time, plus the four switches that
/// zero it outright. The whole of discount mode's exposure is this function.
#[test]
fn d8_bonus_for_ceilings() {
    let mut v = base();
    fund_subsidy(&mut v, 100 * UNIT).unwrap();
    v.discount_on = true;
    set_discount(&mut v, 100, 10_000, 1_000 * UNIT).unwrap();

    // Rate binds: 1% of 1,000 units.
    assert_eq!(bonus_for(&v, 1_000 * UNIT, 100), Ok(10 * UNIT));
    // Cap binds.
    set_discount(&mut v, 100, 10_000, 4 * UNIT).unwrap();
    assert_eq!(bonus_for(&v, 1_000 * UNIT, 100), Ok(4 * UNIT));
    // Pool binds.
    set_discount(&mut v, 100, 10_000, 1_000 * UNIT).unwrap();
    assert_eq!(bonus_for(&v, 1_000_000 * UNIT, 10_000), Ok(100 * UNIT));

    // Off, zero rate, zero cap, empty pool — each on its own pays nothing.
    v.discount_on = false;
    assert_eq!(bonus_for(&v, 1_000 * UNIT, 100), Ok(0));
    v.discount_on = true;
    assert_eq!(bonus_for(&v, 1_000 * UNIT, 0), Ok(0));
    set_discount(&mut v, 100, 10_000, 0).unwrap();
    assert_eq!(bonus_for(&v, 1_000 * UNIT, 100), Ok(0));
    set_discount(&mut v, 100, 10_000, 1_000 * UNIT).unwrap();
    let pool = v.subsidy_units;
    withdraw_subsidy(&mut v, pool).unwrap();
    assert_eq!(bonus_for(&v, 1_000 * UNIT, 100), Ok(0));

    // The floor is the fund's, not the depositor's: a rate that would pay a
    // fraction of a base unit pays nothing at all.
    fund_subsidy(&mut v, 100 * UNIT).unwrap();
    assert_eq!(bonus_for(&v, 99, 100), Ok(0), "0.99 base units floors to 0");
    assert_eq!(bonus_for(&v, 100, 100), Ok(1));
}
