"""Permafrost v4 — orders: invest with a stablecoin / withdraw to a stablecoin, one
signature. Run: PF_MOCK=1 python3 -I tests/test_v4.py   (the mock-swap build)

The mock swap program stands in for Jupiter and checks NOTHING: the tests drive
it with honest and with hostile calls to show fill_leg's guards hold regardless of
what the swap program does. Plan: claude/permafrost-v4-usdc-orders-plan.md.

  O — orders, happy paths (invest, redeem, both stablecoins, cancel refunds)
  R — refusals: every guard, including the attacks a hostile filler could try
  X — extraction: random orders, deposits, redeems and cancels never lower the
      vault's holdings per unit
  U — upgrade: the vault account is untouched by v4; v1/v3 flows still run
  M — sizes
"""

from __future__ import annotations

import random
import struct
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_v1 as V1  # noqa: E402
from harness import *  # noqa: E402,F401,F403
from solders.instruction import AccountMeta  # noqa: E402
from solders.transaction import Transaction  # noqa: E402
from solders.message import Message  # noqa: E402

assert MOCK, "run with PF_MOCK=1 (the mockswap build: cargo-build-sbf --features mockswap)"

TESTS = []
NOTES = {}
must_fail, units_of, legs_of = V1.must_fail, V1.units_of, V1.legs_of
PRICES = (214.39, 339.65, 530.01, 709.76)   # USD per stock, the harness's seed prices
GAS = det_keypair("gas-service")             # pays rent, fills, settles


def test(fn):
    TESTS.append(fn)
    return fn


def world():
    w = World().launch()
    w.svm.airdrop(GAS.pubkey(), 100 * 10**9)
    w.mint_stable(GAS.pubkey(), 1_000_000)             # the settler's fee account
    w.mint_stable(GAS.pubkey(), 1_000_000, USDT)
    w.fund_mock_pool([10**12] * 4, 10**12)             # deep liquidity for the mock
    w.fund_mock_pool_usdt = lambda: w.mint_stable(MOCK_POOL, 10**12, USDT)
    return w


def stable_user(w, label, usdc=1_000_000_000, usdt=0):
    u = w.new_user(label)
    w.mint_stable(u.pubkey(), usdc)
    if usdt:
        w.mint_stable(u.pubkey(), usdt, USDT)
    return u


def plan(amount, fee=0, slip=0.01):
    """A fair plan for `amount` of stablecoin: equal dollars per leg, 1% slippage floor."""
    per = (amount - fee) // 4
    budget = [per] * 4
    stock = [int(per / 10**6 / p * 10**8) for p in PRICES]
    return budget, [int(s * (1 - slip)) for s in stock], stock


def fair_out(leg, amount_in):
    """What the mock delivers at the fair price for `amount_in` of stablecoin."""
    return int(amount_in / 10**6 / PRICES[leg] * 10**8)


def fair_in(leg, stock):
    return int(stock / 10**8 * PRICES[leg] * 10**6)


def open_invest(w, u, oid, amount, fee=0, min_units=0, **kw):
    budget, min_out, stock = plan(amount, fee)
    r = w.send([w.ix_order_invest(u, GAS, oid, amount, budget, min_out, min_units, fee, **kw)], [u, GAS])
    return r, budget, min_out, stock


def fill_all(w, u, oid, stock=None, stable=USDC):
    o = w.order(u.pubkey(), oid)
    for leg in range(4):
        if o["kind"] == KIND_INVEST:
            amt_in = o["budget"][leg]
            amt_out = fair_out(leg, amt_in) if stock is None else stock[leg]
        else:
            amt_in = o["budget"][leg]
            amt_out = fair_in(leg, amt_in)
        r = w.send([w.ix_fill_leg(GAS, u.pubkey(), oid, leg, amt_in, amt_out, stable=stable)], [GAS])
        assert r.ok, f"fill leg {leg}: {r.reason()} {r.logs[-4:]}"
    return w.order(u.pubkey(), oid)


def rent_of(w, owner, oid, stable=USDC):
    oa = order_accounts(owner, oid, stable)
    return sum((w.svm.get_account(k).lamports if w.svm.get_account(k) else 0) for k in [oa["order"], oa["stable"], *oa["legs"]])


def closed(w, owner, oid, stable=USDC):
    oa = order_accounts(owner, oid, stable)
    return all(w.account_data(k) is None for k in [oa["order"], oa["stable"], *oa["legs"]])


# =============================================================== O: happy paths

@test
def O1_invest_order_mints_exactly_what_a_direct_deposit_would():
    w = world()
    u = stable_user(w, "alice")
    amount, fee = 100_000_000, 150_000   # $100, 15c fee cap
    r, budget, min_out, stock = open_invest(w, u, 1, amount, fee, min_units=1)
    assert r.ok, r.reason()
    o = w.order(u.pubkey(), 1)
    assert o["kind"] == KIND_INVEST and o["amount_in"] == amount and o["filled"] == 0 and o["payer"] == GAS.pubkey()
    assert w.stable_balance(u.pubkey()) == 1_000_000_000 - amount, "the stablecoin is escrowed"
    oa = order_accounts(u.pubkey(), 1)
    assert w.balance(oa["stable"]) == amount
    rent = rent_of(w, u.pubkey(), 1)
    gas_before = w.svm.get_account(GAS.pubkey()).lamports

    o = fill_all(w, u, 1)
    assert o["filled"] == 0b1111
    bought = [w.balance(l) for l in oa["legs"]]
    assert all(b >= m for b, m in zip(bought, min_out))

    # Reference: the same stock deposited directly by a different user in a fresh
    # world with the same vault state mints the same units.
    w2 = world()
    ref = V1.funded_user(w2, "ref", bought)
    r2 = w2.send([w2.ix_deposit(ref, bought, 1)], [ref])
    assert r2.ok, r2.reason()
    ref_units = units_of(w2, ref)

    h0, s0 = w.holdings(), w.supply()
    user_usdc0 = w.stable_balance(u.pubkey())
    gas_usdc0 = w.stable_balance(GAS.pubkey())
    r = w.send([w.ix_settle_invest(GAS, u.pubkey(), 1, fee=fee)], [GAS])
    assert r.ok, r.reason() + " " + " ".join(r.logs[-6:])
    NOTES["cu_settle_invest"] = r.raw.compute_units_consumed()
    got = units_of(w, u)
    assert got == ref_units, f"order minted {got} units, a direct deposit of the same stock mints {ref_units}"
    assert got >= 1
    # Every bought token went into the vault (the leftover above the binding leg too).
    assert [a - b for a, b in zip(w.holdings(), h0)] == bought
    assert w.supply() == s0 + got
    assert closed(w, u.pubkey(), 1), "escrow accounts closed"
    gas_after = w.svm.get_account(GAS.pubkey()).lamports
    # The payer gets the escrow rent back; it also paid 5 tx fees and the owner's new PAIF account.
    assert gas_before + rent - 5 * 5_000 - 2_200_000 <= gas_after <= gas_before + rent, (gas_before, gas_after, rent)
    assert w.stable_balance(GAS.pubkey()) == gas_usdc0 + fee, "the settler kept exactly its fee"
    change = amount - sum(budget) - fee
    assert w.stable_balance(u.pubkey()) == user_usdc0 + change, "the owner got the change"
    assert legs_of(w, u) == [0, 0, 0, 0], "no stock tokens in the owner's wallet"
    return f"${amount/1e6:.0f} in → {got/1e9:.6f} units (= direct deposit), fee {fee/1e6:.2f}, change {change/1e6:.2f}, rent {rent/1e9:.4f} SOL back"


@test
def O2_redeem_order_pays_what_a_direct_redeem_would_minus_the_fee():
    w = world()
    u = stable_user(w, "bob")
    r, *_ = open_invest(w, u, 1, 200_000_000)
    assert r.ok
    fill_all(w, u, 1)
    assert w.send([w.ix_settle_invest(GAS, u.pubkey(), 1)], [GAS]).ok
    units = units_of(w, u)
    half = units // 2
    # Reference payouts: a direct redeem of the same units in a copy of the state.
    ref_w = world()
    ref_u = stable_user(ref_w, "bob")
    open_invest(ref_w, ref_u, 1, 200_000_000); fill_all(ref_w, ref_u, 1)
    assert ref_w.send([ref_w.ix_settle_invest(GAS, ref_u.pubkey(), 1)], [GAS]).ok
    ref_w.mint_legs(ref_u, [0, 0, 0, 0])  # user leg ATAs for a direct redeem
    l0 = legs_of(ref_w, ref_u)
    assert ref_w.send([ref_w.ix_redeem(ref_u, half)], [ref_u]).ok
    ref_paid = [b - a for a, b in zip(l0, legs_of(ref_w, ref_u))]

    fee = 100_000
    min_out = [int(fair_in(i, p) * 0.99) for i, p in enumerate(ref_paid)]
    r = w.send([w.ix_order_redeem(u, GAS, 2, half, min_out, fee)], [u, GAS])
    assert r.ok, r.reason() + " " + " ".join(r.logs[-5:])
    NOTES["cu_order_redeem"] = r.raw.compute_units_consumed()
    o = w.order(u.pubkey(), 2)
    assert o["kind"] == KIND_REDEEM and o["budget"] == ref_paid, f"escrowed {o['budget']} vs direct redeem {ref_paid}"
    assert units_of(w, u) == units - half, "units burnt at order time"
    oa = order_accounts(u.pubkey(), 2)
    assert [w.balance(l) for l in oa["legs"]] == ref_paid
    fill_all(w, u, 2)
    usdc0, gas0 = w.stable_balance(u.pubkey()), w.stable_balance(GAS.pubkey())
    h0 = w.holdings()
    r = w.send([w.ix_settle_redeem(GAS, u.pubkey(), 2, fee=fee)], [GAS])
    assert r.ok, r.reason() + " " + " ".join(r.logs[-5:])
    NOTES["cu_settle_redeem"] = r.raw.compute_units_consumed()
    proceeds = sum(fair_in(i, p) for i, p in enumerate(ref_paid))
    assert w.stable_balance(u.pubkey()) == usdc0 + proceeds - fee
    assert w.stable_balance(GAS.pubkey()) == gas0 + fee
    assert w.holdings() == h0, "all stock sold: no dust reached the vault"
    assert closed(w, u.pubkey(), 2)
    assert legs_of(w, u) == [0, 0, 0, 0]
    return f"{half/1e9:.4f} units → ${proceeds/1e6:.2f} − {fee/1e6:.2f} fee; escrowed legs equal a direct redeem's payouts"


@test
def O3_usdt_orders_work_the_same():
    w = world()
    w.fund_mock_pool_usdt()
    u = stable_user(w, "carol", usdc=0, usdt=500_000_000)
    budget, min_out, _ = plan(50_000_000)
    r = w.send([w.ix_order_invest(u, GAS, 7, 50_000_000, budget, min_out, 1, 0, stable=USDT)], [u, GAS])
    assert r.ok, r.reason()
    fill_all(w, u, 7, stable=USDT)
    r = w.send([w.ix_settle_invest(GAS, u.pubkey(), 7, stable=USDT)], [GAS])
    assert r.ok, r.reason() + " ".join(r.logs[-5:])
    units = units_of(w, u)
    r = w.send([w.ix_order_redeem(u, GAS, 8, units, (0, 0, 0, 0), 0, stable=USDT)], [u, GAS])
    assert r.ok, r.reason()
    fill_all(w, u, 8, stable=USDT)
    assert w.send([w.ix_settle_redeem(GAS, u.pubkey(), 8, stable=USDT)], [GAS]).ok
    back = w.stable_balance(u.pubkey(), USDT)
    assert 480_000_000 < back <= 500_000_000, back
    return f"USDT in, {units/1e9:.4f} units, USDT out: {back/1e6:.2f} of 500 (rounding only)"


@test
def O4_owner_can_cancel_any_time_for_a_full_refund():
    w = world()
    u = stable_user(w, "dan")
    r, *_ = open_invest(w, u, 3, 80_000_000)
    assert r.ok
    rent = rent_of(w, u.pubkey(), 3)
    g0 = w.svm.get_account(GAS.pubkey()).lamports
    r = w.send([w.ix_cancel_order(u, u.pubkey(), 3)], [u])
    assert r.ok, r.reason() + " ".join(r.logs[-5:])
    assert w.stable_balance(u.pubkey()) == 1_000_000_000, "every cent back"
    assert closed(w, u.pubkey(), 3)
    assert w.svm.get_account(GAS.pubkey()).lamports == g0 + rent, "rent back to the payer, not the canceller"
    return "unfilled order cancelled by its owner: stablecoin and rent refunded in full"


@test
def O5_after_expiry_anyone_can_cancel_and_partial_fills_are_returned_as_stock():
    w = world()
    u = stable_user(w, "eve")
    r, budget, min_out, _ = open_invest(w, u, 4, 80_000_000, expires_at=w.now() + 120)
    assert r.ok
    # Two legs filled, then the filler vanishes.
    for leg in (0, 2):
        assert w.send([w.ix_fill_leg(GAS, u.pubkey(), 4, leg, budget[leg], fair_out(leg, budget[leg]))], [GAS]).ok
    stranger = w.new_user("stranger")
    must_fail(w.send([w.ix_cancel_order(stranger, u.pubkey(), 4)], [stranger]), code=ERR["OrderNotExpired"], why="not yet expired, not the owner")
    w.warp(121)
    must_fail(w.send([w.ix_fill_leg(GAS, u.pubkey(), 4, 1, budget[1], fair_out(1, budget[1]))], [GAS]), code=ERR["OrderExpired"], why="no fills after expiry")
    r = w.send([w.ix_cancel_order(stranger, u.pubkey(), 4)], [stranger])
    assert r.ok, r.reason() + " ".join(r.logs[-5:])
    legs = legs_of(w, u)
    assert legs[0] == fair_out(0, budget[0]) and legs[2] == fair_out(2, budget[2]) and legs[1] == 0 and legs[3] == 0
    assert w.stable_balance(u.pubkey()) == 1_000_000_000 - budget[0] - budget[2]
    assert closed(w, u.pubkey(), 4)
    return "expired, half-filled order cancelled by a stranger: unspent stablecoin and the two bought legs returned to the owner"


@test
def O6_redeem_order_cancelled_returns_the_escrowed_stock():
    w = world()
    u = stable_user(w, "fay")
    open_invest(w, u, 1, 100_000_000); fill_all(w, u, 1)
    assert w.send([w.ix_settle_invest(GAS, u.pubkey(), 1)], [GAS]).ok
    units = units_of(w, u)
    assert w.send([w.ix_order_redeem(u, GAS, 2, units, (0, 0, 0, 0), 0)], [u, GAS]).ok
    o = w.order(u.pubkey(), 2)
    assert w.send([w.ix_fill_leg(GAS, u.pubkey(), 2, 3, o["budget"][3], fair_in(3, o["budget"][3]))], [GAS]).ok
    r = w.send([w.ix_cancel_order(u, u.pubkey(), 2)], [u])
    assert r.ok, r.reason() + " ".join(r.logs[-5:])
    legs = legs_of(w, u)
    assert legs[:3] == o["budget"][:3] and legs[3] == 0
    assert w.stable_balance(u.pubkey()) == 1_000_000_000 - 100_000_000 + fair_in(3, o["budget"][3])
    return "three unsold legs back as stock, the sold leg's stablecoin back too; units stay burnt (the user holds the stock now)"


# ================================================================ R: refusals

@test
def R1_a_fill_below_the_legs_minimum_is_refused():
    w = world()
    u = stable_user(w, "r1")
    _, budget, min_out, _ = open_invest(w, u, 1, 100_000_000)
    r = w.send([w.ix_fill_leg(GAS, u.pubkey(), 1, 0, budget[0], min_out[0] - 1)], [GAS])
    must_fail(r, code=ERR["FillBelowMinimum"], why="one base unit under the floor")
    assert w.send([w.ix_fill_leg(GAS, u.pubkey(), 1, 0, budget[0], min_out[0])], [GAS]).ok
    return "min_out − 1 refused; exactly min_out accepted"


@test
def R2_a_fill_spending_over_the_legs_budget_is_refused():
    w = world()
    u = stable_user(w, "r2")
    _, budget, min_out, _ = open_invest(w, u, 1, 100_000_000)
    r = w.send([w.ix_fill_leg(GAS, u.pubkey(), 1, 1, budget[1] + 1, fair_out(1, budget[1] + 1))], [GAS])
    must_fail(r, code=ERR["BudgetExceeded"], why="a lamport of stablecoin over budget")
    return "budget + 1 refused even though the stock delivered was generous"


@test
def R3_a_route_that_sells_another_escrowed_leg_is_refused():
    """A hostile filler buys leg 0 by SELLING the escrow's leg 2 (already bought)
    instead of its stablecoin — the attack the five-balance snapshot exists for."""
    w = world()
    u = stable_user(w, "r3")
    _, budget, min_out, _ = open_invest(w, u, 1, 100_000_000)
    assert w.send([w.ix_fill_leg(GAS, u.pubkey(), 1, 2, budget[2], fair_out(2, budget[2]))], [GAS]).ok
    oa = order_accounts(u.pubkey(), 1)
    # Swap accounts: src = escrow leg 2, dst = escrow leg 0 — the mock will happily do it.
    metas = w.mock_swap_metas(oa["order"], oa["legs"][2], oa["legs"][0], LEG_MINTS[2], LEG_MINTS[0])
    data = w.mock_swap_data(fair_out(2, budget[2]) // 2, min_out[0])
    r = w.send([w.ix_fill_leg(GAS, u.pubkey(), 1, 0, 0, 0, swap_metas=metas, swap_data=data)], [GAS])
    must_fail(r, code=ERR["OrderDrained"], why="leg 2 fell")
    assert w.balance(oa["legs"][2]) == fair_out(2, budget[2]), "nothing moved"
    return "leg 0 'bought' by draining leg 2: refused, escrow untouched"


@test
def R4_a_route_that_sends_the_output_elsewhere_is_refused():
    w = world()
    u = stable_user(w, "r4")
    thief = w.new_user("thief")
    w.mint_legs(thief, [0, 0, 0, 0])
    _, budget, min_out, _ = open_invest(w, u, 1, 100_000_000)
    oa = order_accounts(u.pubkey(), 1)
    metas = w.mock_swap_metas(oa["order"], oa["stable"], ata(thief.pubkey(), LEG_MINTS[0]), USDC, LEG_MINTS[0])
    r = w.send([w.ix_fill_leg(GAS, u.pubkey(), 1, 0, budget[0], fair_out(0, budget[0]), swap_metas=metas)], [GAS])
    must_fail(r, code=ERR["FillBelowMinimum"], why="escrow's leg 0 did not rise")
    assert w.balance(oa["stable"]) == 100_000_000 and legs_of(w, thief) == [0, 0, 0, 0]
    return "output redirected to a thief: refused, stablecoin still escrowed, thief got nothing"


@test
def R5_the_escrow_signs_for_no_program_but_the_swap_program():
    """A filler passes the TOKEN program as the 'swap program' with a transfer
    instruction: with the escrow as signer that would move its stablecoin anywhere."""
    w = world()
    u = stable_user(w, "r5")
    thief = w.new_user("thief5")
    w.mint_stable(thief.pubkey(), 0)
    _, budget, min_out, _ = open_invest(w, u, 1, 100_000_000)
    oa = order_accounts(u.pubkey(), 1)
    metas = [AccountMeta(oa["stable"], False, True), AccountMeta(USDC, False, False),
             AccountMeta(ata_classic(thief.pubkey(), USDC), False, True), AccountMeta(oa["order"], False, False)]
    data = bytes([12]) + struct.pack("<QB", 100_000_000, 6)  # TransferChecked
    r = w.send([w.ix_fill_leg(GAS, u.pubkey(), 1, 0, 0, 0, swap_metas=metas, swap_data=data, swap_program=TOKEN)], [GAS])
    must_fail(r, code=ERR["NotSwapProgram"], why="not the swap program")
    assert w.balance(oa["stable"]) == 100_000_000 and w.stable_balance(thief.pubkey()) == 0
    # And the program's own id can't be smuggled in as a swap account (re-entrancy).
    metas = w.mock_swap_metas(oa["order"], oa["stable"], oa["legs"][0], USDC, LEG_MINTS[0]) + [AccountMeta(PROGRAM_ID, False, False)]
    r = w.send([w.ix_fill_leg(GAS, u.pubkey(), 1, 0, budget[0], fair_out(0, budget[0]), swap_metas=metas)], [GAS])
    must_fail(r, code=ERR["BadAccount"], why="own program id among the swap accounts")
    return "token program as 'swap': refused; own program among swap accounts: refused"


@test
def R6_a_leg_fills_once_and_settle_needs_all_four():
    w = world()
    u = stable_user(w, "r6")
    _, budget, min_out, _ = open_invest(w, u, 1, 100_000_000)
    assert w.send([w.ix_fill_leg(GAS, u.pubkey(), 1, 0, budget[0], fair_out(0, budget[0]))], [GAS]).ok
    must_fail(w.send([w.ix_fill_leg(GAS, u.pubkey(), 1, 0, budget[0], fair_out(0, budget[0]))], [GAS]), code=ERR["LegAlreadyFilled"], why="twice")
    must_fail(w.send([w.ix_settle_invest(GAS, u.pubkey(), 1)], [GAS]), code=ERR["LegNotFilled"], why="three legs missing")
    must_fail(w.send([w.ix_fill_leg(GAS, u.pubkey(), 1, 4, 1, 1)], [GAS]), code=ERR["BadLeg"], why="leg 4")
    return "second fill of a leg, settle with legs missing, leg 4: all refused"


@test
def R7_settle_below_min_units_fails_and_the_order_refunds():
    w = world()
    u = stable_user(w, "r7")
    r, budget, min_out, stock = open_invest(w, u, 1, 100_000_000, min_units=10**15)  # impossible ask
    assert r.ok
    fill_all(w, u, 1)
    must_fail(w.send([w.ix_settle_invest(GAS, u.pubkey(), 1)], [GAS]), code=ERR["SlippageUnits"], why="below min_units")
    assert units_of(w, u) == 0
    r = w.send([w.ix_cancel_order(u, u.pubkey(), 1)], [u])
    assert r.ok, r.reason()
    assert legs_of(w, u) == [fair_out(i, budget[i]) for i in range(4)], "the bought stock is the user's"
    return "min_units unmet: no units minted, cancel returned all four bought legs"


@test
def R8_fee_is_capped_by_the_order_and_only_in_its_stablecoin():
    w = world()
    u = stable_user(w, "r8")
    open_invest(w, u, 1, 100_000_000, fee=200_000)
    fill_all(w, u, 1)
    must_fail(w.send([w.ix_settle_invest(GAS, u.pubkey(), 1, fee=200_001)], [GAS]), code=ERR["FeeAboveMax"], why="over the cap")
    # fee_to must be an account of the order's stablecoin (USDT account refused).
    r = w.send([w.ix_settle_invest(GAS, u.pubkey(), 1, fee=200_000, fee_to=ata_classic(GAS.pubkey(), USDT))], [GAS])
    must_fail(r, why="fee to a USDT account for a USDC order")
    g0 = w.stable_balance(GAS.pubkey())
    assert w.send([w.ix_settle_invest(GAS, u.pubkey(), 1, fee=200_000)], [GAS]).ok
    assert w.stable_balance(GAS.pubkey()) == g0 + 200_000
    return "fee cap enforced; fee must be paid in the order's own stablecoin"


@test
def R9_bad_plans_are_refused_at_opening():
    w = world()
    u = stable_user(w, "r9")
    budget, min_out, _ = plan(100_000_000)
    must_fail(w.send([w.ix_order_invest(u, GAS, 1, 100_000_000, [b + 1 for b in budget], min_out, 0, 0)], [u, GAS]), code=ERR["BadBudget"], why="budgets over amount")
    must_fail(w.send([w.ix_order_invest(u, GAS, 1, 100_000_000, budget, min_out, 0, 1)], [u, GAS]), code=ERR["BadBudget"], why="budgets + fee over amount")
    must_fail(w.send([w.ix_order_invest(u, GAS, 1, 100_000_000, [budget[0], 0, budget[2], budget[3]], min_out, 0, 0)], [u, GAS]), code=ERR["BadBudget"], why="a zero budget")
    must_fail(w.send([w.ix_order_invest(u, GAS, 1, 100_000_000, budget, min_out, 0, 0, expires_at=w.now() + 30)], [u, GAS]), code=ERR["BadExpiry"], why="30 s life")
    must_fail(w.send([w.ix_order_invest(u, GAS, 1, 100_000_000, budget, min_out, 0, 0, expires_at=w.now() + 7200)], [u, GAS]), code=ERR["BadExpiry"], why="2 h life")
    must_fail(w.send([w.ix_order_invest(u, GAS, 1, 0, budget, min_out, 0, 0)], [u, GAS]), why="zero amount")
    # Not a stablecoin: a fresh classic mint.
    other = det_keypair("other-mint").pubkey()
    w.create_stable_mint(other)
    w.mint_stable(u.pubkey(), 100_000_000, other)
    must_fail(w.send([w.ix_order_invest(u, GAS, 1, 100_000_000, budget, min_out, 0, 0, stable=other)], [u, GAS]), code=ERR["BadStableMint"], why="not USDC/USDT")
    assert w.order(u.pubkey(), 1) is None
    return "over-budget, zero budget, too short, too long, zero amount, foreign mint: all refused"


@test
def R10_escrow_accounts_cannot_be_swapped_for_another_orders():
    w = world()
    u, v = stable_user(w, "r10a"), stable_user(w, "r10b")
    open_invest(w, u, 1, 100_000_000)
    open_invest(w, v, 1, 100_000_000)
    oa_v = order_accounts(v.pubkey(), 1)
    budget = plan(100_000_000)[0]
    # Fill u's order but name v's leg 0 as the escrow's leg 0.
    r = w.send([w.ix_fill_leg(GAS, u.pubkey(), 1, 0, budget[0], fair_out(0, budget[0]), override={8: AccountMeta(oa_v["legs"][0], False, True)})], [GAS])
    must_fail(r, why="another order's leg account")
    # Settle u's order with v's payer slot pointing at the thief.
    fill_all(w, u, 1)
    thief = w.new_user("thief10")
    r = w.send([w.ix_settle_invest(GAS, u.pubkey(), 1, override={3: AccountMeta(thief.pubkey(), False, True)})], [GAS])
    must_fail(r, why="rent to someone other than the payer")
    r = w.send([w.ix_settle_invest(GAS, u.pubkey(), 1, override={2: AccountMeta(thief.pubkey(), False, False)})], [GAS])
    must_fail(r, why="units to someone other than the owner")
    return "foreign escrow account, foreign payer, foreign owner: all refused by the account constraints"


@test
def R11_wrong_kind_and_a_second_order_with_the_same_id():
    w = world()
    u = stable_user(w, "r11")
    open_invest(w, u, 1, 100_000_000); fill_all(w, u, 1)
    must_fail(w.send([w.ix_settle_redeem(GAS, u.pubkey(), 1)], [GAS]), code=ERR["BadOrderKind"], why="settle_redeem on an invest order")
    must_fail(w.send([w.ix_order_invest(u, GAS, 1, 10_000_000, *plan(10_000_000)[:2], 0, 0)], [u, GAS]), why="id in use")
    assert w.send([w.ix_settle_invest(GAS, u.pubkey(), 1)], [GAS]).ok
    # The id is free again once the order is closed.
    assert w.send([w.ix_order_invest(u, GAS, 1, 10_000_000, *plan(10_000_000)[:2], 0, 0)], [u, GAS]).ok
    return "wrong kind refused; duplicate id refused while open, usable after close"


@test
def R12_the_cap_applies_at_settle():
    w = world()
    u = stable_user(w, "r12")
    assert w.send([w.ix_set_cap(ADMIN, SEED_UNITS + 10**6)], [ADMIN]).ok
    open_invest(w, u, 1, 100_000_000); fill_all(w, u, 1)
    must_fail(w.send([w.ix_settle_invest(GAS, u.pubkey(), 1)], [GAS]), code=ERR["CapExceeded"], why="cap")
    assert w.send([w.ix_cancel_order(u, u.pubkey(), 1)], [u]).ok
    return "settle refused at the cap; the order refunds"


# ============================================================== X: extraction

@test
def X1_random_orders_never_lower_the_vaults_holdings_per_unit():
    w = world()
    rng = random.Random(44)
    users = [stable_user(w, f"x{i}", usdc=5_000_000_000) for i in range(4)]
    for u in users:
        w.mint_legs(u, [0, 0, 0, 0])
    per_unit = lambda: [h * 10**18 // w.supply() for h in w.holdings()]  # noqa: E731
    worst = per_unit()
    oid = 100
    open_orders = []
    steps = 0
    for _ in range(90):
        u = rng.choice(users)
        op = rng.choice(["invest", "invest", "redeem", "cancel", "deposit", "redeem_direct"])
        if op == "invest":
            amt = rng.randint(5_000_000, 400_000_000)
            oid += 1
            r, *_ = open_invest(w, u, oid, amt, fee=rng.randint(0, 300_000), min_units=1)
            if not r.ok:
                continue
            # Fill at a random honest-or-better price (the mock pays what it is told).
            o = w.order(u.pubkey(), oid)
            ok = True
            for leg in range(4):
                spend = rng.randint(o["budget"][leg] // 2, o["budget"][leg])
                out = int(fair_out(leg, spend) * rng.uniform(0.985, 1.05))
                rr = w.send([w.ix_fill_leg(GAS, u.pubkey(), oid, leg, spend, out)], [GAS])
                ok = ok and rr.ok
            if ok and rng.random() < 0.8:
                rr = w.send([w.ix_settle_invest(GAS, u.pubkey(), oid, fee=o["max_fee"])], [GAS])
                if not rr.ok:
                    assert w.send([w.ix_cancel_order(u, u.pubkey(), oid)], [u]).ok
            else:
                open_orders.append((u, oid))
        elif op == "redeem":
            units = units_of(w, u)
            if units == 0:
                continue
            oid += 1
            take = rng.randint(1, units)
            r = w.send([w.ix_order_redeem(u, GAS, oid, take, (0, 0, 0, 0), rng.randint(0, 100_000))], [u, GAS])
            if not r.ok:
                continue
            o = w.order(u.pubkey(), oid)
            for leg in range(4):
                if o["filled"] & (1 << leg):
                    continue
                rr = w.send([w.ix_fill_leg(GAS, u.pubkey(), oid, leg, o["budget"][leg], fair_in(leg, o["budget"][leg]))], [GAS])
                assert rr.ok, rr.reason()
            rr = w.send([w.ix_settle_redeem(GAS, u.pubkey(), oid, fee=o["max_fee"])], [GAS])
            assert rr.ok, rr.reason()
        elif op == "cancel" and open_orders:
            cu, coid = open_orders.pop(rng.randrange(len(open_orders)))
            assert w.send([w.ix_cancel_order(cu, cu.pubkey(), coid)], [cu]).ok
        elif op == "deposit":
            amts = [rng.randint(10**5, 10**7) for _ in range(4)]
            w.mint_legs(u, amts)
            w.send([w.ix_deposit(u, amts, 0)], [u])
        elif op == "redeem_direct":
            units = units_of(w, u)
            if units:
                w.send([w.ix_redeem(u, rng.randint(1, units))], [u])
        steps += 1
        now = per_unit()
        assert all(n >= wv for n, wv in zip(now, worst)), f"holdings per unit fell: {worst} → {now} after {op}"
        worst = now
    for cu, coid in open_orders:
        assert w.send([w.ix_cancel_order(cu, cu.pubkey(), coid)], [cu]).ok
    return f"{steps} random orders / fills / settles / cancels / direct deposits & redeems: holdings per unit never fell"


# ================================================================= U: upgrade

@test
def U1_vault_account_is_untouched_and_v3_flows_still_run():
    w = world()
    v0 = w.account_data(VAULT)
    assert len(v0) == 275, len(v0)
    u = stable_user(w, "u1")
    open_invest(w, u, 1, 50_000_000); fill_all(w, u, 1)
    assert w.send([w.ix_settle_invest(GAS, u.pubkey(), 1)], [GAS]).ok
    v1 = w.account_data(VAULT)
    assert len(v1) == 275
    # Only the fee clock (last_accrual, bytes 127..135) may move.
    assert v0[:127] == v1[:127] and v0[135:] == v1[135:], "a v3 vault field changed"
    # v3 flows on the v4 build: a locked deposit, claim; set_fees; admin handover.
    w.enable_discounts(900, 9 * UNIT // 10, 30, fund_units=50 * UNIT)
    d = V1.funded_user(w, "locker")
    pid = w.vault_v2()["next_position"]
    assert w.send([w.ix_deposit_locked(d, [10**7] * 4, 1, 365, 0)], [d]).ok
    assert w.position(pid)["bonus"] > 0
    assert w.send([w.ix_set_fees(ADMIN, 50, 50, 50)], [ADMIN]).ok
    return "VaultState 275 bytes, v3 fields byte-identical through an order; locked deposit and set_fees still work"


# ==================================================================== M: sizes

@test
def M1_user_signed_transactions_fit():
    w = world()
    u = stable_user(w, "m1")
    budget, min_out, _ = plan(100_000_000, fee=100_000)
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
    sizes = {}
    for name, ix in [("order_invest", w.ix_order_invest(u, GAS, 1, 100_000_000, budget, min_out, 1, 100_000))]:
        tx = Transaction([u, GAS], Message([set_compute_unit_limit(300_000), set_compute_unit_price(10_000), ix], u.pubkey()), w.svm.latest_blockhash())
        sizes[name] = len(bytes(tx))
    r = w.send([w.ix_order_invest(u, GAS, 1, 100_000_000, budget, min_out, 1, 100_000)], [u, GAS])
    assert r.ok, r.reason() + " ".join(r.logs[-6:])
    NOTES["cu_order_invest"] = r.raw.compute_units_consumed()
    fill_all(w, u, 1)
    assert w.send([w.ix_settle_invest(GAS, u.pubkey(), 1)], [GAS]).ok
    units = units_of(w, u)
    ix = w.ix_order_redeem(u, GAS, 2, units, (0, 0, 0, 0), 100_000)
    tx = Transaction([u, GAS], Message([set_compute_unit_limit(300_000), set_compute_unit_price(10_000), ix], u.pubkey()), w.svm.latest_blockhash())
    sizes["order_redeem"] = len(bytes(tx))
    for k, v in sizes.items():
        assert v <= 1232, f"{k} is {v} bytes"
    return ", ".join(f"{k} {v} bytes" for k, v in sizes.items()) + " (limit 1,232; two signers, compute-budget ixs included)"


def main():
    t0 = time.time()
    passed = failed = 0
    for fn in TESTS:
        try:
            detail = fn()
            passed += 1
            print(f"PASS  {fn.__name__}\n      {detail}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {fn.__name__}\n      {type(e).__name__}: {e}")
            traceback.print_exc(limit=3)
    print(f"\n{passed} passed, {failed} failed, {time.time() - t0:.1f}s")
    cu = {k[3:]: v for k, v in NOTES.items() if k.startswith("cu_")}
    if cu:
        print("compute units:", ", ".join(f"{k} {v:,}" for k, v in cu.items()))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
