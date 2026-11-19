#!/usr/bin/env python3
"""
Permafrost — executable reference implementation.

This is the arithmetic the Anchor program must reproduce exactly. It is
deliberately written in integers only, with the same rounding and the same
fixed-point scale the on-chain program will use, so the vectors it emits are
not illustrations — they are assertions the Rust must satisfy.

Nothing here touches a network or a chain. It is pure arithmetic.

Conventions
-----------
  unit mint      9 decimals   UNIT = 1_000_000_000
  xStock mints   8 decimals   TOK  = 100_000_000
  USDC           6 decimals
  k fixed point  K = 10**18
  all division   floor, always, so remainders stay in the fund
  intermediates  Python ints (unbounded); Rust must use u128
"""

from dataclasses import dataclass, field

UNIT = 10**9          # base units per whole unit token
TOK = 10**8           # base units per whole xStock token
K = 10**18            # fixed-point scale for k
BP = 10_000           # basis points
YEAR = 365 * 24 * 60 * 60

LEGS = ["NVDAx", "GOOGLx", "MSFTx", "METAx"]

# Fixed at deployment, unchangeable
MAX_ENTRY_BP = 100
MAX_EXIT_BP = 100
MAX_STREAM_BP = 100
FEE_TIMELOCK = 30 * 24 * 60 * 60     # 30 days on any increase
TRIGGER_BP = 5000                     # a leg above 50% of basket value
TARGET_BP = 3000                      # bring it down to 30%
RECIPIENT_CAP_BP = 4000               # no recipient may end above 40%
MIN_REBALANCE_GAP = 180 * 24 * 60 * 60
CANDIDATE_MIN_AGE = 60 * 60           # 1 hour
CANDIDATE_MAX_AGE = 24 * 60 * 60      # 24 hours

# --- discount mode ---------------------------------------------------------
# The creator may subsidise deposits out of a pool of units they fund themselves.
# Two paths, both opt-in, sharing one pool.
#
# LIQUID: bonus units are handed over immediately. Capped hard and low, because
# an instantly-claimable, instantly-reversible bonus is a faucet: at any rate
# above the ~26bp round-trip execution cost, a bot deposits, redeems, and drains
# the pool in one transaction for no risk. At or below the cap the arbitrage is
# negative and nobody bothers.
#
# LOCKED: bonus up to 100%, but the depositor's OWN units escrow alongside it
# for LOCK_PERIOD. Early exit returns their principal in full and sends the
# bonus back to the pool, so the "you can always get your own money out" promise
# survives. Because the depositor never holds the escrowed units, they cannot
# defeat the lock by moving them to a fresh wallet — which is what breaks every
# simpler version of this.
MAX_DISCOUNT_BP = 100                  # liquid path, immutable ceiling: 1%
MAX_LOCKED_DISCOUNT_BP = 10_000        # locked path, immutable ceiling: 100%
LOCK_PERIOD = 120 * 24 * 60 * 60       # 4 months


@dataclass
class Position:
    """One locked deposit. Escrow holds principal + bonus until unlock_at."""
    owner: int                          # opaque id; a pubkey on chain
    principal: int                      # the depositor's own units
    bonus: int                          # subsidised units, forfeit on early exit
    unlock_at: int                      # unix seconds


@dataclass
class Vault:
    holdings: list                      # base units of each leg, len 4
    supply: int = 0                     # base units of the unit mint
    entry_bp: int = 0
    exit_bp: int = 0
    stream_bp: int = 0
    last_accrual: int = 0               # unix seconds
    creator_units: int = 0              # units accrued to the creator
    dead_units: int = 0                 # seed units, unspendable but counted
    # discount mode
    subsidy_units: int = 0              # pool the creator funds; NOT vault assets
    discount_on: bool = False           # master toggle, creator-controlled
    discount_bp: int = 0                # liquid path rate, <= MAX_DISCOUNT_BP
    locked_discount_bp: int = 0         # locked path rate, <= MAX_LOCKED_DISCOUNT_BP
    max_bonus_units: int = 0            # per-deposit ceiling; 0 disables bonuses
    escrow_units: int = 0               # total held across all open positions
    positions: dict = field(default_factory=dict)
    next_position_id: int = 1
    log: list = field(default_factory=list)


# ---------------------------------------------------------------- seeding

def seed(amounts, initial_units, now):
    """The one and only deposit split equally by dollars.

    CRITICAL: the seed units are sent to an unspendable address, NOT destroyed.
    Supply must remain non-zero forever. See spec section 4.
    """
    assert all(a > 0 for a in amounts), "every leg must be seeded"
    assert initial_units > 0
    v = Vault(holdings=list(amounts), supply=initial_units, last_accrual=now)
    v.dead_units = initial_units
    return v


# ---------------------------------------------------------------- deposit

def deposit(v, delivered):
    """Mint on the smallest delivered ratio; refund the rest.

    Returns (units_to_depositor, units_to_creator, refunds[4]).
    """
    assert v.supply > 0, "vault must be seeded"
    assert all(h > 0 for h in v.holdings), "no leg may be empty"

    ks = [(d * K) // h for d, h in zip(delivered, v.holdings)]
    k = min(ks)

    # Only the proportional part is kept; the excess on every other leg goes back.
    required = [(h * k) // K for h in v.holdings]
    refunds = [d - r for d, r in zip(delivered, required)]
    assert all(r >= 0 for r in refunds)

    # Mint against what is ACTUALLY retained, not against k.
    #
    # Deriving gross from k alone is wrong: flooring `required` can drive every
    # leg to zero while k stays positive, so a dust deposit that is entirely
    # refunded would still mint units out of nothing. Recomputing the ratio from
    # the retained amounts closes that, and the second floor can only ever round
    # in the fund's favour.
    gross = min((v.supply * r) // h for r, h in zip(required, v.holdings))
    if gross == 0:
        raise ValueError("deposit too small to mint a unit; nothing retained")

    to_creator = (gross * v.entry_bp) // BP
    to_depositor = gross - to_creator

    v.holdings = [h + r for h, r in zip(v.holdings, required)]
    v.supply += gross
    v.creator_units += to_creator
    return to_depositor, to_creator, refunds


# ----------------------------------------------------------------- redeem

def redeem(v, units_in):
    """Burn units, pay out the pro-rata share of every leg.

    The exit fee is taken by transferring a slice of the units to the creator
    instead of burning them, so the payout is computed on what is actually burnt.
    Returns (payouts[4], units_burned, units_to_creator).
    """
    assert 0 < units_in <= v.supply
    to_creator = (units_in * v.exit_bp) // BP
    burned = units_in - to_creator

    payouts = [(h * burned) // v.supply for h in v.holdings]

    v.holdings = [h - p for h, p in zip(v.holdings, payouts)]
    v.supply -= burned
    v.creator_units += to_creator
    return payouts, burned, to_creator


# ------------------------------------------------------------ streaming fee

def accrue(v, now):
    """Mint the creator's slice for elapsed time. Permissionless crank.

    To make the creator's new units equal fraction f of the resulting supply:
        m / (S + m) = f   =>   m = S*f / (1 - f)
    with f = stream_bp * elapsed / (BP * YEAR), computed in integers.
    """
    elapsed = now - v.last_accrual
    if elapsed <= 0 or v.stream_bp == 0:
        v.last_accrual = max(v.last_accrual, now)
        return 0

    num = v.stream_bp * elapsed
    den = BP * YEAR
    assert num < den, "accrual window too long; crank more often"

    minted = (v.supply * num) // (den - num)
    v.supply += minted
    v.creator_units += minted
    v.last_accrual = now
    return minted


# -------------------------------------------------------------- rebalancing

def weights_bp(holdings, prices):
    """Basis-point weight of each leg by value. prices are in USDC base units
    per whole token, so value is holdings * price // TOK."""
    vals = [(h * p) // TOK for h, p in zip(holdings, prices)]
    total = sum(vals)
    return vals, total, [(val * BP) // total for val in vals]


def rebalance_plan(holdings, prices):
    """Decide the swap. Returns None if no leg breaches the trigger.

    Sell enough of the breaching leg to bring it to 30% of basket value, and
    allocate the proceeds in EQUAL VALUE THIRDS to the other three — capped so
    that no recipient ends above 40%, with any excess redistributed to the
    recipients that still have room.
    """
    vals, total, w = weights_bp(holdings, prices)
    j = max(range(4), key=lambda i: w[i])
    if w[j] <= TRIGGER_BP:
        return None

    sell_value = vals[j] - (total * TARGET_BP) // BP
    cap_value = (total * RECIPIENT_CAP_BP) // BP
    others = [i for i in range(4) if i != j]

    # Equal thirds, then iteratively cap and redistribute the overflow.
    alloc = {i: 0 for i in others}
    room = {i: max(0, cap_value - vals[i]) for i in others}
    remaining = sell_value
    open_legs = [i for i in others if room[i] > 0]

    while remaining > 0 and open_legs:
        share = remaining // len(open_legs)
        if share == 0:
            # Dust: hand the remainder to the leg with the most room.
            i = max(open_legs, key=lambda x: room[x])
            take = min(remaining, room[i])
            alloc[i] += take
            room[i] -= take
            remaining -= take
            break
        progressed = False
        for i in list(open_legs):
            take = min(share, room[i])
            if take > 0:
                alloc[i] += take
                room[i] -= take
                remaining -= take
                progressed = True
            if room[i] == 0:
                open_legs.remove(i)
        if not progressed:
            break

    # If every recipient hit its cap, we cannot place the whole amount; sell
    # only what can be absorbed. The breaching leg lands above 30% this cycle.
    placed = sell_value - remaining

    return {
        "leg": j,
        "weights_bp": w,
        "total_value": total,
        "sell_value_wanted": sell_value,
        "sell_value_placed": placed,
        "capped": remaining > 0,
        "alloc_value": {i: alloc[i] for i in others},
        # Fixed token ratio, computed ONCE here. fill() never reads a price.
        "sell_tokens": (placed * TOK) // prices[j],
        "buy_tokens": {i: (alloc[i] * TOK) // prices[i] for i in others},
    }


def apply_plan(holdings, plan):
    h = list(holdings)
    h[plan["leg"]] -= plan["sell_tokens"]
    for i, t in plan["buy_tokens"].items():
        h[i] += t
    return h


# ----------------------------------------------------------- discount mode

def _bonus_for(v, user_units, rate_bp):
    """Units the pool will contribute for a deposit that minted `user_units`.

    Three ceilings, all binding: the rate, the per-deposit cap the creator sets,
    and what is actually left in the pool. Taking the minimum rather than failing
    means a nearly-empty pool degrades to a smaller bonus instead of bricking
    deposits for everyone who arrives after it runs dry.
    """
    if not v.discount_on or rate_bp == 0 or v.max_bonus_units == 0:
        return 0
    return min((user_units * rate_bp) // BP, v.max_bonus_units, v.subsidy_units)


def deposit_discounted(v, delivered):
    """Liquid path. Bonus units handed over immediately, capped at 1%.

    The bonus is a TRANSFER out of the pool, never a mint: supply is unchanged,
    so no existing holder is diluted by any of this.

    No entry fee is charged on the bonus — the fee was already taken on what the
    depositor's own delivery minted, and the creator does not charge themselves.
    """
    assert v.discount_bp <= MAX_DISCOUNT_BP
    to_depositor, to_creator, refunds = deposit(v, delivered)
    bonus = _bonus_for(v, to_depositor, v.discount_bp)
    v.subsidy_units -= bonus
    return to_depositor + bonus, to_creator, refunds, bonus


def deposit_locked(v, delivered, now):
    """Locked path. Principal AND bonus escrow together for LOCK_PERIOD.

    The depositor receives nothing spendable now. That is the point: a bonus the
    holder can redeem in the same block is a giveaway, and a bonus locked while
    the principal stays liquid is a free option. Locking both means the money
    that earns the bonus is money genuinely at risk in the fund.

    Returns (position_id, principal, bonus).
    """
    assert v.locked_discount_bp <= MAX_LOCKED_DISCOUNT_BP
    to_depositor, to_creator, refunds = deposit(v, delivered)
    bonus = _bonus_for(v, to_depositor, v.locked_discount_bp)
    v.subsidy_units -= bonus

    pid = v.next_position_id
    v.next_position_id += 1
    v.positions[pid] = Position(
        owner=pid, principal=to_depositor, bonus=bonus, unlock_at=now + LOCK_PERIOD
    )
    v.escrow_units += to_depositor + bonus
    return pid, to_depositor, bonus, to_creator, refunds


def claim_locked(v, pid, now):
    """At or after maturity: the depositor takes principal + bonus."""
    p = v.positions[pid]
    assert now >= p.unlock_at, "position has not matured"
    out = p.principal + p.bonus
    v.escrow_units -= out
    del v.positions[pid]
    return out


def exit_locked_early(v, pid, now):
    """Before maturity: principal back IN FULL, bonus returns to the pool.

    Note what this deliberately does not do: it does not touch the principal, it
    does not apply a penalty, and it does not consult any of the dials. Whatever
    the creator has changed in the meantime, the depositor's own units come back
    unchanged. Redemption is then the ordinary unconditional path.
    """
    p = v.positions[pid]
    assert now < p.unlock_at, "position has matured; use claim_locked"
    v.escrow_units -= p.principal + p.bonus
    v.subsidy_units += p.bonus
    del v.positions[pid]
    return p.principal


def fund_subsidy(v, units):
    """Creator moves their own units into the pool. Not a vault asset.

    The pool and the escrow are both holdings of the unit mint, so together they
    can never exceed what has been minted. On chain the token transfer would
    simply fail; the reference has to encode that or it will happily model a
    pool funded out of nothing.
    """
    assert units > 0
    assert v.subsidy_units + v.escrow_units + units <= v.supply, \
        "cannot fund the pool with units that do not exist"
    v.subsidy_units += units


def withdraw_subsidy(v, units):
    """Creator takes units back out. Cannot reach escrowed or vault holdings."""
    assert 0 < units <= v.subsidy_units
    v.subsidy_units -= units


def set_discount(v, discount_bp, locked_discount_bp, max_bonus_units):
    """Creator-controlled, no timelock.

    Unlike the fee switch, none of these can take anything from a user — the
    worst a hostile creator can do is stop giving their own money away. So there
    is nothing for a timelock to protect against.
    """
    assert 0 <= discount_bp <= MAX_DISCOUNT_BP
    assert 0 <= locked_discount_bp <= MAX_LOCKED_DISCOUNT_BP
    assert max_bonus_units >= 0
    v.discount_bp = discount_bp
    v.locked_discount_bp = locked_discount_bp
    v.max_bonus_units = max_bonus_units


# ------------------------------------------------------------------ checks

def invariants(v):
    """Must hold after every instruction."""
    assert v.supply > 0, "supply may never reach zero"
    assert all(h >= 0 for h in v.holdings)
    assert v.supply >= v.dead_units
    # discount mode
    assert v.subsidy_units >= 0, "the pool may never be overdrawn"
    assert v.escrow_units == sum(p.principal + p.bonus for p in v.positions.values())
    assert v.subsidy_units + v.escrow_units <= v.supply


def unit_value(v, prices):
    """USDC base units per whole unit token — for tests, never on-chain."""
    total = sum((h * p) // TOK for h, p in zip(v.holdings, prices))
    return (total * UNIT) / v.supply


# --------------------------------------------------------- auction pricing

SELL_UNIT = TOK               # ratios are quoted per ONE whole token of the selling leg
AUCTION_START_BP = 25         # opens asking 0.25% MORE than fair — in the fund's favour
AUCTION_FLOOR_BP = -150       # never gives away more than 1.5%
AUCTION_DECAY = 6 * 60 * 60   # linear, 6 hours
AUCTION_WINDOW = 72 * 60 * 60 # stays open 3 days, flat at the floor after decay
MIN_FILL_BP = 100             # a fill must be >= 1% of the original parcel
RETRY_COOLDOWN = 7 * 24 * 60 * 60   # after a failed or partial auction


def ceil_div(a, b):
    return -(-a // b)


def margin_bp(elapsed):
    """Linear from +25bp to -150bp over AUCTION_DECAY, then flat."""
    if elapsed <= 0:
        return AUCTION_START_BP
    if elapsed >= AUCTION_DECAY:
        return AUCTION_FLOOR_BP
    span = AUCTION_START_BP - AUCTION_FLOOR_BP
    # floor the travelled distance -> margin stays marginally higher -> fund's favour
    return AUCTION_START_BP - (span * elapsed) // AUCTION_DECAY


def offered_ratio(buy_ratio_fair, elapsed):
    """What the counterparty must deliver per SELL_UNIT, at this point in the decay.

    Rounded UP: this is what ENTERS the vault, so rounding must favour the fund.
    Every other rounding in the protocol floors because it governs what leaves.
    """
    m = BP + margin_bp(elapsed)
    return [ceil_div(r * m, BP) for r in buy_ratio_fair]


def fill_required(buy_ratio_fair, sell_amount, elapsed):
    """Tokens of each leg the filler must deliver for `sell_amount` of the sold leg."""
    ratio = offered_ratio(buy_ratio_fair, elapsed)
    return [ceil_div(r * sell_amount, SELL_UNIT) for r in ratio]


def fair_ratio(plan, prices):
    """buy_ratio_fair[i]: tokens of leg i per ONE whole token of the sold leg."""
    sell_tokens = plan["sell_tokens"]
    assert sell_tokens > 0
    out = {}
    for i, tokens in plan["buy_tokens"].items():
        out[i] = (tokens * SELL_UNIT) // sell_tokens
    return out


# ------------------------------------------------------- Pyth normalisation

USDC_EXPO = -6        # we normalise every feed to USDC base units

# Confirmed from the Hermes metadata API, 4 Sep 2026. Regular-session feeds:
# schedule America/New_York;0930-1600 x5;C,C  -> closed at weekends.
PYTH_FEEDS = {
    "NVDAx":  "b1073854ed24cbc755dc527418f52b7d271f6cc967bbf8d8129112b18860a593",
    "GOOGLx": "5a48c03e9b9cb337801073ed9d166817473697efff0d138874e0f6a33d6d5aa6",
    "MSFTx":  "d0ca23c1cc005e004ccf1db5bf76aeb6a49218f43dac3d4b275e92de12ded4d1",
    "METAx":  "78a3e3b8e676a8f73c439f5d749737034b139bbbe899ba5775216fba596607fe",
}


def normalise(price, expo, round_up):
    """Convert a Pyth fixed-point price to USDC base units.

    Pyth reports `price` with signed `expo`, meaning the real value is
    price * 10**expo. We want it as an integer count of USDC base units,
    i.e. price * 10**(expo + 6).

    `expo` is READ FROM THE FEED at every use, never hard-coded. Pyth can and
    does change it, and a contract that assumes a constant would silently
    misprice by orders of magnitude the day it moved.

    round_up selects the direction; the caller picks whichever is conservative
    for the leg in question (§8.4).
    """
    shift = expo - USDC_EXPO
    if shift >= 0:
        return price * 10**shift
    d = 10**(-shift)
    return ceil_div(price, d) if round_up else price // d


def conservative_prices(feeds, candidate_leg):
    """feeds[i] = (price, conf, expo). Price the candidate leg LOW and every
    other leg HIGH, so uncertainty makes the contract less willing to act."""
    out = []
    for i, (price, conf, expo) in enumerate(feeds):
        if i == candidate_leg:
            out.append(normalise(price - conf, expo, round_up=False))
        else:
            out.append(normalise(price + conf, expo, round_up=True))
    return out
