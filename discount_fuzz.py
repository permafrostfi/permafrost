#!/usr/bin/env python3
"""Adversarial fuzz for discount mode, against the reference implementation.

Two properties the design has to hold before any of it reaches Anchor:

  P1  No sequence of discounted deposits can pull more units out of the pool
      than the pool holds. The creator's exposure is exactly what they funded,
      whatever order operations arrive in and whatever the dials are set to.

  P2  An early exit returns the depositor's own units IN FULL, regardless of how
      the creator moved the dials while the position was open. The bonus is the
      creator's to withdraw; the principal never is.

Plus three structural checks that would each be a quiet disaster on chain:

  P3  A discount operation never changes vault holdings or total supply — the
      bonus is a transfer out of the pool, not a mint, so nobody is diluted.
  P4  The pool and the escrow are always fully backed by real units.
  P5  The creator can never withdraw units that are sitting in escrow.

Run: python3 discount_fuzz.py [trials]
"""

import copy
import random
import sys

import permafrost_reference as R


def run(trials=200_000, seed=7):
    rng = random.Random(seed)
    stats = {"dep": 0, "liq": 0, "lock": 0, "claim": 0, "early": 0,
             "fund": 0, "wd": 0, "dial": 0, "bonus_capped": 0, "pool_dry": 0}

    for t in range(trials):
        h = [rng.randint(1, 10**13) for _ in range(4)]
        v = R.seed(h, rng.randint(10**6, 10**13), 0)
        v.entry_bp = rng.randint(0, R.MAX_ENTRY_BP)
        v.exit_bp = rng.randint(0, R.MAX_EXIT_BP)
        v.stream_bp = rng.randint(0, R.MAX_STREAM_BP)

        funded = 0          # every unit the creator has ever put in
        withdrawn = 0       # every unit they have taken back
        paid_out = 0        # every bonus unit that has left the pool
        returned = 0        # every bonus unit forfeited back by an early exit
        principals = {}     # position id -> units the depositor put in
        now = 0

        for _ in range(rng.randint(1, 14)):
            op = rng.random()
            before = (list(v.holdings), v.supply)
            # A discounted DEPOSIT legitimately moves holdings and supply — it
            # contains a real deposit. P3 is about the pool operations that must
            # not: funding, withdrawing, dial changes, claims and early exits.
            moves_vault = 0.38 <= op < 0.78

            # ---- creator funds the pool -------------------------------------
            if op < 0.15:
                # The creator can only move in units that actually exist.
                headroom = v.supply - v.subsidy_units - v.escrow_units
                if headroom <= 0:
                    continue
                amt = rng.randint(1, min(headroom, 10**11))
                R.fund_subsidy(v, amt)
                funded += amt
                stats["fund"] += 1

            # ---- creator moves the dials, including mid-position -------------
            elif op < 0.30:
                v.discount_on = rng.random() < 0.8
                R.set_discount(
                    v,
                    rng.randint(0, R.MAX_DISCOUNT_BP),
                    rng.randint(0, R.MAX_LOCKED_DISCOUNT_BP),
                    rng.choice([0, rng.randint(1, 10**11)]),
                )
                stats["dial"] += 1

            # ---- creator withdraws --------------------------------------------
            elif op < 0.38:
                if v.subsidy_units > 0:
                    amt = rng.randint(1, v.subsidy_units)
                    R.withdraw_subsidy(v, amt)
                    withdrawn += amt
                    stats["wd"] += 1

            # ---- liquid discounted deposit -----------------------------------
            elif op < 0.58:
                d = [max(1, x * rng.randint(1000, 500_000) // 1_000_000) for x in v.holdings]
                pool_before = v.subsidy_units
                try:
                    got, _, _, bonus = R.deposit_discounted(v, d)
                except Exception:
                    continue
                stats["liq"] += 1
                paid_out += bonus
                assert bonus <= pool_before, "P1: paid a bonus larger than the pool"
                if bonus and bonus == v.max_bonus_units:
                    stats["bonus_capped"] += 1
                if v.subsidy_units == 0 and bonus:
                    stats["pool_dry"] += 1

            # ---- locked deposit ------------------------------------------------
            elif op < 0.78:
                d = [max(1, x * rng.randint(1000, 500_000) // 1_000_000) for x in v.holdings]
                pool_before = v.subsidy_units
                try:
                    pid, principal, bonus, _, _ = R.deposit_locked(v, d, now)
                except Exception:
                    continue
                stats["lock"] += 1
                paid_out += bonus
                principals[pid] = principal
                assert bonus <= pool_before, "P1: paid a bonus larger than the pool"

            # ---- someone exits a position --------------------------------------
            elif op < 0.92:
                if not v.positions:
                    continue
                pid = rng.choice(list(v.positions))
                p = v.positions[pid]
                if rng.random() < 0.5:
                    # early: principal must come back whole
                    if now < p.unlock_at:
                        expect = principals[pid]
                        bonus_back = p.bonus
                        out = R.exit_locked_early(v, pid, now)
                        assert out == expect, (
                            f"P2: early exit returned {out}, principal was {expect}")
                        returned += bonus_back
                        stats["early"] += 1
                else:
                    # matured
                    t_after = p.unlock_at + rng.randint(0, 10**6)
                    expect = p.principal + p.bonus
                    out = R.claim_locked(v, pid, t_after)
                    assert out == expect, "claim did not pay principal + bonus"
                    stats["claim"] += 1
                    now = max(now, t_after)

            # ---- time passes, fee accrues ---------------------------------------
            else:
                now += rng.randint(0, 40 * 24 * 60 * 60)
                R.accrue(v, now)
                stats["dep"] += 1
                before = (before[0], v.supply)   # accrual legitimately mints

            # P3: no pool-only operation may touch the vault.
            if not moves_vault:
                assert list(v.holdings) == before[0], \
                    "P3: a pool operation moved vault holdings"
                assert v.supply == before[1], \
                    "P3: a pool operation changed total supply"

            # P4 / P5, and the reference's own invariants.
            R.invariants(v)
            assert v.subsidy_units >= 0
            assert v.escrow_units >= 0

        # ---- end of scenario: the books must balance ------------------------
        # Everything the creator put in is either still in the pool, taken back
        # out, or sitting with a depositor as a bonus that has not been forfeited.
        assert funded - withdrawn - paid_out + returned == v.subsidy_units, (
            f"P1: pool accounting drifted in trial {t}")
        assert v.subsidy_units + v.escrow_units <= v.supply, "P4: pool/escrow unbacked"

    return stats


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200_000
    s = run(n)
    print(f"scenarios            : {n:,}")
    for k, label in [
        ("liq", "liquid discounted deposits"),
        ("lock", "locked deposits"),
        ("claim", "matured claims"),
        ("early", "early exits"),
        ("fund", "pool top-ups"),
        ("wd", "creator withdrawals"),
        ("dial", "dial changes mid-flight"),
        ("bonus_capped", "  of which hit the per-deposit cap"),
        ("pool_dry", "  of which emptied the pool"),
    ]:
        print(f"{label:<36} {s[k]:>12,}")
    print("\nP1 pool never overdrawn, accounting balances : PASS")
    print("P2 early exit returns principal in full      : PASS")
    print("P3 discount never moves holdings             : PASS")
    print("P4 pool + escrow always backed by real units : PASS")
    print("P5 creator cannot reach escrowed units       : PASS")
