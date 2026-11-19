#!/usr/bin/env python3
"""Generate the discount-mode test vectors from the reference implementation.

Deliberately a SEPARATE file from `gen_vectors.py` / `permafrost_test_vectors.json`.
Those two are frozen — they are the V1..V16 chain the Rust core is already
asserted against, and editing them to make room for a new feature is exactly the
move that turns a test suite into a rubber stamp. Discount mode gets its own
chain, its own JSON and its own Rust test file.

Five cases, one per thing that can go wrong:

  D1  liquid path, the RATE binds
  D2  locked path held to maturity — claim pays principal + bonus
  D3  locked path exited early — principal back IN FULL, bonus back to the pool
  D4  the POOL runs dry mid-deposit — the bonus degrades, the deposit still works
  D5  the per-deposit CAP binds — one whale cannot take the whole pool
  D6  the dials move while a position is open — the position is unaffected

Run: python3 gen_discount_vectors.py
"""

import copy
import json

from permafrost_reference import (
    seed, deposit, deposit_discounted, deposit_locked, claim_locked,
    exit_locked_early, fund_subsidy, set_discount, invariants,
    UNIT, TOK, LOCK_PERIOD,
)

USDC = 10**6
PRICES = [220 * USDC, 340 * USDC, 510 * USDC, 575 * USDC]
NOW = 1_767_225_600            # the same launch instant gen_vectors.py uses
ENTRY_BP = 30                  # 0.3%, so the entry fee is live in every case

vectors = []


def rec(name, given, expect):
    vectors.append({"case": name, "given": given, "expect": expect})
    print(f"\n### {name}")
    for k, v in given.items():
        print(f"  given  {k}: {v}")
    for k, v in expect.items():
        print(f"  expect {k}: {v}")


def buy_ratio(v, budget_usdc):
    """Delivered amounts that cost `budget_usdc` at PRICES, in the vault's ratio."""
    total = sum((h * p) // TOK for h, p in zip(v.holdings, PRICES))
    k = (budget_usdc * 10**18) // total
    return [(h * k) // 10**18 for h in v.holdings]


# ------------------------------------------------------------------- D0 base
# One seeded vault plus one ordinary deposit, so the creator has units to fund
# the pool with and the supply is a realistic size. Every case below forks from
# this exact state.
SEED_USD = 25
per_leg = (SEED_USD * USDC) // 4
base = seed([(per_leg * TOK) // p for p in PRICES], 25 * UNIT, NOW)
base.entry_bp = ENTRY_BP
deposit(base, buy_ratio(base, 2_000 * USDC))
invariants(base)

POOL = 100 * UNIT              # 100 units parked in the subsidy pool

rec("D0 — the shared starting state",
    {"seed_usd": SEED_USD, "prices": PRICES, "initial_units": 25 * UNIT,
     "entry_bp": ENTRY_BP, "then": "one ordinary $2,000 deposit"},
    {"holdings": base.holdings, "supply": base.supply,
     "creator_units": base.creator_units,
     "note": "every case below forks from exactly this vault"})


def fork(pool=POOL, on=True, dbp=0, lbp=0, cap=0):
    v = copy.deepcopy(base)
    fund_subsidy(v, pool)
    v.discount_on = on
    set_discount(v, dbp, lbp, cap)
    invariants(v)
    return v


# ------------------------------------------------------------- D1 rate binds
# The liquid path with room to spare in both the cap and the pool, so the RATE
# is the binding ceiling. 50bp is the middle of the intended operating range and
# half the immutable 100bp maximum.
v = fork(dbp=50, cap=10 * UNIT)
delivered = buy_ratio(v, 1_000 * USDC)
pool_before = v.subsidy_units
got, to_creator, refunds, bonus = deposit_discounted(v, delivered)
invariants(v)
minted = got - bonus

rec("D1 — liquid discounted deposit, the RATE is the binding ceiling",
    {"delivered": delivered, "discount_bp": 50, "max_bonus_units": 10 * UNIT,
     "subsidy_units_before": pool_before, "entry_bp": ENTRY_BP},
    {"minted_to_depositor": minted, "bonus": bonus, "to_depositor_total": got,
     "to_creator": to_creator, "refunds": refunds,
     "subsidy_units_after": v.subsidy_units, "supply": v.supply,
     "note": "bonus == minted * 50 / 10000, floored; supply unchanged by the bonus"})

# ----------------------------------------------------------- D2 to maturity
v = fork(lbp=2_500, cap=1_000 * UNIT)
delivered = buy_ratio(v, 200 * USDC)
pool_before = v.subsidy_units
pid, principal, bonus, to_creator, refunds = deposit_locked(v, delivered, NOW)
invariants(v)
escrow_after_open = v.escrow_units
claimed = claim_locked(v, pid, NOW + LOCK_PERIOD)
invariants(v)

rec("D2 — locked deposit held to MATURITY",
    {"delivered": delivered, "locked_discount_bp": 2_500,
     "max_bonus_units": 1_000 * UNIT, "subsidy_units_before": pool_before,
     "deposited_at": NOW, "claimed_at": NOW + LOCK_PERIOD},
    {"position_id": pid, "principal": principal, "bonus": bonus,
     "unlock_at": NOW + LOCK_PERIOD, "escrow_after_open": escrow_after_open,
     "claimed": claimed, "escrow_after_claim": v.escrow_units,
     "subsidy_units_after": v.subsidy_units, "open_positions": len(v.positions),
     "note": "claim pays principal + bonus; the pool does NOT get the bonus back"})

# ------------------------------------------------------------- D3 early exit
v = fork(lbp=2_500, cap=1_000 * UNIT)
delivered = buy_ratio(v, 200 * USDC)
pool_before = v.subsidy_units
pid, principal, bonus, to_creator, refunds = deposit_locked(v, delivered, NOW)
pool_after_open = v.subsidy_units
returned = exit_locked_early(v, pid, NOW + LOCK_PERIOD - 1)
invariants(v)

rec("D3 — locked deposit exited EARLY, one second before maturity",
    {"delivered": delivered, "locked_discount_bp": 2_500,
     "subsidy_units_before": pool_before, "deposited_at": NOW,
     "exited_at": NOW + LOCK_PERIOD - 1},
    {"principal": principal, "bonus": bonus,
     "subsidy_units_after_open": pool_after_open,
     "returned_to_depositor": returned,
     "subsidy_units_after_exit": v.subsidy_units,
     "escrow_after_exit": v.escrow_units, "open_positions": len(v.positions),
     "note": "principal back IN FULL, bonus back to the pool, no penalty"})

# --------------------------------------------------------- D4 pool runs dry
# The pool holds 3 units against a 100% rate on a ~199-unit deposit, so the POOL
# is the binding ceiling. The deposit must still succeed with a smaller bonus —
# a nearly-empty pool degrades, it does not brick deposits for everyone after it.
v = fork(pool=3 * UNIT, lbp=10_000, cap=1_000 * UNIT)
delivered = buy_ratio(v, 200 * USDC)
pool_before = v.subsidy_units
pid1, principal1, bonus1, _, _ = deposit_locked(v, delivered, NOW)
invariants(v)
pool_mid = v.subsidy_units
# A second identical deposit against an empty pool: bonus zero, deposit fine.
pid2, principal2, bonus2, _, _ = deposit_locked(v, delivered, NOW)
invariants(v)

rec("D4 — the POOL runs dry mid-deposit",
    {"subsidy_units_before": pool_before, "locked_discount_bp": 10_000,
     "max_bonus_units": 1_000 * UNIT, "delivered": delivered,
     "note_given": "the rate alone would pay ~199 units; the pool holds 3"},
    {"first_principal": principal1, "first_bonus": bonus1,
     "subsidy_units_after_first": pool_mid,
     "second_principal": principal2, "second_bonus": bonus2,
     "subsidy_units_after_second": v.subsidy_units,
     "escrow_units": v.escrow_units, "open_positions": len(v.positions),
     "note": "the pool is the binding ceiling; the next deposit still works, "
             "it just pays nothing"})

# ------------------------------------------------------------ D5 cap binds
# The per-deposit cap is what stops the first large deposit taking the whole
# pool. 100 units of pool at a 100% rate would be gone in one $200 deposit;
# a 7-unit cap makes it reach fourteen depositors instead of one.
v = fork(pool=POOL, lbp=10_000, cap=7 * UNIT)
delivered = buy_ratio(v, 200 * USDC)
pool_before = v.subsidy_units
pid, principal, bonus, _, _ = deposit_locked(v, delivered, NOW)
invariants(v)

rec("D5 — the per-deposit CAP binds",
    {"subsidy_units_before": pool_before, "locked_discount_bp": 10_000,
     "max_bonus_units": 7 * UNIT, "delivered": delivered},
    {"principal": principal, "bonus": bonus,
     "subsidy_units_after": v.subsidy_units, "escrow_units": v.escrow_units,
     "note": "without the cap this one deposit would have taken all 100 units"})

# ------------------------------------------------- D6 dials move mid-position
# The creator turns discount mode off entirely and zeroes every dial while a
# position is open. The position must be completely unaffected: the bonus is
# already escrowed, and exit_locked_early consults no dial.
v = fork(lbp=5_000, cap=1_000 * UNIT)
delivered = buy_ratio(v, 200 * USDC)
pid, principal, bonus, _, _ = deposit_locked(v, delivered, NOW)
escrow_at_open = v.escrow_units
pool_at_open = v.subsidy_units

v.discount_on = False
set_discount(v, 0, 0, 0)
invariants(v)
escrow_after_dials = v.escrow_units
position_after_dials = [v.positions[pid].principal, v.positions[pid].bonus,
                        v.positions[pid].unlock_at]

returned = exit_locked_early(v, pid, NOW + 1)
invariants(v)

rec("D6 — the DIALS MOVE while a position is open",
    {"locked_discount_bp_at_open": 5_000, "delivered": delivered,
     "then": "discount_on=False and set_discount(0, 0, 0)",
     "exited_at": NOW + 1},
    {"principal": principal, "bonus": bonus,
     "escrow_at_open": escrow_at_open, "escrow_after_dials": escrow_after_dials,
     "position_after_dials": position_after_dials,
     "returned_to_depositor": returned,
     "subsidy_units_at_open": pool_at_open,
     "subsidy_units_after_exit": v.subsidy_units,
     "note": "changing a dial never reaches an open position; the early exit "
             "still returns the principal in full"})

with open("permafrost_discount_vectors.json", "w") as f:
    json.dump(vectors, f, indent=2)
print(f"\nwrote permafrost_discount_vectors.json — {len(vectors)} cases")
