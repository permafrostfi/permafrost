#!/usr/bin/env python3
"""Permafrost v1 program — the test suite promised in the approved layout.

    python3 -I tests/test_v1.py            # from ~/pf/program, after cargo-build-sbf --arch v3

Every test builds a fresh chain (see harness.py): mainnet's real Token-2022 and
ATA programs, the four REAL xStock mints with Backed's keys swapped for a test
issuer, and our program installed as upgradeable with a test admin as upgrade
authority.

Sections:
  H  happy paths
  A  adversarial — every one MUST fail, and for the right reason
  E  economics and issuer powers — drain, pause, freeze, multiplier, donation
  D  differential — random sequences, every number compared with the Python reference
  M  measurements — transaction size, compute, deploy cost
"""

from __future__ import annotations

import random
import struct
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # permafrost_reference.py

import permafrost_reference as R  # noqa: E402
from harness import *  # noqa: E402,F401,F403
from solders.instruction import AccountMeta, Instruction  # noqa: E402

TESTS = []
NOTES = {}


def test(fn):
    TESTS.append(fn)
    return fn


def must_fail(r, *, code=None, contains=None, why=""):
    assert not r.ok, f"expected failure ({why}) but it SUCCEEDED"
    if code is not None:
        got = r.custom_code()
        assert got == code, f"expected error {code}, got {got}: {r.reason()}"
    if contains is not None:
        text = (r.reason() or "") + " " + " ".join(r.logs)
        assert contains in text, f"expected '{contains}' in failure, got: {r.reason()}"
    return r.reason()


def ref_deposit(holdings, supply, delivered):
    v = R.Vault(holdings=list(holdings), supply=supply)
    to_dep, to_cre, refunds = R.deposit(v, list(delivered))
    return to_dep, [d - f for d, f in zip(delivered, refunds)]


def ref_redeem(holdings, supply, units):
    v = R.Vault(holdings=list(holdings), supply=supply)
    payouts, burned, to_cre = R.redeem(v, units)
    return payouts


def funded_user(w, label, amounts=(10**9,) * 4):
    u = w.new_user(label)
    w.mint_legs(u, list(amounts))
    w.ensure_unit_ata(u)
    return u


def units_of(w, u):
    return w.balance(ata(u.pubkey(), UNIT_MINT))


def legs_of(w, u):
    return [w.balance(ata(u.pubkey(), m)) for m in LEG_MINTS]


def snapshot(w, *users):
    return (tuple(w.holdings()), w.supply(),
            tuple(tuple(legs_of(w, u)) + (units_of(w, u),) for u in users))


def mint_extensions(data: bytes):
    """Token-2022 TLV extension types present on a mint account."""
    out, off = [], 166
    while off + 4 <= len(data):
        t, ln = struct.unpack_from("<HH", data, off)
        if t == 0:
            break
        out.append(t)
        off += 4 + ln
    return out


# =========================================================== H — happy paths

@test
def H1_initialize_creates_exactly_what_the_layout_says():
    w = World()
    r = w.send([w.ix_initialize(ADMIN)], [ADMIN])
    assert r.ok, r.reason()
    NOTES["cu_initialize"] = r.cu
    assert w.vault_cap() == CAP_50K, "cap"
    m = w.account_data(UNIT_MINT)
    assert struct.unpack_from("<I", m, 0)[0] == 1 and m[4:36] == bytes(VAULT), "mint authority must be the vault PDA"
    assert m[44] == 9, "9 decimals"
    assert struct.unpack_from("<I", m, 46)[0] == 0, "L3: NO freeze authority"
    exts = mint_extensions(m)
    assert 12 not in exts, f"L3: NO permanent delegate (extensions {exts})"
    assert 14 not in exts, "no transfer hook on our own token"
    assert set(exts) == {18, 19}, f"only metadata pointer + metadata expected, got {exts}"
    assert b"Permafrost" in m and b"PERMA" in m, "on-chain name and symbol"
    d = w.account_data(DEAD_UNITS)
    assert d[32:64] == bytes(DEAD_OWNER), "dead-units account owned by the never-signing PDA (L4 revised)"
    assert d[32:64] != bytes(INCINERATOR)
    assert w.supply() == 0
    return f"cap 50,000 units · no freeze authority · extensions {sorted(exts)} only · CU {r.cu:,}"


@test
def H2_seed_puts_tokens_in_and_25_units_in_the_dead_units_pda():
    w = World()
    assert w.send([w.ix_initialize(ADMIN)], [ADMIN]).ok
    w.mint_legs(ADMIN, SEED_AMOUNTS)
    r = w.send([w.ix_seed(ADMIN, SEED_AMOUNTS)], [ADMIN])
    assert r.ok, r.reason()
    NOTES["cu_seed"] = r.cu
    assert w.holdings() == SEED_AMOUNTS
    assert w.supply() == SEED_UNITS and w.balance(DEAD_UNITS) == SEED_UNITS
    assert legs_of(w, ADMIN) == [0, 0, 0, 0]
    return f"holdings {w.holdings()} · supply 25 units, all held by the dead-units PDA · CU {r.cu:,}"


@test
def H3_deposit_in_exact_ratio_takes_everything_offered():
    w = World().launch()
    u = funded_user(w, "alice")
    h, s = w.holdings(), w.supply()
    offer = [x * 4 for x in h]                       # exactly 4× the vault, same ratio
    exp_units, exp_req = ref_deposit(h, s, offer)
    before = legs_of(w, u)
    r = w.send([w.ix_deposit(u, offer)], [u])
    assert r.ok, r.reason()
    NOTES["cu_deposit"] = r.cu
    got_req = [b - a for b, a in zip(before, legs_of(w, u))]
    assert units_of(w, u) == exp_units and got_req == exp_req
    assert got_req == offer, "exact ratio: nothing left behind"
    return f"minted {exp_units:,} (= 4 × 25 units) · took exactly what was offered · CU {r.cu:,}"


@test
def H4_lopsided_deposit_leaves_the_excess_in_the_wallet():
    w = World().launch()
    u = funded_user(w, "alice")
    h, s = w.holdings(), w.supply()
    offer = [h[0] * 10, h[1] * 2, h[2] * 10, h[3] * 10]   # leg 1 is the binding one
    exp_units, exp_req = ref_deposit(h, s, offer)
    before = legs_of(w, u)
    r = w.send([w.ix_deposit(u, offer)], [u])
    assert r.ok, r.reason()
    taken = [b - a for b, a in zip(before, legs_of(w, u))]
    assert taken == exp_req and units_of(w, u) == exp_units
    assert all(t <= o for t, o in zip(taken, offer))
    left = [o - t for o, t in zip(offer, taken)]
    assert left[1] <= 1 and min(left[0], left[2], left[3]) > 0
    return f"offered {offer} · took {taken} · excess never left the wallet"


@test
def H5_redeem_partial_then_full_matches_the_reference():
    w = World().launch()
    u = funded_user(w, "alice")
    assert w.send([w.ix_deposit(u, [10**7] * 4)], [u]).ok
    for step in ("third", "rest"):
        part = units_of(w, u) // 3 if step == "third" else units_of(w, u)
        h, s = w.holdings(), w.supply()
        exp = ref_redeem(h, s, part)
        before = legs_of(w, u)
        r = w.send([w.ix_redeem(u, part)], [u])
        assert r.ok, r.reason()
        NOTES["cu_redeem"] = r.cu
        got = [a - b for a, b in zip(legs_of(w, u), before)]
        assert got == exp, f"payouts {got} != reference {exp}"
    assert units_of(w, u) == 0 and w.supply() == SEED_UNITS and w.balance(DEAD_UNITS) == SEED_UNITS
    return f"partial and full redemption match the reference · supply back to 25 dead units · CU {r.cu:,}"


@test
def H6_cap_refuses_the_deposit_that_would_cross_it_and_allows_one_that_fits():
    cap = SEED_UNITS + 100 * UNIT                     # room for exactly 100 units
    w = World().launch(cap=cap)
    u = funded_user(w, "alice")
    h = w.holdings()
    too_big = [x * 5 for x in h]                      # would mint 125 units
    r = w.send([w.ix_deposit(u, too_big)], [u])
    must_fail(r, code=ERR["CapExceeded"], why="125 units > 100 of room")
    fits = [x * 4 for x in h]                         # mints exactly 100 units → supply == cap
    r = w.send([w.ix_deposit(u, fits)], [u])
    assert r.ok, r.reason()
    assert w.supply() == cap
    r = w.send([w.ix_deposit(u, [x // 10 for x in h])], [u])
    must_fail(r, code=ERR["CapExceeded"], why="vault exactly full")
    return "125 units refused · exactly 100 accepted (supply == cap) · next deposit refused"


@test
def H7_set_cap_by_admin_unblocks_deposits():
    w = World().launch(cap=SEED_UNITS)               # full from the start
    u = funded_user(w, "alice")
    must_fail(w.send([w.ix_deposit(u, [10**6] * 4)], [u]), code=ERR["CapExceeded"])
    r = w.send([w.ix_set_cap(ADMIN, CAP_50K)], [ADMIN])
    assert r.ok, r.reason()
    NOTES["cu_set_cap"] = r.cu
    assert w.vault_cap() == CAP_50K
    assert w.send([w.ix_deposit(u, [10**6] * 4)], [u]).ok
    return f"refused at cap 25 → admin raised to 50,000 → accepted · CU {r.cu:,}"


@test
def H8_redemption_ignores_the_cap_entirely():
    w = World().launch()
    u = funded_user(w, "alice")
    assert w.send([w.ix_deposit(u, [10**7] * 4)], [u]).ok
    assert w.send([w.ix_set_cap(ADMIN, 0)], [ADMIN]).ok       # cap far BELOW supply
    must_fail(w.send([w.ix_deposit(u, [10**6] * 4)], [u]), code=ERR["CapExceeded"])
    r = w.send([w.ix_redeem(u, units_of(w, u))], [u])
    assert r.ok, "a cap below supply must never trap anyone: " + r.reason()
    return "cap set to 0, below supply: deposits refused, redemption still works"


# ===================================================== A — adversarial

@test
def A01_fake_mint_in_place_of_a_real_leg():
    w = World().launch()
    u = funded_user(w, "alice")
    # The strongest impostor: an exact byte-for-byte copy of the real NVDAx mint,
    # initialised and owned by Token-2022, sitting at a different address.
    fake = det_keypair("fake-mint").pubkey()
    real = w.svm.get_account(LEG_MINTS[0])
    w.svm.set_account(fake, Account(real.lamports, bytes(real.data), TOKEN_2022, False))
    r = w.send([w.ix_deposit(u, [10**6] * 4, override={IDX["mint0"]: fake})], [u])
    return must_fail(r, why="byte-identical copy of NVDAx at another address")


@test
def A02_legs_passed_in_the_wrong_order():
    w = World().launch()
    u = funded_user(w, "alice")
    o = {IDX["mint0"]: LEG_MINTS[1], IDX["mint1"]: LEG_MINTS[0],
         IDX["vault_leg0"]: VAULT_LEGS[1], IDX["vault_leg1"]: VAULT_LEGS[0],
         IDX["user_leg0"]: ata(u.pubkey(), LEG_MINTS[1]), IDX["user_leg1"]: ata(u.pubkey(), LEG_MINTS[0])}
    return must_fail(w.send([w.ix_deposit(u, [10**6] * 4, override=o)], [u]), why="order is fixed")


@test
def A03_attacker_token_account_passed_as_a_vault_account():
    w = World().launch()
    u = funded_user(w, "alice")
    mallory = funded_user(w, "mallory")
    evil = ata(mallory.pubkey(), LEG_MINTS[0])
    r = w.send([w.ix_redeem(u, 10**6, override={IDX["vault_leg0"]: evil})], [u])
    must_fail(r, why="vault leg must be the vault's own ATA")
    r = w.send([w.ix_deposit(u, [10**6] * 4, override={IDX["vault_leg0"]: evil})], [u])
    return must_fail(r, why="vault leg must be the vault's own ATA")


@test
def A04_fake_token_program():
    w = World().launch()
    u = funded_user(w, "alice")
    classic = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    for impostor in (classic, ATA_PROGRAM, SYSTEM):
        must_fail(w.send([w.ix_deposit(u, [10**6] * 4, override={IDX["token_program"]: impostor})], [u]),
                  why="token program pinned to Token-2022")
    return "classic Token, ATA program and System all refused in the token-program slot"


@test
def A05_same_account_in_two_slots():
    w = World().launch()
    u = funded_user(w, "alice")
    a0 = ata(u.pubkey(), LEG_MINTS[0])
    must_fail(w.send([w.ix_deposit(u, [10**6] * 4, override={IDX["user_leg1"]: a0})], [u]))
    return must_fail(w.send([w.ix_deposit(u, [10**6] * 4, override={IDX["vault_leg1"]: VAULT_LEGS[0]})], [u]))


@test
def A06_set_cap_by_anyone_but_the_upgrade_authority():
    w = World().launch()
    mallory = w.new_user("mallory")
    r = w.send([w.ix_set_cap(mallory, 10**18)], [mallory])
    reason = must_fail(r, code=ERR["Unauthorized"])
    assert w.vault_cap() == CAP_50K
    return reason


@test
def A07_initialize_by_anyone_but_the_upgrade_authority():
    w = World()
    mallory = w.new_user("mallory", sol=50)
    return must_fail(w.send([w.ix_initialize(mallory)], [mallory]), code=ERR["Unauthorized"])


@test
def A08_seed_by_anyone_but_the_upgrade_authority():
    w = World()
    assert w.send([w.ix_initialize(ADMIN)], [ADMIN]).ok
    mallory = w.new_user("mallory")
    w.mint_legs(mallory, SEED_AMOUNTS)
    return must_fail(w.send([w.ix_seed(mallory, SEED_AMOUNTS)], [mallory]), code=ERR["Unauthorized"])


@test
def A09_initialize_twice():
    w = World().launch()
    return must_fail(w.send([w.ix_initialize(ADMIN)], [ADMIN]), why="vault already exists")


@test
def A10_seed_twice():
    w = World().launch()
    w.mint_legs(ADMIN, SEED_AMOUNTS)
    return must_fail(w.send([w.ix_seed(ADMIN, SEED_AMOUNTS)], [ADMIN]), code=ERR["AlreadySeeded"])


@test
def A11_deposit_before_seed():
    w = World()
    assert w.send([w.ix_initialize(ADMIN)], [ADMIN]).ok
    u = funded_user(w, "alice")
    return must_fail(w.send([w.ix_deposit(u, [10**6] * 4)], [u]))


@test
def A12_redeem_more_units_than_held():
    w = World().launch()
    u = funded_user(w, "alice")
    assert w.send([w.ix_deposit(u, [10**7] * 4)], [u]).ok
    return must_fail(w.send([w.ix_redeem(u, units_of(w, u) + 1)], [u]), why="can't burn what you don't hold")


@test
def A13_deposit_slippage_limit():
    w = World().launch()
    u = funded_user(w, "alice")
    exp, _ = ref_deposit(w.holdings(), w.supply(), [10**7] * 4)
    before = snapshot(w, u)
    must_fail(w.send([w.ix_deposit(u, [10**7] * 4, exp + 1)], [u]), code=ERR["SlippageUnits"])
    assert snapshot(w, u) == before, "a refused deposit must move nothing"
    assert w.send([w.ix_deposit(u, [10**7] * 4, exp)], [u]).ok
    return "min_units_out one above the outcome: refused, nothing moved; at the outcome: accepted"


@test
def A14_redeem_slippage_limit():
    w = World().launch()
    u = funded_user(w, "alice")
    assert w.send([w.ix_deposit(u, [10**7] * 4)], [u]).ok
    n = units_of(w, u)
    exp = ref_redeem(w.holdings(), w.supply(), n)
    before = snapshot(w, u)
    must_fail(w.send([w.ix_redeem(u, n, [exp[0], exp[1], exp[2] + 1, exp[3]])], [u]), code=ERR["SlippageLeg"])
    assert snapshot(w, u) == before, "a refused redemption must move nothing"
    return "min_out one above on a single leg: refused, nothing moved"


@test
def A15_dust_deposit_mints_nothing():
    w = World().launch()
    u = funded_user(w, "alice")
    before = snapshot(w, u)
    reason = must_fail(w.send([w.ix_deposit(u, [1, 1, 1, 1])], [u]), code=ERR["CoreDust"])
    assert snapshot(w, u) == before
    return reason


@test
def A16_vault_owned_account_as_a_redemption_destination():
    w = World().launch()
    u = funded_user(w, "alice")
    assert w.send([w.ix_deposit(u, [10**7] * 4)], [u]).ok
    return must_fail(w.send([w.ix_redeem(u, 10**6, override={IDX["user_leg2"]: VAULT_LEGS[2]})], [u]),
                     code=ERR["BadAccount"])


@test
def A17_attacker_brings_their_own_program_data():
    """The realistic version: the attacker deploys their OWN program, so they hold a
    genuine ProgramData naming themselves as upgrade authority, and passes it in."""
    w = World().launch()
    mallory = w.new_user("mallory")
    their_program = det_keypair("mallory-program").pubkey()
    their_pd = pda([bytes(their_program)], LOADER_V3)
    data = struct.pack("<IQ", 3, 0) + b"\x01" + bytes(mallory.pubkey()) + b"\x00" * 64
    w.svm.set_account(their_pd, Account(10**9, data, LOADER_V3, False))
    ix = w.ix_set_cap(mallory, 10**18, override={2: their_pd})
    must_fail(w.send([ix], [mallory]), code=ERR["Unauthorized"], why="ProgramData must be OUR program's")
    ix = w.ix_set_cap(mallory, 10**18, override={1: their_program, 2: their_pd})
    must_fail(w.send([ix], [mallory]), why="program slot must be our program")
    assert w.vault_cap() == CAP_50K
    return "foreign ProgramData refused · foreign program refused · cap unchanged"


@test
def A18_burning_someone_elses_units():
    w = World().launch()
    mallory = funded_user(w, "mallory")
    r = w.send([w.ix_redeem(mallory, SEED_UNITS, override={IDX["user_unit"]: DEAD_UNITS})], [mallory])
    must_fail(r, why="only the owner can burn units")
    assert w.balance(DEAD_UNITS) == SEED_UNITS
    return "redeeming the dead-units PDA's 25 units through the program: refused — supply can never reach zero"


@test
def A19_upgrade_authority_burnt_means_admin_powers_end():
    w = World(upgrade_authority=ADMIN.pubkey()).launch()
    data = bytearray(w.account_data(PROGRAM_DATA))
    data[12] = 0                                    # Option<Pubkey> → None: authority burnt
    data[13:45] = b"\x00" * 32
    w.svm.set_account(PROGRAM_DATA, Account(10**9, bytes(data), LOADER_V3, False))
    must_fail(w.send([w.ix_set_cap(ADMIN, 1)], [ADMIN]), code=ERR["Unauthorized"])
    return "with upgrade authority burnt, even the former admin can't change the cap"


@test
def A20_nobody_can_burn_the_seed_units_directly():
    """Regression for the bug A18 found on 22 Nov 2026. Token-2022 lets ANYONE burn
    from an account owned by the incinerator or the system program. The seed units
    must not be held by either: a raw Token-2022 burn, bypassing our program, by a
    stranger, must fail — and supply must stay above zero."""
    w = World().launch()
    stranger = w.new_user("total-stranger")
    burn = Instruction(TOKEN_2022, bytes([15]) + struct.pack("<QB", SEED_UNITS, 9), [
        AccountMeta(DEAD_UNITS, False, True),
        AccountMeta(UNIT_MINT, False, True),
        AccountMeta(stranger.pubkey(), True, False),
    ])
    must_fail(w.send([burn], [stranger]), why="seed units must not be permissionlessly burnable")
    xfer = w.transfer_checked_ix(DEAD_UNITS, UNIT_MINT, ata(stranger.pubkey(), UNIT_MINT),
                                 stranger.pubkey(), SEED_UNITS, decimals=9)
    w.ensure_unit_ata(stranger)
    must_fail(w.send([xfer], [stranger]), why="seed units must not be transferable")
    assert w.balance(DEAD_UNITS) == SEED_UNITS and w.supply() == SEED_UNITS
    return "raw Token-2022 burn and transfer of the seed units by a stranger: both refused · supply intact"


# =========================================== E — economics and issuer powers

@test
def E1_donation_cannot_be_used_against_the_next_depositor():
    w = World().launch()
    mallory = funded_user(w, "mallory", amounts=(10**10,) * 4)
    bob = funded_user(w, "bob")
    assert w.send([w.ix_deposit(mallory, [10**6] * 4)], [mallory]).ok
    gift = [10**9] * 4                              # donate straight into the vault accounts
    ixs = [w.transfer_checked_ix(ata(mallory.pubkey(), m), m, v, mallory.pubkey(), g)
           for m, v, g in zip(LEG_MINTS, VAULT_LEGS, gift)]
    assert w.send(ixs, [mallory]).ok
    b0 = legs_of(w, bob)
    assert w.send([w.ix_deposit(bob, [10**8] * 4)], [bob]).ok
    paid = [x - y for x, y in zip(b0, legs_of(w, bob))]
    assert w.send([w.ix_redeem(bob, units_of(w, bob))], [bob]).ok
    back = [y - x for x, y in zip([b - p for b, p in zip(b0, paid)], legs_of(w, bob))]
    loss = [p - k for p, k in zip(paid, back)]
    assert all(0 <= l <= 2 for l in loss), f"bob lost more than rounding: {loss}"
    return f"after a 10-token-per-leg donation, bob's round trip lost {loss} base units — rounding only"


@test
def E2_issuer_drain_is_shared_pro_rata_and_an_emptied_leg_stops_deposits():
    w = World().launch()
    alice = funded_user(w, "alice")
    bob = funded_user(w, "bob")
    assert w.send([w.ix_deposit(alice, [10**8] * 4)], [alice]).ok
    half = w.holdings()[2] // 2
    assert w.issuer_drain(2, half, ISSUER.pubkey()).ok, "permanent delegate drains half of MSFTx"
    assert w.send([w.ix_deposit(bob, [10**7] * 4)], [bob]).ok, "L1: deposits continue after a partial drain"
    exp = ref_redeem(w.holdings(), w.supply(), units_of(w, alice) // 2)
    before = legs_of(w, alice)
    assert w.send([w.ix_redeem(alice, units_of(w, alice) // 2)], [alice]).ok
    got = [a - b for a, b in zip(legs_of(w, alice), before)]
    assert got == exp, "redemption pays the post-drain pro-rata share"
    assert w.issuer_drain(2, w.holdings()[2], ISSUER.pubkey()).ok, "drain MSFTx to zero"
    assert w.holdings()[2] == 0
    must_fail(w.send([w.ix_deposit(bob, [10**7] * 4)], [bob]), code=ERR["CoreEmptyLeg"], why="D1")
    before = legs_of(w, alice)
    r = w.send([w.ix_redeem(alice, units_of(w, alice))], [alice])
    assert r.ok, "L1: redemptions continue with a leg at zero: " + r.reason()
    got = [a - b for a, b in zip(legs_of(w, alice), before)]
    assert got[2] == 0 and min(got[0], got[1], got[3]) > 0
    return "half-drain: shared pro-rata, all operations continue · full drain: deposits refused (D1), redemptions still pay the other three legs"


@test
def E3_issuer_pause_stops_everything_and_moves_nothing():
    w = World().launch()
    alice = funded_user(w, "alice")
    assert w.send([w.ix_deposit(alice, [10**7] * 4)], [alice]).ok
    r = w.issuer_pause(0, True)
    assert r.ok, "pause NVDAx: " + r.reason()
    before = snapshot(w, alice)
    must_fail(w.send([w.ix_redeem(alice, units_of(w, alice))], [alice]), why="L8: all-or-nothing")
    must_fail(w.send([w.ix_deposit(alice, [10**6] * 4)], [alice]))
    assert snapshot(w, alice) == before, "nothing moved while paused"
    assert w.issuer_pause(0, False).ok
    assert w.send([w.ix_redeem(alice, units_of(w, alice))], [alice]).ok, "resumes cleanly"
    return "one leg paused: redemptions and deposits refused, nothing moved (L8) · resume: works again"


@test
def E4_issuer_freeze_of_a_vault_account():
    w = World().launch()
    alice = funded_user(w, "alice")
    assert w.send([w.ix_deposit(alice, [10**7] * 4)], [alice]).ok
    assert w.issuer_freeze(VAULT_LEGS[1], 1, True).ok, "freeze the vault's GOOGLx account"
    before = snapshot(w, alice)
    must_fail(w.send([w.ix_redeem(alice, units_of(w, alice))], [alice]))
    must_fail(w.send([w.ix_deposit(alice, [10**6] * 4)], [alice]))
    assert snapshot(w, alice) == before
    assert w.issuer_freeze(VAULT_LEGS[1], 1, False).ok
    assert w.send([w.ix_redeem(alice, units_of(w, alice))], [alice]).ok
    return "vault's GOOGLx account frozen: everything refused, nothing moved · thawed: works"


@test
def E5_dividend_multiplier_changes_nothing_in_deposit_or_redeem():
    """The D2 argument, tested: a per-leg multiplier cancels out of every ratio
    deposit and redeem compute, because they read raw amounts."""
    results = []
    start = None
    for mult in (None, 2.5):
        w = World().launch()
        if start is None:
            from replica import tlv
            start = struct.unpack_from("<d", dict(tlv(w.account_data(LEG_MINTS[0])))[25], 32)[0]
        if mult is not None:
            before = w.account_data(LEG_MINTS[0])
            r = w.issuer_set_multiplier(0, mult, 0)
            assert r.ok, "update multiplier: " + r.reason()
            assert w.account_data(LEG_MINTS[0]) != before, "multiplier really changed"
        alice = funded_user(w, "alice")
        assert w.send([w.ix_deposit(alice, [3 * 10**7, 10**7, 10**7, 10**7])], [alice]).ok
        u = units_of(w, alice)
        b = legs_of(w, alice)
        assert w.send([w.ix_redeem(alice, u // 2)], [alice]).ok
        results.append((u, [a - x for a, x in zip(legs_of(w, alice), b)]))
    assert results[0] == results[1], f"multiplier changed the outcome: {results}"
    return f"NVDAx multiplier {start:.4f} → 2.5: deposit and redeem outputs identical ({results[0][0]:,} units)"


# ============================================ D — differential vs the reference

@test
def D1_random_sequences_match_the_python_reference_exactly():
    rng = random.Random(20261122)
    total_ops = 0
    for world_n in range(4):
        w = World().launch()
        users = [funded_user(w, f"d{world_n}-{i}", amounts=(10**11,) * 4) for i in range(3)]
        for _ in range(45):
            u = rng.choice(users)
            h, s = w.holdings(), w.supply()
            if rng.random() < 0.6 or units_of(w, u) == 0:
                scale = 10 ** rng.randint(-2, 2)
                d = [max(0, int(x * scale * rng.uniform(0.5, 2.0))) for x in h]
                try:
                    exp = ref_deposit(h, s, d)
                except Exception:
                    exp = None
                if exp and s + exp[0] > w.vault_cap():
                    exp = "cap"
                before = legs_of(w, u)
                u0 = units_of(w, u)
                r = w.send([w.ix_deposit(u, d)], [u])
                if exp in (None, "cap"):
                    assert not r.ok, f"reference refused but chain accepted: {d}"
                else:
                    assert r.ok, f"chain refused a deposit the reference accepts: {r.reason()}"
                    assert units_of(w, u) - u0 == exp[0]
                    assert [b - a for b, a in zip(before, legs_of(w, u))] == exp[1]
            else:
                n = max(1, int(units_of(w, u) * rng.choice([0.01, 0.3, 0.5, 1.0])))
                exp = ref_redeem(h, s, n)
                before = legs_of(w, u)
                r = w.send([w.ix_redeem(u, n)], [u])
                assert r.ok, r.reason()
                assert [a - b for a, b in zip(legs_of(w, u), before)] == exp
            total_ops += 1
            assert w.supply() >= SEED_UNITS and w.balance(DEAD_UNITS) == SEED_UNITS
    return f"{total_ops} random deposits and redemptions across 4 chains: every number identical to permafrost_reference.py"


# ============================================================ M — measurements

@test
def M1_transaction_sizes():
    from solders.compute_budget import set_compute_unit_limit as cul
    from solders.message import Message
    from solders.transaction import Transaction
    w = World().launch()
    u = funded_user(w, "alice")
    relayer = w.new_user("relayer")
    sizes = {}

    def size(ixs, signers):
        tx = Transaction(signers, Message(ixs, signers[0].pubkey()), w.svm.latest_blockhash())
        return len(bytes(tx))

    sizes["deposit, user pays"] = size([cul(200_000), w.ix_deposit(u, [10**6] * 4)], [u])
    sizes["deposit, relayer pays + creates unit account"] = size(
        [cul(200_000), w.create_ata_ix(relayer.pubkey(), u.pubkey(), UNIT_MINT), w.ix_deposit(u, [10**6] * 4)],
        [relayer, u])
    sizes["redeem, relayer pays + creates 4 destination accounts"] = size(
        [cul(200_000), *[w.create_ata_ix(relayer.pubkey(), u.pubkey(), m) for m in LEG_MINTS],
         w.ix_redeem(u, 10**6)], [relayer, u])
    sizes["initialize"] = size([cul(400_000), w.ix_initialize(ADMIN)], [ADMIN])
    sizes["seed"] = size([cul(200_000), w.ix_seed(ADMIN, SEED_AMOUNTS)], [ADMIN])
    NOTES["tx_sizes"] = sizes
    worst = max(sizes.values())
    assert worst <= 1232, f"a transaction exceeds 1,232 bytes: {sizes}"
    return " · ".join(f"{k}: {v}" for k, v in sizes.items()) + " (limit 1,232)"


@test
def M2_deploy_cost():
    w = World()
    elf = PROGRAM_SO.stat().st_size
    one = w.svm.minimum_balance_for_rent_exemption(45 + elf)
    two = w.svm.minimum_balance_for_rent_exemption(45 + 2 * elf)
    NOTES["program_bytes"] = elf
    NOTES["deploy_sol_1x"] = one / 1e9
    NOTES["deploy_sol_2x"] = two / 1e9
    return f"program {elf:,} bytes · ProgramData rent {one/1e9:.3f} SOL (room for same size) / {two/1e9:.3f} SOL (room to double) · the upload buffer is drained back to the payer inside the deploy, so peak need ≈ ProgramData rent + fees (measured: a 3.25 SOL wallet deployed it with 0.07 left)"


# ================================================================== runner

def main():
    t0 = time.time()
    passed = failed = 0
    for fn in TESTS:
        name = fn.__name__
        try:
            detail = fn()
            passed += 1
            print(f"PASS  {name}\n      {detail}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}\n      {type(e).__name__}: {e}")
            traceback.print_exc(limit=2)
    print(f"\n{passed} passed, {failed} failed, {time.time() - t0:.1f}s")
    cu = {k[3:]: v for k, v in NOTES.items() if k.startswith("cu_")}
    if cu:
        print("compute units:", ", ".join(f"{k} {v:,}" for k, v in cu.items()))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
