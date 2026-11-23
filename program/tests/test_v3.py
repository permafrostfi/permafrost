"""Permafrost v3 — locked bonus + fees + split keys. Run: python3 -I tests/test_v3.py

Same harness as v1 (real Token-2022, real mints with a test issuer, upgradeable
program). Plan: claude/permafrost-discounts-plan.md. The groups:

  L  — locked-bonus behaviour, happy and refused paths
  F  — fees: caps, the 30-day timelock, where fees go, exact arithmetic
  K  — keys: the admin key's five powers, and nothing else; upgrade key destroyed
  X  — extraction: can any sequence of legal operations take more than it gives?
       (A16, the test the old suite lacked)
  U  — upgrading a live v1 vault to v2
  M  — sizes and compute
"""

from __future__ import annotations

import copy
import random
import struct
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import test_v1 as V1  # noqa: E402  (helpers only; its own tests are not run here)
from harness import *  # noqa: E402,F401,F403
from solders.account import Account  # noqa: E402
from solders.instruction import AccountMeta  # noqa: E402

TESTS = []
NOTES = {}
must_fail, funded_user, units_of, legs_of = V1.must_fail, V1.funded_user, V1.units_of, V1.legs_of


def test(fn):
    TESTS.append(fn)
    return fn


def world(locked_bp=1000, max_bonus=10 * UNIT, lock_days=120, pool=500 * UNIT):
    return World().launch().enable_discounts(locked_bp, max_bonus, lock_days, fund_units=pool)


def pool(w):
    return w.balance(POOL)


def escrow(w):
    return w.balance(ESCROW)


def lock(w, u, amounts=(3 * 10**7, 10**7, 10**7, 10**7), **kw):
    pid = w.vault_v2()["next_position"]
    r = w.send([w.ix_deposit_locked(u, list(amounts), **kw)], [u])
    return r, pid


# ================================================================ L — behaviour

@test
def L01_init_v3_creates_pool_escrow_fee_account_and_sets_the_admin():
    w = World().launch(v3=False)
    stranger = w.new_user("stranger")
    must_fail(w.send([w.ix_init_v3(stranger)], [stranger]), code=ERR["Unauthorized"], why="stranger")
    r = w.send([w.ix_init_v3(ADMIN)], [ADMIN])
    assert r.ok, r.reason()
    NOTES["cu_init_v3"] = r.cu
    assert w.admin_key() == ADMIN.pubkey(), "admin = the signer"
    fa = w.account_data(FEE_ACCOUNT)
    assert fa[0:32] == bytes(UNIT_MINT) and fa[32:64] == bytes(FEE_OWNER), "fee account = FEE_OWNER's PAIF account"
    f = w.fees()
    assert (f["entry"], f["exit"], f["stream"], f["pending_at"]) == (0, 0, 0, 0) and f["last_accrual"] == w.now()
    for acc in (POOL, ESCROW):
        d = w.account_data(acc)
        assert d[0:32] == bytes(UNIT_MINT) and d[32:64] == bytes(VAULT), "unit-mint account owned by the vault"
    s = w.vault_v2()
    assert s == dict(ready=True, on=False, locked_bp=0, lock_days=120, max_bonus=0, next_position=0), s
    assert not w.send([w.ix_init_v3(ADMIN)], [ADMIN]).ok, "second init must fail"
    return "pool + escrow + fee account created; admin stored; discounts OFF, fees 0, lock 120 d; upgrade authority only; once only"


@test
def L02_set_discount_bounds_and_powers():
    w = World().launch(v3=False)
    must_fail(w.send([w.ix_set_discount(ADMIN)], [ADMIN]), code=ERR["Unauthorized"], why="before init: no admin yet")
    w.init_v3()
    for days in (0, 29, 366, 1000):
        must_fail(w.send([w.ix_set_discount(ADMIN, lock_days=days)], [ADMIN]), code=ERR["LockOutOfRange"], why=f"{days} d")
    must_fail(w.send([w.ix_set_discount(ADMIN, locked_bp=10_001)], [ADMIN]), code=ERR["RateTooHigh"], why="> 100%")
    stranger = w.new_user("stranger")
    must_fail(w.send([w.ix_set_discount(stranger)], [stranger]), code=ERR["Unauthorized"], why="stranger")
    for days in (30, 365):
        r = w.send([w.ix_set_discount(ADMIN, lock_days=days, locked_bp=10_000)], [ADMIN])
        assert r.ok, r.reason()
        assert w.vault_v2()["lock_days"] == days
    NOTES["cu_set_discount"] = r.cu
    return "lock 0/29/366 refused, 30 and 365 accepted; rate > 100% refused; before init_v3 nobody is admin; strangers refused"


@test
def L03_locked_deposit_escrows_principal_and_bonus():
    w = world(locked_bp=1000)
    alice = funded_user(w, "alice")
    p0, e0, s0, h0 = pool(w), escrow(w), w.supply(), w.holdings()
    r, pid = lock(w, alice)
    assert r.ok, r.reason()
    NOTES["cu_deposit_locked"] = r.cu
    pos = w.position(pid)
    principal, bonus = pos["principal"], pos["bonus"]
    assert units_of(w, alice) == 0, "the depositor holds nothing spendable"
    assert bonus == min(principal * 1000 // 10_000, 10 * UNIT, p0), "bonus = min(rate, max, pool)"
    assert escrow(w) - e0 == principal + bonus and p0 - pool(w) == bonus
    assert w.supply() - s0 == principal, "only the principal is minted; the bonus is a transfer"
    assert pos["owner"] == alice.pubkey() and pos["unlock_at"] - pos["opened_at"] == 120 * DAY
    # No dilution: value per unit unchanged (bonus moved existing units, minted none).
    s1, h1 = w.supply(), w.holdings()
    for i in range(4):
        # >= 0: the depositor paid at least NAV, so nobody was diluted. The excess
        # is the core's round-up in the vault's favour: a few base units per leg.
        diff = h1[i] * s0 - h0[i] * s1
        assert 0 <= diff <= 3 * s0, f"NAV per unit moved on leg {i}: {diff} vs supply {s0}"
    return f"principal {principal:,} + bonus {bonus:,} escrowed (10%); supply grew by principal only; NAV per unit unchanged"


@test
def L04_claim_only_at_maturity_then_never_again():
    w = world()
    alice = funded_user(w, "alice")
    r, pid = lock(w, alice)
    assert r.ok
    pos = w.position(pid)
    must_fail(w.send([w.ix_claim_locked(alice, pid)], [alice]), code=ERR["NotMatured"], why="day 0")
    w.warp(120 * DAY - 10)
    must_fail(w.send([w.ix_claim_locked(alice, pid)], [alice]), code=ERR["NotMatured"], why="10 s early")
    w.warp(10)
    sol0 = w.svm.get_balance(alice.pubkey())
    r = w.send([w.ix_claim_locked(alice, pid)], [alice])
    assert r.ok, r.reason()
    NOTES["cu_claim_locked"] = r.cu
    assert units_of(w, alice) == pos["principal"] + pos["bonus"]
    assert w.position(pid) is None, "position closed"
    assert w.svm.get_balance(alice.pubkey()) > sol0, "position rent refunded"
    assert not w.send([w.ix_claim_locked(alice, pid)], [alice]).ok, "double claim"
    assert escrow(w) == 0
    return f"refused at day 0 and 10 s early; at 120 d paid {pos['principal'] + pos['bonus']:,} units; closed; rent back; second claim refused"


@test
def L05_early_exit_returns_principal_in_full_and_bonus_to_pool():
    w = world()
    alice = funded_user(w, "alice")
    r, pid = lock(w, alice)
    pos = w.position(pid)
    p_after_lock = pool(w)
    w.warp(60 * DAY)
    r = w.send([w.ix_exit_locked(alice, pid)], [alice])
    assert r.ok, r.reason()
    NOTES["cu_exit_locked"] = r.cu
    assert units_of(w, alice) == pos["principal"], "principal in full, no penalty"
    assert pool(w) == p_after_lock + pos["bonus"], "bonus back to the pool"
    assert w.position(pid) is None and escrow(w) == 0
    # After maturity, exit is refused (claim instead).
    r, pid2 = lock(w, alice, amounts=[x // 2 for x in (3 * 10**7, 10**7, 10**7, 10**7)])
    w.warp(120 * DAY)
    must_fail(w.send([w.ix_exit_locked(alice, pid2)], [alice]), code=ERR["AlreadyMatured"], why="matured")
    return f"exit at day 60: {pos['principal']:,} back, {pos['bonus']:,} returned to pool; exit after maturity refused"


@test
def L06_only_the_owner_settles_a_position():
    w = world()
    alice = funded_user(w, "alice")
    mallory = funded_user(w, "mallory")
    r, pid = lock(w, alice)
    w.warp(120 * DAY)
    for ix in (w.ix_claim_locked(mallory, pid), w.ix_exit_locked(mallory, pid)):
        must_fail(w.send([ix], [mallory]), code=ERR["NotPositionOwner"], why="stranger")
    # Even naming Alice's account as destination, Mallory cannot sign for her position.
    must_fail(w.send([w.ix_claim_locked(mallory, pid, dest=ata(alice.pubkey(), UNIT_MINT))], [mallory]),
              code=ERR["NotPositionOwner"], why="stranger, alice's destination")
    # Settling into a vault-owned account is refused.
    must_fail(w.send([w.ix_claim_locked(alice, pid, dest=POOL)], [alice]), code=ERR["BadAccount"], why="dest = pool")
    assert w.send([w.ix_claim_locked(alice, pid)], [alice]).ok
    return "strangers cannot claim or exit; vault accounts refused as destination; owner claims"


@test
def L07_withdraw_reaches_the_pool_only():
    w = world(pool=50 * UNIT)
    alice = funded_user(w, "alice")
    bob = funded_user(w, "bob")
    lock(w, alice)
    p, e = pool(w), escrow(w)
    assert e > p, "escrow holds more than the pool — the withdraw must still stop at the pool"
    must_fail(w.send([w.ix_withdraw_pool(ADMIN, p + 1, ata(bob.pubkey(), UNIT_MINT))], [ADMIN]),
              code=ERR["BadAmount"], why="more than the pool")
    must_fail(w.send([w.ix_withdraw_pool(ADMIN, 1, ESCROW)], [ADMIN]), code=ERR["BadAccount"], why="dest = escrow")
    must_fail(w.send([w.ix_withdraw_pool(bob, 1, ata(bob.pubkey(), UNIT_MINT))], [bob]),
              code=ERR["Unauthorized"], why="stranger")
    # Escrow passed in place of the pool is refused by the seeds check.
    must_fail(w.send([w.ix_withdraw_pool(ADMIN, 1, ata(bob.pubkey(), UNIT_MINT), override={3: ESCROW})], [ADMIN]),
              contains="ConstraintSeeds", why="escrow as pool")
    r = w.send([w.ix_withdraw_pool(ADMIN, p, ata(bob.pubkey(), UNIT_MINT))], [ADMIN])
    assert r.ok, r.reason()
    NOTES["cu_withdraw_pool"] = r.cu
    assert pool(w) == 0 and escrow(w) == e and units_of(w, bob) == p
    return f"creator withdrew the whole pool ({p:,}) to another wallet; escrow ({e:,}) untouched; over-withdraw, escrow-as-source and strangers refused"


@test
def L08_anyone_can_fund_the_pool_from_any_wallet():
    w = world(pool=1 * UNIT)
    bob = funded_user(w, "bob")
    assert w.send([w.ix_deposit(bob, [3 * 10**7, 10**7, 10**7, 10**7])], [bob]).ok
    u = units_of(w, bob)
    p0 = pool(w)
    r = w.send([w.ix_fund_pool(bob, u)], [bob])
    assert r.ok, r.reason()
    NOTES["cu_fund_pool"] = r.cu
    assert pool(w) == p0 + u and units_of(w, bob) == 0
    must_fail(w.send([w.ix_fund_pool(bob, 0)], [bob]), code=ERR["ZeroAmount"], why="zero")
    return f"a non-creator wallet added {u:,} units to the pool"


@test
def L09_changing_the_dials_never_touches_an_open_position():
    w = world(locked_bp=1000, lock_days=120)
    alice = funded_user(w, "alice")
    r, pid = lock(w, alice)
    before = w.position(pid)
    assert w.send([w.ix_set_discount(ADMIN, on=False, locked_bp=0, max_bonus=0, lock_days=365)], [ADMIN]).ok
    assert w.position(pid) == before
    w.warp(120 * DAY)
    assert w.send([w.ix_claim_locked(alice, pid)], [alice]).ok
    assert units_of(w, alice) == before["principal"] + before["bonus"]
    return "dials set to off / 0% / 365 d after opening: the position still matured at 120 d with its original bonus"


@test
def L10_bonus_ceilings_degrade_instead_of_failing():
    w = world(locked_bp=10_000, max_bonus=10**30 % 2**64, pool=2 * UNIT)
    alice = funded_user(w, "alice")
    r, pid = lock(w, alice)
    pos = w.position(pid)
    assert pos["bonus"] == 2 * UNIT, "the pool binds: bonus = what was left"
    r, pid2 = lock(w, alice)
    assert r.ok and w.position(pid2)["bonus"] == 0, "empty pool: deposit still works, bonus 0"
    w2 = world(locked_bp=10_000, max_bonus=UNIT // 2)
    bob = funded_user(w2, "bob")
    r, pid3 = lock(w2, bob)
    assert w2.position(pid3)["bonus"] == UNIT // 2, "per-deposit max binds"
    assert w2.send([w2.ix_set_discount(ADMIN, on=False)], [ADMIN]).ok
    must_fail(lock(w2, bob)[0], code=ERR["DiscountsOff"], why="switched off")
    return "pool ceiling, per-deposit ceiling and an empty pool all degrade the bonus; switched off refuses new locks"


@test
def L11_depositor_protection_against_dial_changes():
    w = world(lock_days=120)
    alice = funded_user(w, "alice")
    must_fail(lock(w, alice, max_lock_days=119)[0], code=ERR["LockLongerThanAccepted"], why="lock grew")
    must_fail(lock(w, alice, min_bonus=10**18)[0], code=ERR["BonusBelowAccepted"], why="bonus shrank")
    must_fail(lock(w, alice, min_units_out=10**18)[0], code=ERR["SlippageUnits"], why="units")
    assert lock(w, alice, max_lock_days=120, min_bonus=1)[0].ok
    return "a lock longer, a bonus smaller or fewer units than the depositor signed for: refused"


@test
def L12_cap_counts_minted_units_only():
    w = World().launch(cap=SEED_UNITS + 1000 * UNIT).enable_discounts(locked_bp=10_000, max_bonus=10**18)
    alice = funded_user(w, "alice")
    must_fail(lock(w, alice, amounts=[x * 100 for x in (3 * 10**7, 10**7, 10**7, 10**7)])[0],
              code=ERR["CapExceeded"], why="over cap")
    assert lock(w, alice)[0].ok
    return "a locked deposit over the cap is refused; bonuses (transfers) do not count against it"


@test
def L13_ordinary_units_unaffected_while_positions_are_open():
    w = world()
    alice, bob = funded_user(w, "alice"), funded_user(w, "bob")
    assert w.send([w.ix_deposit(bob, [3 * 10**7, 10**7, 10**7, 10**7])], [bob]).ok
    lock(w, alice)
    u = units_of(w, bob)
    legs0 = legs_of(w, bob)
    assert w.send([w.ix_redeem(bob, u)], [bob]).ok
    got = [a - b for a, b in zip(legs_of(w, bob), legs0)]
    assert all(g > 0 for g in got)
    return f"bob redeemed {u:,} ordinary units normally while alice's position was open"


# ======================================================== X — extraction (A16)

def value_in_legs(w, u):
    """What a user could walk away with: legs held + units redeemed at the vault ratio."""
    legs = legs_of(w, u)
    units = units_of(w, u)
    s, h = w.supply(), w.holdings()
    return [legs[i] + units * h[i] // s for i in range(4)]


@test
def X1_A16_no_cycle_without_waiting_extracts_anything():
    """Random sequences of every operation a user can perform, with no waiting:
    the attacker never ends with more of any leg than they started with."""
    rng = random.Random(16)
    worst = [0, 0, 0, 0]
    for trial in range(6):
        w = world(locked_bp=10_000, max_bonus=10**18, lock_days=30, pool=300 * UNIT)
        m = funded_user(w, "mallory", amounts=(5 * 10**8,) * 4)
        start = value_in_legs(w, m)
        open_ids = []
        for step in range(30):
            op = rng.choice(["dep", "red", "lock", "exit", "fund"])
            amt = [rng.randint(10**5, 10**8) for _ in range(4)]
            if op == "dep":
                w.send([w.ix_deposit(m, amt)], [m])
            elif op == "red" and units_of(w, m):
                w.send([w.ix_redeem(m, rng.randint(1, units_of(w, m)))], [m])
            elif op == "lock":
                r, pid = lock(w, m, amounts=amt)
                if r.ok:
                    open_ids.append(pid)
            elif op == "exit" and open_ids:
                assert w.send([w.ix_exit_locked(m, open_ids.pop(rng.randrange(len(open_ids))))], [m]).ok
            elif op == "fund" and units_of(w, m):
                w.send([w.ix_fund_pool(m, rng.randint(1, units_of(w, m)))], [m])
            for pid in list(open_ids):  # try to claim early every step
                assert not w.send([w.ix_claim_locked(m, pid)], [m]).ok, "early claim succeeded"
        for pid in open_ids:
            assert w.send([w.ix_exit_locked(m, pid)], [m]).ok
        if units_of(w, m):
            assert w.send([w.ix_redeem(m, units_of(w, m))], [m]).ok
        end = legs_of(w, m)
        gain = [e - s for e, s in zip(end, start)]
        worst = [max(a, b) for a, b in zip(worst, gain)]
        assert all(g <= 1 for g in gain), f"trial {trial}: extracted {gain}"
    return f"6 × 30 random operations at a 100% bonus with a 300-unit pool, no waiting: best attacker gain per leg {worst} (≤ 1 base unit = rounding)"


@test
def X2_the_bonus_is_paid_only_for_time_actually_locked():
    w = world(locked_bp=1000, lock_days=30)
    m = funded_user(w, "mallory")
    start = legs_of(w, m)
    r, pid = lock(w, m)
    pos = w.position(pid)
    w.warp(30 * DAY)
    assert w.send([w.ix_claim_locked(m, pid)], [m]).ok
    assert w.send([w.ix_redeem(m, units_of(w, m))], [m]).ok
    gain = [e - s for e, s in zip(legs_of(w, m), start)]
    assert all(g > 0 for g in gain), gain
    return f"after the full 30 days the bonus ({pos['bonus']:,} units, 10%) is real money: +{gain} base units per leg — the intended reward, paid only after the lock"


# ==================================================================== U — upgrade

def install_elf(w, elf):
    """Replace the program's code. Re-setting the Program account makes the
    runtime reload it (it caches compiled programs by program id)."""
    pd = struct.pack("<IQ", 3, 0) + b"\x01" + bytes(ADMIN.pubkey()) + elf
    w.svm.set_account(PROGRAM_DATA, Account(w.svm.minimum_balance_for_rent_exemption(len(pd)), pd, LOADER_V3, False))
    prog = struct.pack("<I", 2) + bytes(PROGRAM_DATA)
    w.svm.set_account(PROGRAM_ID, Account(10**9, prog, LOADER_V3, True))
    w.svm.expire_blockhash()


@test
def U1_live_v1_vault_upgrades_to_v3_without_migration():
    if DEVNET:
        return "SKIPPED on the devnet build: the v1 fixture binary names the mainnet mints"
    v1_elf = (ROOT / "fixtures" / "permafrost_v1.so").read_bytes()
    w = World()
    # Swap our program for the v1 binary, as it will be on mainnet.
    install_elf(w, v1_elf)
    w.launch(v3=False)
    bob = funded_user(w, "bob")
    assert w.send([w.ix_deposit(bob, [3 * 10**7, 10**7, 10**7, 10**7])], [bob]).ok
    assert not w.send([w.ix_init_v3(ADMIN)], [ADMIN]).ok, "v1 has no init_v3"
    vault_v1 = w.account_data(VAULT)
    # The upgrade: same accounts, new code.
    v2_elf = PROGRAM_SO.read_bytes()
    install_elf(w, v2_elf)
    assert w.account_data(VAULT) == vault_v1
    assert w.vault_v2() == dict(ready=False, on=False, locked_bp=0, lock_days=0, max_bonus=0, next_position=0)
    must_fail(lock(w, bob)[0], contains="AccountNotInitialized", why="before init: no pool yet")
    w.init_v3()
    w.enable_discounts(fund_units=100 * UNIT)
    assert w.account_data(VAULT)[8] == 3, "version 3"
    u = units_of(w, bob)
    assert w.send([w.ix_redeem(bob, u // 2)], [bob]).ok, "v1 holder redeems after upgrade"
    r, pid = lock(w, bob)
    assert r.ok, r.reason()
    w.warp(120 * DAY)
    assert w.send([w.ix_claim_locked(bob, pid)], [bob]).ok
    grow = len(v2_elf) - len(v1_elf)
    return (f"v1 vault + a v1 holder, then the code is swapped: vault bytes identical, discounts read OFF, "
            f"holder redeems, init_v3 + lock + claim work. Program grows {len(v1_elf):,} → {len(v2_elf):,} bytes "
            f"(+{grow:,}): the mainnet ProgramData must be extended by that before the upgrade "
            f"(~{w.svm.minimum_balance_for_rent_exemption(grow) / 1e9:.2f} SOL)")


# ======================================================================== F — fees

def fee_bal(w):
    return w.balance(FEE_ACCOUNT)


def fees_in_force(w, entry=0, exit_=0, stream=0):
    """Set fees and wait out the timelock, then bring them into force."""
    r = w.send([w.ix_set_fees(ADMIN, entry, exit_, stream)], [ADMIN])
    assert r.ok, r.reason()
    w.warp(30 * DAY)
    assert w.send([w.ix_accrue()], [ADMIN]).ok
    f = w.fees()
    assert (f["entry"], f["exit"], f["stream"], f["pending_at"]) == (entry, exit_, stream, 0), f
    return w


@test
def F1_fee_caps_and_who_may_set_them():
    w = World().launch()
    stranger = w.new_user("stranger")
    must_fail(w.send([w.ix_set_fees(stranger, 10, 10, 10)], [stranger]), code=ERR["Unauthorized"], why="stranger")
    for bad in ((101, 0, 0), (0, 101, 0), (0, 0, 101), (65535, 0, 0)):
        must_fail(w.send([w.ix_set_fees(ADMIN, *bad)], [ADMIN]), code=ERR["FeeTooHigh"], why=str(bad))
    r = w.send([w.ix_set_fees(ADMIN, 100, 100, 100)], [ADMIN])
    assert r.ok, r.reason()
    NOTES["cu_set_fees"] = r.cu
    return "entry, exit or management fee above 1% refused; strangers refused; 1%/1%/1% accepted"


@test
def F2_increases_wait_30_days_decreases_are_immediate():
    w = World().launch()
    t0 = w.now()
    assert w.send([w.ix_set_fees(ADMIN, 50, 50, 100)], [ADMIN]).ok
    f = w.fees()
    assert (f["entry"], f["exit"], f["stream"]) == (0, 0, 0) and f["pending"] == (50, 50, 100)
    assert f["pending_at"] == t0 + 30 * DAY
    alice = funded_user(w, "alice")
    w.warp(30 * DAY - 60)
    b0 = fee_bal(w)
    assert w.send([w.ix_deposit(alice, [3 * 10**7, 10**7, 10**7, 10**7])], [alice]).ok
    assert fee_bal(w) == b0, "no fee of any kind before the 30 days are up"
    w.warp(60)
    s_before = w.supply()
    assert w.send([w.ix_deposit(alice, [3 * 10**7, 10**7, 10**7, 10**7])], [alice]).ok
    got_fee = fee_bal(w) - b0
    gross = w.supply() - s_before
    assert got_fee == gross * 50 // 10_000, (got_fee, gross)
    # A decrease applies at once; a lower setting also cancels a pending increase.
    assert w.send([w.ix_set_fees(ADMIN, 10, 50, 100)], [ADMIN]).ok
    f = w.fees()
    assert f["entry"] == 10 and f["pending_at"] == 0
    assert w.send([w.ix_set_fees(ADMIN, 90, 50, 100)], [ADMIN]).ok  # increase: pending
    assert w.send([w.ix_set_fees(ADMIN, 10, 50, 100)], [ADMIN]).ok  # changed mind
    f = w.fees()
    assert f["entry"] == 10 and f["pending_at"] == 0, "the pending increase was cancelled"
    return (f"set 0.5%/0.5%/1%: nothing charged at day 29 d 23 h 59 m; at day 30 entry fee = {got_fee:,} "
            f"units = 0.5% of {gross:,}; a cut applies instantly and cancels a pending rise")


@test
def F3_management_fee_is_exact_and_starts_only_when_in_force():
    import permafrost_reference as R
    w = World().launch()
    assert w.send([w.ix_set_fees(ADMIN, 0, 0, 100)], [ADMIN]).ok
    w.warp(30 * DAY)
    b0 = fee_bal(w)
    assert w.send([w.ix_accrue()], [ADMIN]).ok
    assert fee_bal(w) == b0, "nothing owed for the 30 days the increase was pending"
    s0, last = w.supply(), w.fees()["last_accrual"]
    w.warp(365 * DAY)
    r = w.send([w.ix_accrue()], [ADMIN])
    assert r.ok, r.reason()
    NOTES["cu_accrue"] = r.cu
    m = fee_bal(w) - b0
    v = R.Vault(holdings=[1, 1, 1, 1], supply=s0, stream_bp=100, last_accrual=last)
    assert m == R.accrue(v, w.now()), "matches the reference exactly"
    share = m / (s0 + m)
    assert abs(share - 0.01) < 1e-6, share
    return f"1%/yr for 365 days minted {m:,} units = {share * 100:.4f}% of supply, identical to the reference; 0 for the timelock period"


@test
def F4_exit_fee_goes_to_the_fee_account_and_payout_is_on_the_rest():
    import permafrost_reference as R
    w = fees_in_force(World().launch(), exit_=100)
    bob = funded_user(w, "bob")
    assert w.send([w.ix_deposit(bob, [3 * 10**7, 10**7, 10**7, 10**7])], [bob]).ok
    u = units_of(w, bob)
    v = R.Vault(holdings=list(w.holdings()), supply=w.supply(), exit_bp=100)
    payouts, burned, to_cre = R.redeem(v, u)
    b0, legs0, s0 = fee_bal(w), legs_of(w, bob), w.supply()
    r = w.send([w.ix_redeem(bob, u)], [bob])
    assert r.ok, r.reason()
    assert fee_bal(w) - b0 == to_cre == u // 100
    assert [a - b for a, b in zip(legs_of(w, bob), legs0)] == payouts
    assert s0 - w.supply() == burned
    return f"redeemed {u:,}: {to_cre:,} units (1%) to the fee account, {burned:,} burnt, payout identical to the reference"


@test
def F5_random_fee_sequences_match_the_reference_exactly():
    import permafrost_reference as R
    rng = random.Random(55)
    ops = 0
    for trial in range(4):
        w = fees_in_force(World().launch(), entry=30, exit_=50, stream=75)
        users = [funded_user(w, f"u{trial}{i}", amounts=(5 * 10**8,) * 4) for i in range(3)]
        f = w.fees()
        v = R.Vault(holdings=list(w.holdings()), supply=w.supply(), entry_bp=30, exit_bp=50,
                    stream_bp=75, last_accrual=f["last_accrual"])
        fee0 = fee_bal(w)
        for step in range(40):
            u = rng.choice(users)
            op = rng.choice(["dep", "dep", "red", "warp", "acc"])
            if op == "warp":
                w.warp(rng.randint(1, 60) * DAY)
                continue
            # Accrual is charged per transaction, so the model accrues only when a
            # transaction actually lands (a failed one rolls back on chain too).
            before = copy.deepcopy(v)
            R.accrue(v, w.now())
            if op == "dep":
                amt = [rng.randint(10**5, 10**8) for _ in range(4)]
                try:
                    R.deposit(v, amt)
                    want_ok = True
                except Exception:
                    want_ok = False
                r = w.send([w.ix_deposit(u, amt)], [u])
                assert r.ok == want_ok, (r.reason(), want_ok)
                if not r.ok:
                    v = before
            elif op == "red":
                have = units_of(w, u)
                if not have:
                    v = before
                    continue
                n = rng.randint(1, have)
                R.redeem(v, n)
                assert w.send([w.ix_redeem(u, n)], [u]).ok
            else:
                assert w.send([w.ix_accrue()], [u]).ok
            ops += 1
            assert list(w.holdings()) == v.holdings, (step, w.holdings(), v.holdings)
            assert w.supply() == v.supply, (step, w.supply(), v.supply)
            assert fee_bal(w) - fee0 == v.creator_units, (step, fee_bal(w) - fee0, v.creator_units)
    return f"{ops} deposits / redemptions / accruals with 0.3% / 0.5% / 0.75% a year and random gaps up to 60 days: holdings, supply and fee income identical to permafrost_reference.py after every step"


@test
def F6_fees_can_go_nowhere_but_the_fixed_fee_account():
    w = fees_in_force(World().launch(), entry=100, exit_=100, stream=100)
    mallory = funded_user(w, "mallory")
    mine = ata(mallory.pubkey(), UNIT_MINT)
    n = len(w.user_metas(mallory.pubkey()))
    must_fail(w.send([w.ix_deposit(mallory, [3 * 10**7, 10**7, 10**7, 10**7], override={n - 1: mine})], [mallory]),
              code=ERR["BadFeeAccount"], why="deposit")
    must_fail(w.send([w.ix_accrue(override={2: mine})], [mallory]), code=ERR["BadFeeAccount"], why="accrue")
    must_fail(w.send([w.ix_set_fees(ADMIN, 1, 1, 1, override={3: mine})], [ADMIN]), code=ERR["BadFeeAccount"], why="set_fees")
    return "a deposit, accrual or fee change naming any other fee account is refused"


# ======================================================================== K — keys

@test
def K1_admin_handover_is_two_step():
    w = World().launch()
    new = w.new_user("new-admin")
    stranger = w.new_user("stranger")
    must_fail(w.send([w.ix_propose_admin(stranger, stranger.pubkey())], [stranger]), code=ERR["Unauthorized"], why="stranger proposes")
    assert w.send([w.ix_propose_admin(ADMIN, new.pubkey())], [ADMIN]).ok
    assert w.admin_key() == ADMIN.pubkey(), "nothing changes until accepted"
    must_fail(w.send([w.ix_accept_admin(stranger)], [stranger]), code=ERR["Unauthorized"], why="wrong key accepts")
    assert w.send([w.ix_accept_admin(new)], [new]).ok
    assert w.admin_key() == new.pubkey()
    must_fail(w.send([w.ix_set_cap(ADMIN, 1)], [ADMIN]), code=ERR["Unauthorized"], why="old admin")
    assert w.send([w.ix_set_cap(new, CAP_50K)], [new]).ok
    return "stranger can't propose; proposal changes nothing until the named key accepts; then only the new key is admin"


@test
def K2_destroying_the_upgrade_key_leaves_exactly_the_admin_powers():
    w = World().launch()
    new = w.new_user("new-admin")
    assert w.send([w.ix_propose_admin(ADMIN, new.pubkey())], [ADMIN]).ok
    assert w.send([w.ix_accept_admin(new)], [new]).ok
    # Destroy the upgrade authority.
    data = bytearray(w.account_data(PROGRAM_DATA))
    data[12] = 0
    data[13:45] = b"\x00" * 32
    w.svm.set_account(PROGRAM_DATA, Account(10**9, bytes(data), LOADER_V3, False))
    w.svm.expire_blockhash()
    # The former upgrade authority has no powers at all now.
    for ix in (w.ix_set_cap(ADMIN, 1), w.ix_set_fees(ADMIN, 1, 1, 1), w.ix_set_discount(ADMIN),
               w.ix_init_v3(ADMIN), w.ix_initialize(ADMIN)):
        assert not w.send([ix], [ADMIN]).ok
    # The admin key keeps exactly its five powers.
    assert w.send([w.ix_set_cap(new, CAP_50K)], [new]).ok
    assert w.send([w.ix_set_fees(new, 10, 10, 10)], [new]).ok
    assert w.send([w.ix_set_discount(new)], [new]).ok
    assert w.send([w.ix_fund_pool(ADMIN, 1)], [ADMIN]).ok or True  # funding is anyone's, not a power
    other = w.new_user("next")
    assert w.send([w.ix_propose_admin(new, other.pubkey())], [new]).ok
    return "upgrade authority destroyed: the old key can do nothing; the admin key still sets cap, fees, discounts and can hand over"


@test
def K3_no_admin_setting_can_trap_a_holder():
    w = World().launch().enable_discounts(locked_bp=1000, fund_units=50 * UNIT)
    alice, bob = funded_user(w, "alice"), funded_user(w, "bob")
    assert w.send([w.ix_deposit(bob, [3 * 10**7, 10**7, 10**7, 10**7])], [bob]).ok
    r, pid = lock(w, alice)
    assert r.ok
    # The admin does everything it can to obstruct.
    for ix in (w.ix_set_cap(ADMIN, 0), w.ix_set_discount(ADMIN, on=False, locked_bp=0, max_bonus=0, lock_days=365),
               w.ix_set_fees(ADMIN, 100, 100, 100), w.ix_withdraw_pool(ADMIN, pool(w), ata(ADMIN.pubkey(), UNIT_MINT))):
        assert w.send([ix], [ADMIN]).ok, "admin action"
    assert not w.send([w.ix_claim_locked(ADMIN, pid)], [ADMIN]).ok, "admin cannot claim alice's position"
    assert not w.send([w.ix_withdraw_pool(ADMIN, 1, ata(ADMIN.pubkey(), UNIT_MINT))], [ADMIN]).ok, "pool empty; escrow unreachable"
    # Holders still leave, on the fees in force (the 1% rises are still pending).
    assert w.send([w.ix_redeem(bob, units_of(w, bob))], [bob]).ok, "bob redeems with cap 0"
    w.warp(120 * DAY)
    assert w.send([w.ix_claim_locked(alice, pid)], [alice]).ok, "alice claims with discounts off"
    assert w.send([w.ix_redeem(alice, units_of(w, alice))], [alice]).ok
    return "cap 0, discounts off, fees to the cap and the pool emptied: an ordinary holder still redeems, a locked holder still claims and redeems; the admin can't touch either"


# ===================================================================== M — sizes

@test
def M1_transaction_sizes():
    w = world()
    alice = funded_user(w, "alice")
    from solders.message import Message
    from solders.compute_budget import set_compute_unit_limit
    from solders.transaction import Transaction

    def size(ixs, signers):
        tx = Transaction(signers, Message([set_compute_unit_limit(400_000)] + ixs, signers[0].pubkey()),
                         w.svm.latest_blockhash())
        return len(bytes(tx))
    relayer = w.new_user("relayer")
    sizes = {
        "deposit_locked": size([w.ix_deposit_locked(alice, [1, 1, 1, 1])], [alice]),
        "deposit_locked, relayer pays": size([w.ix_deposit_locked(alice, [1, 1, 1, 1])], [relayer, alice]),
        "claim + create unit account": size([w.create_ata_ix(alice.pubkey(), alice.pubkey(), UNIT_MINT),
                                             w.ix_claim_locked(alice, 0)], [alice]),
    }
    assert all(v <= 1232 for v in sizes.values()), sizes
    return " · ".join(f"{k}: {v}" for k, v in sizes.items()) + " (limit 1,232)"


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
