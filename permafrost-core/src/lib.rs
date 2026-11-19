//! Permafrost — pure-Rust port of the arithmetic in `permafrost_reference.py`.
//!
//! Rung 1: no Solana, no network, no accounts. Just the integer maths a Solana
//! program will later wrap.
//!
//! Conventions (identical to the Python reference):
//!   unit mint      9 decimals   UNIT = 1_000_000_000
//!   xStock mints   8 decimals   TOK  = 100_000_000
//!   USDC           6 decimals
//!   k fixed point  K = 10**18
//!   all division   floor, ALWAYS — except `offered_ratio` / `fill_required`,
//!                  which ceil because they govern what ENTERS the vault.
//!   intermediates  u128 (Python uses unbounded ints; see `docs` notes in the
//!                  crate-level tests for where that difference could bite).

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub const UNIT: u128 = 1_000_000_000; // base units per whole unit token
pub const TOK: u128 = 100_000_000; // base units per whole xStock token
pub const K: u128 = 1_000_000_000_000_000_000; // 10**18
pub const BP: u128 = 10_000;
pub const YEAR: u128 = 365 * 24 * 60 * 60;

pub const NLEGS: usize = 4;
pub const LEGS: [&str; NLEGS] = ["NVDAx", "GOOGLx", "MSFTx", "METAx"];

// Fixed at deployment, unchangeable.
pub const MAX_ENTRY_BP: u128 = 100;
pub const MAX_EXIT_BP: u128 = 100;
pub const MAX_STREAM_BP: u128 = 100;
pub const FEE_TIMELOCK: i64 = 30 * 24 * 60 * 60;
pub const TRIGGER_BP: u128 = 5000;
pub const TARGET_BP: u128 = 3000;
pub const RECIPIENT_CAP_BP: u128 = 4000;
pub const MIN_REBALANCE_GAP: i64 = 180 * 24 * 60 * 60;
pub const CANDIDATE_MIN_AGE: i64 = 60 * 60;
pub const CANDIDATE_MAX_AGE: i64 = 24 * 60 * 60;

// Discount mode. All three are fixed at deployment and unchangeable.
//
// LIQUID: bonus units are handed over immediately, so the ceiling is set below
// the ~26bp round-trip execution cost — above that a bot deposits, redeems and
// drains the pool for no risk.
//
// LOCKED: bonus up to 100%, but the depositor's OWN units escrow alongside it
// for LOCK_PERIOD. Because the depositor never holds the escrowed units they
// cannot defeat the lock by moving them to a fresh wallet.
pub const MAX_DISCOUNT_BP: u128 = 100; // liquid path ceiling: 1%
pub const MAX_LOCKED_DISCOUNT_BP: u128 = 10_000; // locked path ceiling: 100%
pub const LOCK_PERIOD: i64 = 120 * 24 * 60 * 60; // 4 months

// Auction pricing.
pub const SELL_UNIT: u128 = TOK;
pub const AUCTION_START_BP: i128 = 25;
pub const AUCTION_FLOOR_BP: i128 = -150;
pub const AUCTION_DECAY: i64 = 6 * 60 * 60;
pub const AUCTION_WINDOW: i64 = 72 * 60 * 60;
pub const MIN_FILL_BP: u128 = 100;
pub const RETRY_COOLDOWN: i64 = 7 * 24 * 60 * 60;

// Pyth.
pub const USDC_EXPO: i32 = -6;
pub const PYTH_FEEDS: [(&str, &str); NLEGS] = [
    (
        "NVDAx",
        "b1073854ed24cbc755dc527418f52b7d271f6cc967bbf8d8129112b18860a593",
    ),
    (
        "GOOGLx",
        "5a48c03e9b9cb337801073ed9d166817473697efff0d138874e0f6a33d6d5aa6",
    ),
    (
        "MSFTx",
        "d0ca23c1cc005e004ccf1db5bf76aeb6a49218f43dac3d4b275e92de12ded4d1",
    ),
    (
        "METAx",
        "78a3e3b8e676a8f73c439f5d749737034b139bbbe899ba5775216fba596607fe",
    ),
];

// ------------------------------------------------------------------ errors

/// Every failure the reference expresses as an `assert` or a `raise` becomes a
/// variant here. Nothing in this crate panics on caller input.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum PfError {
    /// `seed`: some leg was seeded with zero.
    EmptySeedLeg,
    /// `seed`: initial_units == 0.
    ZeroInitialUnits,
    /// `deposit`/`redeem`: vault not seeded (supply == 0).
    UnseededVault,
    /// `deposit`: some holding is zero, so its ratio is undefined.
    EmptyLeg,
    /// `deposit`: nothing was retained, so nothing may be minted (vector V4).
    DustDeposit,
    /// `redeem`: units_in == 0 or units_in > supply.
    BadRedeemAmount,
    /// `accrue`: stream_bp * elapsed >= BP * YEAR — the m = S*f/(1-f) form blows up.
    AccrualWindowTooLong,
    /// `weights_bp`: the basket has zero total value.
    ZeroBasketValue,
    /// `rebalance_plan` / `fair_ratio`: a price or a token count was zero.
    ZeroPrice,
    /// `fair_ratio`: the plan sells nothing.
    NothingToSell,
    /// An intermediate exceeded u128. Python would carry on with a bignum.
    Overflow,

    // ---------------------------------------------------------- discount mode
    /// `set_discount` / `deposit_discounted` / `deposit_locked`: a rate is above
    /// its immutable ceiling.
    DiscountRateTooHigh,
    /// `fund_subsidy`: units == 0.
    ZeroFundAmount,
    /// `fund_subsidy`: pool + escrow + amount would exceed the minted supply,
    /// i.e. the creator tried to fund with units that do not exist. On chain the
    /// token transfer simply fails; here it has to be refused explicitly.
    UnbackedSubsidy,
    /// `withdraw_subsidy`: units == 0 or more than the pool holds.
    BadWithdrawAmount,
    /// `claim_locked` / `exit_locked_early`: no open position with that id.
    NoSuchPosition,
    /// `claim_locked`: the position has not matured yet.
    NotMatured,
    /// `exit_locked_early`: the position has matured; use `claim_locked`.
    AlreadyMatured,
}

pub type Res<T> = Result<T, PfError>;

// ------------------------------------------------------- checked arithmetic
//
// Every multiply-then-divide goes through these so a u128 overflow surfaces as
// `PfError::Overflow` rather than a panic or a silent wrap. The Python
// reference has unbounded ints and therefore no equivalent failure mode; the
// differential harness stays well inside u128 so the two never diverge there.

#[inline]
fn mul(a: u128, b: u128) -> Res<u128> {
    a.checked_mul(b).ok_or(PfError::Overflow)
}

#[inline]
fn add(a: u128, b: u128) -> Res<u128> {
    a.checked_add(b).ok_or(PfError::Overflow)
}

/// Floor division. `b` must be non-zero (callers check).
#[inline]
fn fdiv(a: u128, b: u128) -> Res<u128> {
    if b == 0 {
        return Err(PfError::ZeroPrice);
    }
    Ok(a / b)
}

/// `(a * b) / c`, floored, with the multiply done first and in u128.
#[inline]
fn muldiv(a: u128, b: u128, c: u128) -> Res<u128> {
    fdiv(mul(a, b)?, c)
}

/// Ceiling division on unsigned values: `-(-a // b)` in Python terms.
#[inline]
pub fn ceil_div_u128(a: u128, b: u128) -> Res<u128> {
    if b == 0 {
        return Err(PfError::ZeroPrice);
    }
    if a == 0 {
        return Ok(0);
    }
    Ok((a - 1) / b + 1)
}

/// Python's floor division for signed values (rounds toward -inf, unlike Rust's
/// `/`, which truncates toward zero). Needed because Pyth prices minus a
/// confidence interval can legitimately go negative.
#[inline]
pub fn py_floor_div_i128(a: i128, b: i128) -> i128 {
    let q = a / b;
    if (a % b != 0) && ((a < 0) != (b < 0)) { q - 1 } else { q }
}

/// Python's `ceil_div` = `-(-a // b)`, for signed values.
#[inline]
pub fn py_ceil_div_i128(a: i128, b: i128) -> i128 {
    -py_floor_div_i128(-a, b)
}

// ------------------------------------------------------------------- vault

/// One locked deposit. The escrow holds principal + bonus until `unlock_at`.
///
/// `owner` is an opaque id here and a pubkey on chain. The reference sets it to
/// the position id; this port keeps that so the differential harness compares
/// like for like.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Position {
    pub owner: u64,
    /// The depositor's own units — returned in full on an early exit.
    pub principal: u128,
    /// Subsidised units — forfeit back to the pool on an early exit.
    pub bonus: u128,
    pub unlock_at: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Vault {
    pub holdings: [u128; NLEGS],
    pub supply: u128,
    pub entry_bp: u128,
    pub exit_bp: u128,
    pub stream_bp: u128,
    pub last_accrual: i64,
    pub creator_units: u128,
    pub dead_units: u128,

    // ---------------------------------------------------------- discount mode
    /// The pool the creator funds out of their own units. NOT a vault asset:
    /// bonuses are transfers out of it, never mints, so nobody is diluted.
    pub subsidy_units: u128,
    /// Master toggle, creator-controlled.
    pub discount_on: bool,
    /// Liquid path rate, <= MAX_DISCOUNT_BP.
    pub discount_bp: u128,
    /// Locked path rate, <= MAX_LOCKED_DISCOUNT_BP.
    pub locked_discount_bp: u128,
    /// Per-deposit ceiling; 0 disables bonuses entirely.
    pub max_bonus_units: u128,
    /// Total held across all open positions.
    pub escrow_units: u128,
    /// A `BTreeMap` rather than a `HashMap`: iteration order is part of the
    /// observable behaviour here (the invariant sums over it and the harness
    /// serialises it), and it must not depend on a hasher seed.
    pub positions: BTreeMap<u64, Position>,
    pub next_position_id: u64,
}

impl Default for Vault {
    fn default() -> Self {
        Vault {
            holdings: [0; NLEGS],
            supply: 0,
            entry_bp: 0,
            exit_bp: 0,
            stream_bp: 0,
            last_accrual: 0,
            creator_units: 0,
            dead_units: 0,
            subsidy_units: 0,
            discount_on: false,
            discount_bp: 0,
            locked_discount_bp: 0,
            max_bonus_units: 0,
            escrow_units: 0,
            positions: BTreeMap::new(),
            // The reference starts position ids at 1, so 0 is never a valid id.
            next_position_id: 1,
        }
    }
}

impl Vault {
    pub fn new(holdings: [u128; NLEGS], supply: u128) -> Self {
        Vault {
            holdings,
            supply,
            ..Default::default()
        }
    }
}

// ---------------------------------------------------------------- seeding

/// The one and only deposit split equally by dollars.
///
/// The seed units go to an unspendable address, they are NOT destroyed:
/// supply must remain non-zero forever.
pub fn seed(amounts: [u128; NLEGS], initial_units: u128, now: i64) -> Res<Vault> {
    if amounts.iter().any(|&a| a == 0) {
        return Err(PfError::EmptySeedLeg);
    }
    if initial_units == 0 {
        return Err(PfError::ZeroInitialUnits);
    }
    Ok(Vault {
        holdings: amounts,
        supply: initial_units,
        last_accrual: now,
        dead_units: initial_units,
        ..Default::default()
    })
}

// ---------------------------------------------------------------- deposit

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DepositOut {
    pub to_depositor: u128,
    pub to_creator: u128,
    pub refunds: [u128; NLEGS],
    /// Exposed for tests; the reference keeps it local.
    pub gross: u128,
    pub required: [u128; NLEGS],
    pub k: u128,
}

/// Mint on the smallest delivered ratio; refund the rest.
pub fn deposit(v: &mut Vault, delivered: [u128; NLEGS]) -> Res<DepositOut> {
    if v.supply == 0 {
        return Err(PfError::UnseededVault);
    }
    if v.holdings.iter().any(|&h| h == 0) {
        return Err(PfError::EmptyLeg);
    }

    let mut k = u128::MAX;
    for i in 0..NLEGS {
        let ki = muldiv(delivered[i], K, v.holdings[i])?;
        if ki < k {
            k = ki;
        }
    }

    // Only the proportional part is kept; the excess on every other leg goes back.
    let mut required = [0u128; NLEGS];
    let mut refunds = [0u128; NLEGS];
    for i in 0..NLEGS {
        required[i] = muldiv(v.holdings[i], k, K)?;
        // `required[i] <= delivered[i]` holds because k <= delivered[i]*K/holdings[i];
        // guard anyway rather than underflow.
        if required[i] > delivered[i] {
            return Err(PfError::Overflow);
        }
        refunds[i] = delivered[i] - required[i];
    }

    // Mint against what is ACTUALLY retained, not against k.
    //
    // Deriving gross from k alone is wrong: flooring `required` can drive every
    // leg to zero while k stays positive, so a dust deposit that is entirely
    // refunded would still mint units out of nothing. Recomputing the ratio
    // from the retained amounts closes that, and the second floor can only ever
    // round in the fund's favour.
    let mut gross = u128::MAX;
    for i in 0..NLEGS {
        let g = muldiv(v.supply, required[i], v.holdings[i])?;
        if g < gross {
            gross = g;
        }
    }
    if gross == 0 {
        return Err(PfError::DustDeposit);
    }

    let to_creator = muldiv(gross, v.entry_bp, BP)?;
    let to_depositor = gross - to_creator;

    for i in 0..NLEGS {
        v.holdings[i] = add(v.holdings[i], required[i])?;
    }
    v.supply = add(v.supply, gross)?;
    v.creator_units = add(v.creator_units, to_creator)?;

    Ok(DepositOut {
        to_depositor,
        to_creator,
        refunds,
        gross,
        required,
        k,
    })
}

// ----------------------------------------------------------------- redeem

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RedeemOut {
    pub payouts: [u128; NLEGS],
    pub burned: u128,
    pub to_creator: u128,
}

/// Burn units, pay out the pro-rata share of every leg.
///
/// The exit fee is taken by transferring a slice of the units to the creator
/// instead of burning them, so the payout is computed on what is actually burnt.
pub fn redeem(v: &mut Vault, units_in: u128) -> Res<RedeemOut> {
    if units_in == 0 || units_in > v.supply {
        return Err(PfError::BadRedeemAmount);
    }
    let to_creator = muldiv(units_in, v.exit_bp, BP)?;
    let burned = units_in - to_creator;

    let mut payouts = [0u128; NLEGS];
    for i in 0..NLEGS {
        payouts[i] = muldiv(v.holdings[i], burned, v.supply)?;
    }
    for i in 0..NLEGS {
        v.holdings[i] -= payouts[i]; // payouts[i] <= holdings[i] since burned <= supply
    }
    v.supply -= burned;
    v.creator_units = add(v.creator_units, to_creator)?;

    Ok(RedeemOut {
        payouts,
        burned,
        to_creator,
    })
}

// ------------------------------------------------------------ streaming fee

/// Mint the creator's slice for elapsed time. Permissionless crank.
///
/// To make the creator's new units equal fraction f of the resulting supply:
///   m / (S + m) = f   =>   m = S*f / (1 - f)
/// with f = stream_bp * elapsed / (BP * YEAR), computed in integers.
pub fn accrue(v: &mut Vault, now: i64) -> Res<u128> {
    let elapsed = now - v.last_accrual;
    if elapsed <= 0 || v.stream_bp == 0 {
        v.last_accrual = v.last_accrual.max(now);
        return Ok(0);
    }
    let num = mul(v.stream_bp, elapsed as u128)?;
    let den = mul(BP, YEAR)?;
    if num >= den {
        return Err(PfError::AccrualWindowTooLong);
    }
    let minted = muldiv(v.supply, num, den - num)?;
    v.supply = add(v.supply, minted)?;
    v.creator_units = add(v.creator_units, minted)?;
    v.last_accrual = now;
    Ok(minted)
}

// ----------------------------------------------------------- discount mode
//
// Port of the `discount mode` section of `permafrost_reference.py`, bit-exact.
// Every bonus is a TRANSFER out of the pool, never a mint: `supply` is untouched
// by all of it, so no existing holder is diluted by any of these instructions.

/// Units the pool will contribute for a deposit that minted `user_units`.
///
/// Three ceilings, all binding: the rate, the per-deposit cap the creator sets,
/// and what is actually left in the pool. Taking the minimum rather than failing
/// means a nearly-empty pool degrades to a smaller bonus instead of bricking
/// deposits for everyone who arrives after it runs dry.
pub fn bonus_for(v: &Vault, user_units: u128, rate_bp: u128) -> Res<u128> {
    if !v.discount_on || rate_bp == 0 || v.max_bonus_units == 0 {
        return Ok(0);
    }
    let by_rate = muldiv(user_units, rate_bp, BP)?;
    Ok(by_rate.min(v.max_bonus_units).min(v.subsidy_units))
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DiscountedDepositOut {
    /// What the depositor walks away with: their own minted units plus the bonus.
    pub to_depositor: u128,
    pub to_creator: u128,
    pub refunds: [u128; NLEGS],
    pub bonus: u128,
    /// The depositor's own share before the bonus was added, kept for tests.
    pub minted_to_depositor: u128,
}

/// Liquid path. Bonus units handed over immediately, capped at 1%.
///
/// No entry fee is charged on the bonus — the fee was already taken on what the
/// depositor's own delivery minted, and the creator does not charge themselves.
pub fn deposit_discounted(v: &mut Vault, delivered: [u128; NLEGS]) -> Res<DiscountedDepositOut> {
    if v.discount_bp > MAX_DISCOUNT_BP {
        return Err(PfError::DiscountRateTooHigh);
    }
    let d = deposit(v, delivered)?;
    let bonus = bonus_for(v, d.to_depositor, v.discount_bp)?;
    // `bonus_for` already clamps to the pool, so this cannot underflow.
    v.subsidy_units -= bonus;
    Ok(DiscountedDepositOut {
        to_depositor: add(d.to_depositor, bonus)?,
        to_creator: d.to_creator,
        refunds: d.refunds,
        bonus,
        minted_to_depositor: d.to_depositor,
    })
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LockedDepositOut {
    pub position_id: u64,
    pub principal: u128,
    pub bonus: u128,
    pub to_creator: u128,
    pub refunds: [u128; NLEGS],
    pub unlock_at: i64,
}

/// Locked path. Principal AND bonus escrow together for `LOCK_PERIOD`.
///
/// The depositor receives nothing spendable now. That is the point: a bonus the
/// holder can redeem in the same block is a giveaway, and a bonus locked while
/// the principal stays liquid is a free option. Locking both means the money
/// that earns the bonus is money genuinely at risk in the fund.
pub fn deposit_locked(v: &mut Vault, delivered: [u128; NLEGS], now: i64) -> Res<LockedDepositOut> {
    if v.locked_discount_bp > MAX_LOCKED_DISCOUNT_BP {
        return Err(PfError::DiscountRateTooHigh);
    }
    let d = deposit(v, delivered)?;
    let bonus = bonus_for(v, d.to_depositor, v.locked_discount_bp)?;
    v.subsidy_units -= bonus;

    let pid = v.next_position_id;
    v.next_position_id = v
        .next_position_id
        .checked_add(1)
        .ok_or(PfError::Overflow)?;
    let unlock_at = now.checked_add(LOCK_PERIOD).ok_or(PfError::Overflow)?;
    let escrowed = add(d.to_depositor, bonus)?;
    v.positions.insert(
        pid,
        Position {
            owner: pid,
            principal: d.to_depositor,
            bonus,
            unlock_at,
        },
    );
    v.escrow_units = add(v.escrow_units, escrowed)?;

    Ok(LockedDepositOut {
        position_id: pid,
        principal: d.to_depositor,
        bonus,
        to_creator: d.to_creator,
        refunds: d.refunds,
        unlock_at,
    })
}

/// At or after maturity: the depositor takes principal + bonus.
pub fn claim_locked(v: &mut Vault, pid: u64, now: i64) -> Res<u128> {
    let p = v.positions.get(&pid).ok_or(PfError::NoSuchPosition)?;
    if now < p.unlock_at {
        return Err(PfError::NotMatured);
    }
    let out = add(p.principal, p.bonus)?;
    v.escrow_units -= out; // the escrow total always includes this position
    v.positions.remove(&pid);
    Ok(out)
}

/// Before maturity: principal back IN FULL, bonus returns to the pool.
///
/// Note what this deliberately does not do: it does not touch the principal, it
/// does not apply a penalty, and it does not consult any of the dials. Whatever
/// the creator has changed in the meantime, the depositor's own units come back
/// unchanged. Redemption is then the ordinary unconditional path.
pub fn exit_locked_early(v: &mut Vault, pid: u64, now: i64) -> Res<u128> {
    let p = v.positions.get(&pid).ok_or(PfError::NoSuchPosition)?;
    if now >= p.unlock_at {
        return Err(PfError::AlreadyMatured);
    }
    let principal = p.principal;
    let bonus = p.bonus;
    v.escrow_units -= add(principal, bonus)?;
    v.subsidy_units = add(v.subsidy_units, bonus)?;
    v.positions.remove(&pid);
    Ok(principal)
}

/// Creator moves their own units into the pool. Not a vault asset.
///
/// The pool and the escrow are both holdings of the unit mint, so together they
/// can never exceed what has been minted. On chain the token transfer would
/// simply fail; here it has to be refused explicitly or the model would happily
/// run a pool funded out of nothing.
pub fn fund_subsidy(v: &mut Vault, units: u128) -> Res<()> {
    if units == 0 {
        return Err(PfError::ZeroFundAmount);
    }
    let committed = add(add(v.subsidy_units, v.escrow_units)?, units)?;
    if committed > v.supply {
        return Err(PfError::UnbackedSubsidy);
    }
    v.subsidy_units += units;
    Ok(())
}

/// Creator takes units back out. Cannot reach escrowed units or vault holdings.
pub fn withdraw_subsidy(v: &mut Vault, units: u128) -> Res<()> {
    if units == 0 || units > v.subsidy_units {
        return Err(PfError::BadWithdrawAmount);
    }
    v.subsidy_units -= units;
    Ok(())
}

/// Creator-controlled, no timelock.
///
/// Unlike the fee switch, none of these can take anything from a user — the
/// worst a hostile creator can do is stop giving their own money away. So there
/// is nothing for a timelock to protect against.
///
/// Changing a dial never affects an open position: the bonus is already
/// escrowed, and `exit_locked_early` consults no dial.
pub fn set_discount(
    v: &mut Vault,
    discount_bp: u128,
    locked_discount_bp: u128,
    max_bonus_units: u128,
) -> Res<()> {
    if discount_bp > MAX_DISCOUNT_BP || locked_discount_bp > MAX_LOCKED_DISCOUNT_BP {
        return Err(PfError::DiscountRateTooHigh);
    }
    v.discount_bp = discount_bp;
    v.locked_discount_bp = locked_discount_bp;
    v.max_bonus_units = max_bonus_units;
    Ok(())
}

// -------------------------------------------------------------- invariants

pub fn invariants(v: &Vault) -> bool {
    if v.supply == 0 || v.supply < v.dead_units {
        return false;
    }
    // The escrow total must be exactly the sum of the open positions — a stale
    // counter here would let the creator withdraw units a depositor still owns.
    let mut escrowed = 0u128;
    for p in v.positions.values() {
        let Some(each) = p.principal.checked_add(p.bonus) else {
            return false;
        };
        let Some(t) = escrowed.checked_add(each) else {
            return false;
        };
        escrowed = t;
    }
    if escrowed != v.escrow_units {
        return false;
    }
    // The pool and the escrow are both balances of the unit mint, so together
    // they can never exceed what has been minted.
    match v.subsidy_units.checked_add(v.escrow_units) {
        Some(t) => t <= v.supply,
        None => false,
    }
}

/// USDC base units per whole unit token, as an exact rational scaled by `UNIT`.
/// The reference returns a float; here we keep the numerator so tests can
/// compare without floating point.
pub fn unit_value_num(v: &Vault, prices: &[u128; NLEGS]) -> Res<u128> {
    let mut total = 0u128;
    for i in 0..NLEGS {
        total = add(total, muldiv(v.holdings[i], prices[i], TOK)?)?;
    }
    mul(total, UNIT)
}

pub fn basket_value(holdings: &[u128; NLEGS], prices: &[u128; NLEGS]) -> Res<u128> {
    let mut total = 0u128;
    for i in 0..NLEGS {
        total = add(total, muldiv(holdings[i], prices[i], TOK)?)?;
    }
    Ok(total)
}

// -------------------------------------------------------------- rebalancing

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Weights {
    pub vals: [u128; NLEGS],
    pub total: u128,
    pub w: [u128; NLEGS],
}

/// Basis-point weight of each leg by value. Prices are USDC base units per
/// whole token, so value is `holdings * price / TOK`.
pub fn weights_bp(holdings: &[u128; NLEGS], prices: &[u128; NLEGS]) -> Res<Weights> {
    let mut vals = [0u128; NLEGS];
    let mut total = 0u128;
    for i in 0..NLEGS {
        vals[i] = muldiv(holdings[i], prices[i], TOK)?;
        total = add(total, vals[i])?;
    }
    if total == 0 {
        return Err(PfError::ZeroBasketValue);
    }
    let mut w = [0u128; NLEGS];
    for i in 0..NLEGS {
        w[i] = muldiv(vals[i], BP, total)?;
    }
    Ok(Weights { vals, total, w })
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Plan {
    pub leg: usize,
    pub weights_bp: [u128; NLEGS],
    pub total_value: u128,
    pub sell_value_wanted: u128,
    pub sell_value_placed: u128,
    pub capped: bool,
    /// Indexed by leg; the entry for `leg` itself is always 0 (the reference
    /// keys a dict by the three other legs).
    pub alloc_value: [u128; NLEGS],
    /// Fixed token ratio, computed ONCE here. `fill` never reads a price.
    pub sell_tokens: u128,
    pub buy_tokens: [u128; NLEGS],
}

/// Decide the swap. `Ok(None)` if no leg breaches the trigger.
///
/// Sell enough of the breaching leg to bring it to 30% of basket value, and
/// allocate the proceeds in EQUAL VALUE THIRDS to the other three — capped so
/// that no recipient ends above 40%, with any excess redistributed to the
/// recipients that still have room.
pub fn rebalance_plan(holdings: &[u128; NLEGS], prices: &[u128; NLEGS]) -> Res<Option<Plan>> {
    let Weights { vals, total, w } = weights_bp(holdings, prices)?;

    // Python's `max(range(4), key=...)` returns the FIRST maximal index.
    let mut j = 0usize;
    for i in 1..NLEGS {
        if w[i] > w[j] {
            j = i;
        }
    }
    if w[j] <= TRIGGER_BP {
        return Ok(None);
    }

    let target_value = muldiv(total, TARGET_BP, BP)?;
    // w[j] > 5000 means vals[j] > total/2 > total*3000/BP, so this cannot underflow.
    let sell_value = vals[j] - target_value;
    let cap_value = muldiv(total, RECIPIENT_CAP_BP, BP)?;

    let others: Vec<usize> = (0..NLEGS).filter(|&i| i != j).collect();
    let mut alloc = [0u128; NLEGS];
    let mut room = [0u128; NLEGS];
    for &i in &others {
        room[i] = cap_value.saturating_sub(vals[i]);
    }
    let mut remaining = sell_value;
    let mut open_legs: Vec<usize> = others.iter().copied().filter(|&i| room[i] > 0).collect();

    while remaining > 0 && !open_legs.is_empty() {
        let share = remaining / (open_legs.len() as u128);
        if share == 0 {
            // Dust: hand the remainder to the leg with the most room.
            // Python's `max(..., key=)` keeps the FIRST maximum; open_legs is
            // in ascending index order, so ties go to the lowest index.
            let mut best = open_legs[0];
            for &x in &open_legs {
                if room[x] > room[best] {
                    best = x;
                }
            }
            let take = remaining.min(room[best]);
            alloc[best] += take;
            // (the reference also decrements room[best] here; dead, we break)
            remaining -= take;
            break;
        }
        let mut progressed = false;
        let snapshot = open_legs.clone();
        for i in snapshot {
            let take = share.min(room[i]);
            if take > 0 {
                alloc[i] += take;
                room[i] -= take;
                remaining -= take;
                progressed = true;
            }
            if room[i] == 0 {
                open_legs.retain(|&x| x != i);
            }
        }
        if !progressed {
            break;
        }
    }

    // If every recipient hit its cap we cannot place the whole amount; sell only
    // what can be absorbed. The breaching leg lands above 30% this cycle.
    let placed = sell_value - remaining;

    if prices[j] == 0 {
        return Err(PfError::ZeroPrice);
    }
    let sell_tokens = muldiv(placed, TOK, prices[j])?;
    let mut buy_tokens = [0u128; NLEGS];
    for &i in &others {
        if prices[i] == 0 {
            return Err(PfError::ZeroPrice);
        }
        buy_tokens[i] = muldiv(alloc[i], TOK, prices[i])?;
    }

    Ok(Some(Plan {
        leg: j,
        weights_bp: w,
        total_value: total,
        sell_value_wanted: sell_value,
        sell_value_placed: placed,
        capped: remaining > 0,
        alloc_value: alloc,
        sell_tokens,
        buy_tokens,
    }))
}

pub fn apply_plan(holdings: &[u128; NLEGS], plan: &Plan) -> Res<[u128; NLEGS]> {
    let mut h = *holdings;
    if plan.sell_tokens > h[plan.leg] {
        // Cannot happen: sell_tokens = placed*TOK/p <= (h*p/TOK)*TOK/p <= h.
        return Err(PfError::Overflow);
    }
    h[plan.leg] -= plan.sell_tokens;
    for i in 0..NLEGS {
        if i != plan.leg {
            h[i] = add(h[i], plan.buy_tokens[i])?;
        }
    }
    Ok(h)
}

/// `buy_ratio_fair[i]`: tokens of leg i per ONE whole token of the sold leg.
/// Index `plan.leg` is 0, matching how the reference's dict is flattened.
pub fn fair_ratio(plan: &Plan) -> Res<[u128; NLEGS]> {
    if plan.sell_tokens == 0 {
        return Err(PfError::NothingToSell);
    }
    let mut out = [0u128; NLEGS];
    for i in 0..NLEGS {
        if i != plan.leg {
            out[i] = muldiv(plan.buy_tokens[i], SELL_UNIT, plan.sell_tokens)?;
        }
    }
    Ok(out)
}

// --------------------------------------------------------- auction pricing

/// Linear from +25bp to -150bp over `AUCTION_DECAY`, then flat.
///
/// The travelled distance is FLOORED, so the margin stays fractionally higher —
/// the fund's favour.
pub fn margin_bp(elapsed: i64) -> i128 {
    if elapsed <= 0 {
        return AUCTION_START_BP;
    }
    if elapsed >= AUCTION_DECAY {
        return AUCTION_FLOOR_BP;
    }
    let span = AUCTION_START_BP - AUCTION_FLOOR_BP; // 175
    // Both operands positive here, so Python's floor and Rust's truncation agree.
    AUCTION_START_BP - (span * elapsed as i128) / (AUCTION_DECAY as i128)
}

/// What the counterparty must deliver per SELL_UNIT, at this point in the decay.
///
/// Rounded UP: this is what ENTERS the vault, so rounding must favour the fund.
/// Every other rounding in the protocol floors, because it governs what leaves.
pub fn offered_ratio(buy_ratio_fair: &[u128; NLEGS], elapsed: i64) -> Res<[u128; NLEGS]> {
    let m = BP as i128 + margin_bp(elapsed);
    if m < 0 {
        // Unreachable with the fixed constants (min m = 9850), but a negative
        // multiplier would flip the rounding direction, so refuse loudly.
        return Err(PfError::Overflow);
    }
    let m = m as u128;
    let mut out = [0u128; NLEGS];
    for i in 0..NLEGS {
        out[i] = ceil_div_u128(mul(buy_ratio_fair[i], m)?, BP)?;
    }
    Ok(out)
}

/// Tokens of each leg the filler must deliver for `sell_amount` of the sold leg.
pub fn fill_required(
    buy_ratio_fair: &[u128; NLEGS],
    sell_amount: u128,
    elapsed: i64,
) -> Res<[u128; NLEGS]> {
    let ratio = offered_ratio(buy_ratio_fair, elapsed)?;
    let mut out = [0u128; NLEGS];
    for i in 0..NLEGS {
        out[i] = ceil_div_u128(mul(ratio[i], sell_amount)?, SELL_UNIT)?;
    }
    Ok(out)
}

// ------------------------------------------------------- Pyth normalisation

/// Convert a Pyth fixed-point price to USDC base units.
///
/// Pyth reports `price` with signed `expo`; the real value is `price*10**expo`.
/// We want it as an integer count of USDC base units, i.e. `price*10**(expo+6)`.
///
/// `expo` is READ FROM THE FEED at every use, never hard-coded.
///
/// Signed throughout, because `price - conf` in `conservative_prices` can go
/// negative and Python would happily floor-divide it. `py_floor_div_i128` /
/// `py_ceil_div_i128` reproduce Python's rounding-toward-negative-infinity.
pub fn normalise(price: i128, expo: i32, round_up: bool) -> Res<i128> {
    let shift = expo - USDC_EXPO;
    if shift >= 0 {
        if shift > 38 {
            return Err(PfError::Overflow);
        }
        let f = 10i128.checked_pow(shift as u32).ok_or(PfError::Overflow)?;
        price.checked_mul(f).ok_or(PfError::Overflow)
    } else {
        let neg = -shift;
        if neg > 38 {
            return Err(PfError::Overflow);
        }
        let d = 10i128.checked_pow(neg as u32).ok_or(PfError::Overflow)?;
        Ok(if round_up {
            py_ceil_div_i128(price, d)
        } else {
            py_floor_div_i128(price, d)
        })
    }
}

/// `feeds[i] = (price, conf, expo)`. Price the candidate leg LOW and every
/// other leg HIGH, so uncertainty makes the contract less willing to act.
pub fn conservative_prices(feeds: &[(i128, i128, i32)], candidate_leg: usize) -> Res<Vec<i128>> {
    let mut out = Vec::with_capacity(feeds.len());
    for (i, &(price, conf, expo)) in feeds.iter().enumerate() {
        if i == candidate_leg {
            let p = price.checked_sub(conf).ok_or(PfError::Overflow)?;
            out.push(normalise(p, expo, false)?);
        } else {
            let p = price.checked_add(conf).ok_or(PfError::Overflow)?;
            out.push(normalise(p, expo, true)?);
        }
    }
    Ok(out)
}
