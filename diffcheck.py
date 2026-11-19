#!/usr/bin/env python3
"""Differential harness: replay diffgen's scenarios through the Python reference.

Reads JSONL on stdin (one scenario per line, from `diffgen`), replays the
identical inputs through `permafrost_reference.py`, and compares every returned
value and the final vault state against what the Rust core produced.

Numbers cross the wire as decimal strings — intermediates reach 10^36 and a JSON
number tops out at 2^64, so comparing as numbers would compare rounded values,
which is the very class of bug this harness exists to catch.

    ./permafrost-core/target/release/diffgen 250000 | python3 diffcheck.py
"""

import json
import sys
import time
from collections import Counter

import permafrost_reference as R

MAXFAIL = 12


def S(x):
    """Normalise a Python value to the shape diffgen emits."""
    if isinstance(x, bool) or x is None:
        return x
    if isinstance(x, int):
        return str(x)
    if isinstance(x, (list, tuple)):
        return [S(e) for e in x]
    return x


def I(x):
    """Parse diffgen's decimal strings back to ints."""
    if isinstance(x, list):
        return [I(e) for e in x]
    return int(x)


def flat(d, n=4):
    """Reference returns dicts keyed by leg; flatten to a 4-list, 0 elsewhere."""
    out = [0] * n
    for i, val in d.items():
        out[i] = val
    return out


def run_scenario(rec):
    """Replay one scenario. Returns a list of (op_index, field, python, rust)."""
    bad = []
    s = rec["seed"]
    try:
        v = R.seed(I(s["amounts"]), I(s["initial_units"]), 0)
    except Exception:
        return [(-1, "seed", "raised", "accepted by rust")]
    v.entry_bp = s["entry_bp"]
    v.exit_bp = s["exit_bp"]
    v.stream_bp = s["stream_bp"]
    # Discount mode is armed at seeding rather than left to the op loop, so that
    # most discounted deposits actually pay a bonus. See the note in diffgen.
    v.discount_on = s["discount_on"]
    v.discount_bp = I(s["discount_bp"])
    v.locked_discount_bp = I(s["locked_discount_bp"])
    v.max_bonus_units = I(s["max_bonus_units"])
    v.subsidy_units = I(s["subsidy_units"])

    def cmp(idx, name, py, rs):
        if S(py) != rs:
            bad.append((idx, name, S(py), rs))

    for idx, (op, exp) in enumerate(zip(rec["ops"], rec["res"])):
        kind = op[0]
        rust_ok = exp[0] == "ok"

        # ------------------------------------------------------------ deposit
        if kind == "dep":
            try:
                to_dep, to_cre, refunds = R.deposit(v, I(op[1]))
                py_ok = True
            except Exception:
                py_ok = False
            if py_ok != rust_ok:
                bad.append((idx, "dep/status", py_ok, rust_ok))
                return bad  # the two vault states have diverged; stop here
            if py_ok:
                cmp(idx, "dep/to_depositor", to_dep, exp[1])
                cmp(idx, "dep/to_creator", to_cre, exp[2])
                cmp(idx, "dep/refunds", refunds, exp[3])
                cmp(idx, "dep/gross", to_dep + to_cre, exp[4])

        # ------------------------------------------------------------- redeem
        elif kind == "red":
            try:
                payouts, burned, to_cre = R.redeem(v, I(op[1]))
                py_ok = True
            except Exception:
                py_ok = False
            if py_ok != rust_ok:
                bad.append((idx, "red/status", py_ok, rust_ok))
                return bad
            if py_ok:
                cmp(idx, "red/payouts", payouts, exp[1])
                cmp(idx, "red/burned", burned, exp[2])
                cmp(idx, "red/to_creator", to_cre, exp[3])

        # ------------------------------------------------------------- accrue
        elif kind == "acc":
            try:
                minted = R.accrue(v, op[1])
                py_ok = True
            except Exception:
                py_ok = False
            if py_ok != rust_ok:
                bad.append((idx, "acc/status", py_ok, rust_ok))
                return bad
            if py_ok:
                cmp(idx, "acc/minted", minted, exp[1])
                if v.last_accrual != exp[2]:
                    bad.append((idx, "acc/last_accrual", v.last_accrual, exp[2]))

        # ---------------------------------------------------------- rebalance
        elif kind == "reb":
            prices = I(op[1])
            try:
                plan = R.rebalance_plan(v.holdings, prices)
                py_ok = True
            except Exception:
                plan, py_ok = None, False
            if py_ok != rust_ok:
                bad.append((idx, "reb/status", py_ok, rust_ok))
                continue
            if not py_ok:
                continue
            if plan is None:
                if exp[1] is not None:
                    bad.append((idx, "reb/none", None, exp[1]))
                continue
            if exp[1] is None:
                bad.append((idx, "reb/none", "plan", None))
                continue
            if plan["leg"] != exp[1]:
                bad.append((idx, "reb/leg", plan["leg"], exp[1]))
            cmp(idx, "reb/weights_bp", plan["weights_bp"], exp[2])
            cmp(idx, "reb/total_value", plan["total_value"], exp[3])
            cmp(idx, "reb/sell_value_wanted", plan["sell_value_wanted"], exp[4])
            cmp(idx, "reb/sell_value_placed", plan["sell_value_placed"], exp[5])
            if plan["capped"] != exp[6]:
                bad.append((idx, "reb/capped", plan["capped"], exp[6]))
            cmp(idx, "reb/alloc_value", flat(plan["alloc_value"]), exp[7])
            cmp(idx, "reb/sell_tokens", plan["sell_tokens"], exp[8])
            cmp(idx, "reb/buy_tokens", flat(plan["buy_tokens"]), exp[9])
            cmp(idx, "reb/apply", R.apply_plan(v.holdings, plan), exp[10])

        # ------------------------------------------------------------ auction
        elif kind == "auc":
            prices, elapsed, sell_num = I(op[1]), op[2], I(op[3])
            try:
                plan = R.rebalance_plan(v.holdings, prices)
                if plan is None:
                    py = None
                else:
                    fair = flat(R.fair_ratio(plan, prices))
                    amt = max(plan["sell_tokens"] * sell_num // 10_000, 1)
                    py = [
                        R.margin_bp(elapsed),
                        fair,
                        R.offered_ratio(fair, elapsed),
                        amt,
                        R.fill_required(fair, amt, elapsed),
                    ]
                py_ok = True
            except Exception:
                py, py_ok = None, False
            if py_ok != rust_ok:
                bad.append((idx, "auc/status", py_ok, rust_ok))
                continue
            if py_ok:
                cmp(idx, "auc", py, exp[1])

        # ----------------------------------------------------- discount mode
        # Every one of these mutates vault state, so a status divergence means
        # the two models have parted company and nothing after it is meaningful:
        # bail out the way `dep` and `red` do rather than reporting noise.
        elif kind == "fund":
            try:
                R.fund_subsidy(v, I(op[1]))
                py_ok = True
            except Exception:
                py_ok = False
            if py_ok != rust_ok:
                bad.append((idx, "fund/status", py_ok, rust_ok))
                return bad
            if py_ok:
                cmp(idx, "fund/subsidy_units", v.subsidy_units, exp[1])

        elif kind == "wd":
            try:
                R.withdraw_subsidy(v, I(op[1]))
                py_ok = True
            except Exception:
                py_ok = False
            if py_ok != rust_ok:
                bad.append((idx, "wd/status", py_ok, rust_ok))
                return bad
            if py_ok:
                cmp(idx, "wd/subsidy_units", v.subsidy_units, exp[1])

        elif kind == "dial":
            # The toggle is a plain field, not part of set_discount, so it is
            # assigned first and unconditionally — exactly as diffgen does.
            v.discount_on = op[1]
            try:
                R.set_discount(v, I(op[2]), I(op[3]), I(op[4]))
                py_ok = True
            except Exception:
                py_ok = False
            if py_ok != rust_ok:
                bad.append((idx, "dial/status", py_ok, rust_ok))
                return bad
            if py_ok:
                cmp(idx, "dial/discount_bp", v.discount_bp, exp[1])
                cmp(idx, "dial/locked_discount_bp", v.locked_discount_bp, exp[2])
                cmp(idx, "dial/max_bonus_units", v.max_bonus_units, exp[3])

        elif kind == "depd":
            try:
                got, to_cre, refunds, bonus = R.deposit_discounted(v, I(op[1]))
                py_ok = True
            except Exception:
                py_ok = False
            if py_ok != rust_ok:
                bad.append((idx, "depd/status", py_ok, rust_ok))
                return bad
            if py_ok:
                cmp(idx, "depd/to_depositor", got, exp[1])
                cmp(idx, "depd/to_creator", to_cre, exp[2])
                cmp(idx, "depd/refunds", refunds, exp[3])
                cmp(idx, "depd/bonus", bonus, exp[4])
                cmp(idx, "depd/minted_to_depositor", got - bonus, exp[5])
                cmp(idx, "depd/subsidy_units", v.subsidy_units, exp[6])

        elif kind == "depl":
            try:
                pid, principal, bonus, to_cre, refunds = R.deposit_locked(
                    v, I(op[1]), op[2])
                py_ok = True
            except Exception:
                py_ok = False
            if py_ok != rust_ok:
                bad.append((idx, "depl/status", py_ok, rust_ok))
                return bad
            if py_ok:
                if pid != exp[1]:
                    bad.append((idx, "depl/position_id", pid, exp[1]))
                cmp(idx, "depl/principal", principal, exp[2])
                cmp(idx, "depl/bonus", bonus, exp[3])
                cmp(idx, "depl/to_creator", to_cre, exp[4])
                cmp(idx, "depl/refunds", refunds, exp[5])
                unlock = op[2] + R.LOCK_PERIOD
                if unlock != exp[6]:
                    bad.append((idx, "depl/unlock_at", unlock, exp[6]))
                cmp(idx, "depl/subsidy_units", v.subsidy_units, exp[7])
                cmp(idx, "depl/escrow_units", v.escrow_units, exp[8])

        elif kind in ("clm", "exl"):
            fn = R.claim_locked if kind == "clm" else R.exit_locked_early
            try:
                out = fn(v, op[1], op[2])
                py_ok = True
            except Exception:
                py_ok = False
            if py_ok != rust_ok:
                bad.append((idx, f"{kind}/status", py_ok, rust_ok))
                return bad
            if py_ok:
                cmp(idx, f"{kind}/out", out, exp[1])
                cmp(idx, f"{kind}/escrow_units", v.escrow_units, exp[2])
                cmp(idx, f"{kind}/subsidy_units", v.subsidy_units, exp[3])

        # --------------------------------------------------------------- pyth
        elif kind == "pyth":
            feeds = [(int(f[0]), int(f[1]), f[2]) for f in op[1]]
            try:
                py = R.conservative_prices(feeds, op[2])
                py_ok = True
            except Exception:
                py, py_ok = None, False
            if py_ok != rust_ok:
                bad.append((idx, "pyth/status", py_ok, rust_ok))
                continue
            if py_ok:
                cmp(idx, "pyth", py, exp[1])

    f = rec["final"]
    cmp(99, "final/holdings", v.holdings, f["holdings"])
    cmp(99, "final/supply", v.supply, f["supply"])
    cmp(99, "final/creator_units", v.creator_units, f["creator_units"])
    cmp(99, "final/dead_units", v.dead_units, f["dead_units"])
    if v.last_accrual != f["last_accrual"]:
        bad.append((99, "final/last_accrual", v.last_accrual, f["last_accrual"]))
    cmp(99, "final/subsidy_units", v.subsidy_units, f["subsidy_units"])
    cmp(99, "final/escrow_units", v.escrow_units, f["escrow_units"])
    cmp(99, "final/discount_bp", v.discount_bp, f["discount_bp"])
    cmp(99, "final/locked_discount_bp", v.locked_discount_bp, f["locked_discount_bp"])
    cmp(99, "final/max_bonus_units", v.max_bonus_units, f["max_bonus_units"])
    if v.discount_on != f["discount_on"]:
        bad.append((99, "final/discount_on", v.discount_on, f["discount_on"]))
    if v.next_position_id != f["next_position_id"]:
        bad.append((99, "final/next_position_id",
                    v.next_position_id, f["next_position_id"]))
    # ids and timestamps cross as JSON numbers, unit amounts as decimal strings —
    # build the row in exactly that shape rather than running it through S().
    pos = [[p.owner, str(p.principal), str(p.bonus), p.unlock_at]
           for _, p in sorted(v.positions.items())]
    if pos != f["positions"]:
        bad.append((99, "final/positions", pos, f["positions"]))

    try:
        R.invariants(v)
        inv = True
    except AssertionError:
        inv = False
    if inv != f["inv"]:
        bad.append((99, "final/invariants", inv, f["inv"]))
    return bad, inv


def main():
    t0 = time.time()
    header = json.loads(sys.stdin.readline())
    n = failures = shown = broken_inv = 0
    opcount, errcount = Counter(), Counter()

    for line in sys.stdin:
        rec = json.loads(line)
        n += 1
        for op, res in zip(rec["ops"], rec["res"]):
            opcount[op[0]] += 1
            if res[0] != "ok":
                errcount[op[0]] += 1
        out = run_scenario(rec)
        bad, inv = out if isinstance(out, tuple) else (out, True)
        if not inv:
            broken_inv += 1
        if bad:
            failures += 1
            if shown < MAXFAIL:
                shown += 1
                print(f"\n=== MISMATCH scenario {rec['id']} ===")
                print("  seed:", json.dumps(rec["seed"]))
                for idx, name, py, rs in bad[:6]:
                    print(f"  op[{idx}] {name}\n    python: {py}\n    rust  : {rs}")
                    if 0 <= idx < len(rec["ops"]):
                        print("    op    :", json.dumps(rec["ops"][idx])[:300])

    dt = time.time() - t0
    print(f"\nscenarios          : {n:,}   (generator seed {header['seed']})")
    print(f"operations         : {sum(opcount.values()):,}")
    for k in sorted(opcount):
        rej = errcount[k]
        print(f"    {k:<5} {opcount[k]:>10,}   rejected by both: {rej:>9,} ({rej / opcount[k]:5.1%})")
    print(f"invariant failures : {broken_inv:,}")
    print(f"MISMATCHES         : {failures:,}")
    print(f"elapsed            : {dt:.1f}s")
    return 1 if (failures or broken_inv) else 0


if __name__ == "__main__":
    sys.exit(main())
