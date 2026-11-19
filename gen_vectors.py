#!/usr/bin/env python3
"""Generate the Permafrost test vectors from the reference implementation."""

import json
from permafrost_reference import (
    Vault, seed, deposit, redeem, accrue, rebalance_plan, apply_plan,
    weights_bp, invariants, unit_value, UNIT, TOK, BP, LEGS,
)

USDC = 10**6
P = {  # USDC base units per whole token
    "t0": [220 * USDC, 340 * USDC, 510 * USDC, 575 * USDC],
    "t1": [300 * USDC, 380 * USDC, 560 * USDC, 650 * USDC],
    "t2": [380 * USDC, 420 * USDC, 600 * USDC, 700 * USDC],
    "t3": [340 * USDC, 400 * USDC, 580 * USDC, 675 * USDC],
}

vectors = []


def rec(name, given, expect):
    vectors.append({"case": name, "given": given, "expect": expect})
    print(f"\n### {name}")
    for k, v in given.items():
        print(f"  given  {k}: {v}")
    for k, v in expect.items():
        print(f"  expect {k}: {v}")


def buy_ratio(v, budget_usdc, prices):
    """What the router aims for: k such that the basket costs `budget`."""
    total = sum((h * p) // TOK for h, p in zip(v.holdings, prices))
    k = (budget_usdc * 10**18) // total
    return [(h * k) // 10**18 for h in v.holdings]


# ---------------------------------------------------------------- V1 seed
SEED_USD = 25
per_leg = (SEED_USD * USDC) // 4
seed_tokens = [(per_leg * TOK) // p for p in P["t0"]]
INITIAL_UNITS = 25 * UNIT          # 1 unit = $1 at launch
v = seed(seed_tokens, INITIAL_UNITS, now := 1_767_225_600)
invariants(v)

rec("V1 — seed the empty vault",
    {"usd": SEED_USD, "split": "equal by dollars ($6.25 a leg)",
     "prices_t0": P["t0"], "initial_units": INITIAL_UNITS},
    {"holdings": v.holdings, "supply": v.supply,
     "dead_units": v.dead_units,
     "unit_value_usdc": round(unit_value(v, P["t0"]) / USDC, 9),
     "note": "seed units go to an UNSPENDABLE ADDRESS; supply must never be 0"})

# ------------------------------------------------------- V2 exact deposit
before_price = unit_value(v, P["t1"])
delivered = buy_ratio(v, 2000 * USDC, P["t1"])
u, c, refunds = deposit(v, delivered)
invariants(v)
rec("V2 — deposit $2,000 in exact ratio, 0 bp entry fee",
    {"holdings_before": [h - r for h, r in zip(v.holdings, [0, 0, 0, 0])],
     "delivered": delivered, "entry_bp": 0},
    {"units_to_depositor": u, "units_to_creator": c, "refunds": refunds,
     "supply": v.supply,
     "unit_value_before_usdc": round(before_price / USDC, 9),
     "unit_value_after_usdc": round(unit_value(v, P["t1"]) / USDC, 9),
     "invariant": "unit value must be IDENTICAL before and after"})

# --------------------------------------------- V3 imperfect ratio deposit
h_before = list(v.holdings)
sup_before = v.supply
skewed = buy_ratio(v, 500 * USDC, P["t2"])
skewed[1] = int(skewed[1] * 1.10)     # 10% too much Alphabet
skewed[3] = int(skewed[3] * 1.03)     # 3% too much Meta
u3, c3, ref3 = deposit(v, skewed)
invariants(v)
rec("V3 — deposit with two over-delivered legs; mint on the smallest ratio",
    {"holdings_before": h_before, "delivered": skewed, "entry_bp": 0},
    {"units_to_depositor": u3, "refunds": ref3,
     "note": "refund is non-zero only on the over-delivered legs",
     "supply": v.supply})

# ----------------------------------------------------- V4 dust deposit
h_before = list(v.holdings)
dust = [1, 1, 1, 1]
try:
    deposit(v, dust)
    outcome = "MINTED — BUG"
except ValueError as e:
    outcome = f"rejected: {e}"
invariants(v)
rec("V4 — dust deposit of 1 base unit per leg must be REJECTED",
    {"holdings_before": h_before, "delivered": dust},
    {"result": outcome,
     "why": "flooring `required` drives every leg to 0 while k stays positive. "
            "Deriving gross from k would mint ~8,800 base units against nothing "
            "retained. gross MUST be recomputed from the retained amounts."})

# ------------------------------------------------------ V5 entry fee 100bp
v.entry_bp = 100
h_before = list(v.holdings)
d5 = buy_ratio(v, 1000 * USDC, P["t3"])
u5, c5, ref5 = deposit(v, d5)
invariants(v)
rec("V5 — deposit $1,000 with the entry fee at its 100 bp cap",
    {"holdings_before": h_before, "delivered": d5, "entry_bp": 100},
    {"units_to_depositor": u5, "units_to_creator": c5,
     "check": "to_depositor + to_creator == gross", "gross": u5 + c5,
     "creator_share_bp": (c5 * BP) // (u5 + c5)})
v.entry_bp = 0

# ---------------------------------------------------------- V6 redemption
h_before = list(v.holdings)
sup_before = v.supply
half = u // 2
pay, burned, cfee = redeem(v, half)
invariants(v)
rec("V6 — redeem half of the V2 depositor's units, 0 bp exit fee",
    {"holdings_before": h_before, "supply_before": sup_before,
     "units_in": half, "exit_bp": 0},
    {"payouts": pay, "units_burned": burned, "units_to_creator": cfee,
     "supply_after": v.supply,
     "invariant": "payout_i == holdings_i * burned / supply_before, floored"})

# ------------------------------------------------------- V7 exit fee 100bp
v.exit_bp = 100
h_before = list(v.holdings)
sup_before = v.supply
pay7, burned7, cfee7 = redeem(v, half // 2)
invariants(v)
rec("V7 — redeem with the exit fee at its 100 bp cap",
    {"holdings_before": h_before, "supply_before": sup_before,
     "units_in": half // 2, "exit_bp": 100},
    {"payouts": pay7, "units_burned": burned7, "units_to_creator": cfee7,
     "note": "fee units are TRANSFERRED to the creator, not burned; the payout "
             "is computed on the burnt amount only"})
v.exit_bp = 0

# ------------------------------------------------------- V8 streaming fee
v.stream_bp = 100
sup_before = v.supply
minted = accrue(v, now + 365 * 24 * 3600)
invariants(v)
rec("V8 — streaming fee at 100 bp/yr accrued over exactly one year",
    {"supply_before": sup_before, "stream_bp": 100, "elapsed_seconds": 365 * 24 * 3600},
    {"units_minted": minted, "supply_after": v.supply,
     "creator_share_bp": (minted * BP) // v.supply,
     "invariant": "minted/(supply_before+minted) == 100bp, floored"})
v.stream_bp = 0

# --------------------------------------------------- V9 no rebalance needed
_, _, w = weights_bp(v.holdings, P["t3"])
rec("V9 — rebalance check with no leg above 50%",
    {"holdings": v.holdings, "prices": P["t3"]},
    {"weights_bp": w, "plan": None if rebalance_plan(v.holdings, P["t3"]) is None else "…",
     "note": "observe() must record no candidate"})

# ------------------------------------------- V10 rebalance, no cap binding
H10 = [(550 * USDC * TOK) // P["t3"][0], (200 * USDC * TOK) // P["t3"][1],
       (150 * USDC * TOK) // P["t3"][2], (100 * USDC * TOK) // P["t3"][3]]
H10 = [h * 1000 for h in H10]          # scale to a $1m fund
plan10 = rebalance_plan(H10, P["t3"])
after10 = apply_plan(H10, plan10)
_, tot10, w10 = weights_bp(H10, P["t3"])
_, _, wa10 = weights_bp(after10, P["t3"])
rec("V10 — rebalance from 55/20/15/10, equal thirds, cap does not bind",
    {"weights_before_bp": w10, "total_value_usdc": tot10},
    {"leg": LEGS[plan10["leg"]],
     "sell_value_usdc": plan10["sell_value_placed"],
     "alloc_value_usdc": {LEGS[i]: a for i, a in plan10["alloc_value"].items()},
     "capped": plan10["capped"],
     "weights_after_bp": wa10,
     "note": "equal thirds pushes the smallest laggard up the most"})

# ------------------------------------------------ V11 rebalance, cap binds
H11 = [(505 * USDC * TOK) // P["t3"][0], (445 * USDC * TOK) // P["t3"][1],
       (30 * USDC * TOK) // P["t3"][2], (20 * USDC * TOK) // P["t3"][3]]
H11 = [h * 1000 for h in H11]
plan11 = rebalance_plan(H11, P["t3"])
after11 = apply_plan(H11, plan11)
_, tot11, w11 = weights_bp(H11, P["t3"])
_, _, wa11 = weights_bp(after11, P["t3"])
rec("V11 — rebalance from 50.5/44.5/3/2, the 40% recipient cap binds",
    {"weights_before_bp": w11, "total_value_usdc": tot11},
    {"leg": LEGS[plan11["leg"]],
     "sell_value_usdc": plan11["sell_value_placed"],
     "alloc_value_usdc": {LEGS[i]: a for i, a in plan11["alloc_value"].items()},
     "capped": plan11["capped"],
     "weights_after_bp": wa11,
     "note": "without the cap Alphabet would land above 50% and immediately "
             "re-breach the trigger, then sit there for 180 days"})

with open("permafrost_test_vectors.json", "w") as fh:
    json.dump(vectors, fh, indent=2)
print(f"\n\nwrote permafrost_test_vectors.json — {len(vectors)} cases")

# --------------------------------------------- V13/V14 auction decay schedule
from permafrost_reference import (
    fair_ratio, margin_bp, offered_ratio, fill_required,
    AUCTION_DECAY, AUCTION_START_BP, AUCTION_FLOOR_BP, SELL_UNIT,
)

fr = fair_ratio(plan10, P["t3"])
fr_list = [fr.get(i, 0) for i in range(4)]

sched = []
for hours in [0, 1, 3, 6, 24, 72]:
    t = hours * 3600
    sched.append({"hours": hours, "margin_bp": margin_bp(t),
                  "offered_ratio": offered_ratio(fr_list, t)})

rec("V13 — decay schedule on the V10 auction (per 1 whole NVDAx)",
    {"buy_ratio_fair": fr_list,
     "start_bp": AUCTION_START_BP, "floor_bp": AUCTION_FLOOR_BP,
     "decay_seconds": AUCTION_DECAY},
    {"schedule": sched,
     "note": "margin runs +25bp -> -150bp linearly over 6h, then flat. "
             "Ratios round UP: they are what ENTERS the vault."})

sell_amt = plan10["sell_tokens"] // 10          # a 10% partial fill
req_open = fill_required(fr_list, sell_amt, 0)
req_floor = fill_required(fr_list, sell_amt, AUCTION_DECAY)
rec("V14 — a 10% partial fill, at open and at the floor",
    {"sell_amount_tokens": sell_amt, "buy_ratio_fair": fr_list},
    {"required_at_t0": req_open,
     "required_at_floor": req_floor,
     "delta_bp": 175,
     "note": "fill() reads no oracle; only elapsed time. Required amounts use "
             "ceiling division so the vault never rounds against itself."})

with open("permafrost_test_vectors.json", "w") as fh:
    json.dump(vectors, fh, indent=2)
print(f"\nwrote permafrost_test_vectors.json — {len(vectors)} cases")

# ------------------------------------------------------ V12 Pyth exponent
from permafrost_reference import normalise, conservative_prices, PYTH_FEEDS

rows = []
for price, expo in [(22043750000, -8), (34055000000, -8),
                    (50999000, -5), (57443123456, -9), (340550000, -6)]:
    rows.append({"pyth_price": price, "expo": expo,
                 "usdc_floor": normalise(price, expo, False),
                 "usdc_ceil": normalise(price, expo, True),
                 "human": f"${price * (10 ** expo):,.6f}"})

rec("V12 — Pyth exponent normalisation to USDC base units",
    {"rule": "usdc = price * 10**(expo + 6); expo is READ from the feed, never assumed",
     "cases": len(rows)},
    {"table": rows,
     "note": "expo -8 is typical for US equities today but MUST NOT be hard-coded. "
             "A one-place error here is unfixable after deployment."})

# conservative direction, candidate leg priced low, others high
feeds = [(22043750000, 5000000, -8), (34055000000, 8000000, -8),
         (50999000000, 12000000, -8), (57443000000, 14000000, -8)]
rec("V12b — conservative pricing, candidate leg = NVDAx (index 0)",
    {"feeds_price_conf_expo": feeds, "candidate_leg": 0},
    {"prices_usdc": conservative_prices(feeds, 0),
     "note": "candidate uses (price - conf) rounded DOWN; others use "
             "(price + conf) rounded UP. Both make the trigger harder to hit."})

rec("V12c — the confirmed mainnet feed IDs",
    {"source": "hermes.pyth.network/v2/price_feeds, 4 Sep 2026",
     "symbol_pattern": "Equity.US.<TICKER>/USD (regular session)"},
    {"feeds": PYTH_FEEDS,
     "schedule": "America/New_York;0930-1600 x5;C,C — closed at weekends, "
                 "which is what enforces weekday-only rebalancing"})

with open("permafrost_test_vectors.json", "w") as fh:
    json.dump(vectors, fh, indent=2)
print(f"\nwrote permafrost_test_vectors.json — {len(vectors)} cases")
