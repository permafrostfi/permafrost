//! Differential-testing scenario generator.
//!
//! Emits one JSON object per line on stdout: a randomly generated scenario
//! (seed + a sequence of operations) together with everything the Rust core
//! produced for it. `diffcheck.py` replays the same inputs through
//! `permafrost_reference.py` and diffs the outputs bit-for-bit.
//!
//!     cargo run --release --bin diffgen -- <count> <seed> | python3 ../diffcheck.py
//!
//! The generator is a deterministic splitmix64, so a failing run is exactly
//! reproducible from the (count, seed) pair printed in the header line.
//!
//! EVERY number crosses as a decimal STRING. Intermediates here reach 10^36 and
//! JSON numbers are only good to 2^64, so anything else would silently truncate
//! and the harness would be comparing rounded values — which is precisely the
//! class of bug it exists to catch.

use permafrost_core::*;
use serde_json::{json, Value};
use std::io::{BufWriter, Write};

// ------------------------------------------------------------------- prng

struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
    /// Uniform in [lo, hi] inclusive.
    fn range(&mut self, lo: u128, hi: u128) -> u128 {
        if hi <= lo {
            return lo;
        }
        let span = hi - lo + 1;
        let r = ((self.next() as u128) << 64) | (self.next() as u128);
        lo + (r % span)
    }
    fn irange(&mut self, lo: i64, hi: i64) -> i64 {
        lo + (self.range(0, (hi - lo) as u128) as i64)
    }
    /// Log-uniform in [lo, hi]: picks a bit-width first, so dust and whale
    /// magnitudes appear about equally often instead of every draw being large.
    fn logrange(&mut self, lo: u128, hi: u128) -> u128 {
        let hb = (128 - hi.leading_zeros()).max(1);
        let lb = (128 - lo.leading_zeros()).max(1);
        let bits = self.range(lb as u128, hb as u128) as u32;
        let base = if bits <= 1 { 1 } else { 1u128 << (bits - 1) };
        let top = if bits >= 128 { u128::MAX } else { (1u128 << bits) - 1 };
        let a = base.max(lo);
        let b = top.min(hi);
        if b < a { a } else { self.range(a, b) }
    }
    fn pick(&mut self, n: usize) -> usize {
        (self.next() % (n as u64)) as usize
    }
}

// ---------------------------------------------------------------- helpers

/// Every number crosses the wire as a decimal string. See the module note.
fn n(x: u128) -> Value {
    Value::String(x.to_string())
}
fn ni(x: i128) -> Value {
    Value::String(x.to_string())
}
fn arr(a: &[u128; NLEGS]) -> Value {
    json!([n(a[0]), n(a[1]), n(a[2]), n(a[3])])
}

/// Prices in USDC base units per whole token: $0.50 .. $20,000.
fn gen_prices(rng: &mut Rng) -> [u128; NLEGS] {
    let mut p = [0u128; NLEGS];
    for i in 0..NLEGS {
        p[i] = rng.logrange(500_000, 20_000_000_000);
    }
    p
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let count: u64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(100_000);
    let rngseed: u64 = args
        .get(2)
        .and_then(|s| s.parse().ok())
        .unwrap_or(0x5045_524D_4146_524F);
    // "real": magnitudes a live vault would actually hold, so the campaign is
    // not dominated by single-base-unit vaults where every deposit is dust.
    // Default: the full log-uniform sweep, dust and whales alike.
    let real = args.get(3).map(|s| s == "real").unwrap_or(false);
    let mut rng = Rng(rngseed);

    let out = std::io::stdout();
    let mut w = BufWriter::with_capacity(1 << 20, out.lock());
    writeln!(w, "{}", json!({"header": true, "count": count, "seed": rngseed})).unwrap();

    for id in 0..count {
        // ---------------------------------------------------------- seeding
        let mut amounts = [0u128; NLEGS];
        for i in 0..NLEGS {
            amounts[i] = if real {
                rng.logrange(10_000_000_000, 100_000_000_000_000) // 100 .. 1e6 tokens
            } else {
                rng.logrange(1, 100_000_000_000_000) // 1e-8 .. 1e6 tokens
            };
        }
        let initial_units = if real {
            rng.logrange(1_000_000_000_000, 1_000_000_000_000_000) // 1e3 .. 1e6 units
        } else {
            rng.logrange(1, 1_000_000_000_000_000)
        };
        let entry_bp = rng.range(0, MAX_ENTRY_BP);
        let exit_bp = rng.range(0, MAX_EXIT_BP);
        let stream_bp = rng.range(0, MAX_STREAM_BP);

        let mut v = match permafrost_core::seed(amounts, initial_units, 0) {
            Ok(v) => v,
            Err(_) => continue,
        };
        v.entry_bp = entry_bp;
        v.exit_bp = exit_bp;
        v.stream_bp = stream_bp;

        // ------------------------------------------------- discount mode setup
        //
        // Set the dials and pre-fund the pool at seeding rather than relying on
        // the op loop to stumble into a productive combination. A bonus is only
        // non-zero when the toggle is on AND the rate is non-zero AND the cap is
        // non-zero AND the pool has something in it; leaving all four to chance
        // over a handful of ops would make most discounted deposits pay zero and
        // the campaign would be testing the trivial path.
        let discount_on = rng.pick(4) != 0; // on 75% of the time
        let discount_bp = rng.range(0, MAX_DISCOUNT_BP);
        let locked_discount_bp = rng.range(0, MAX_LOCKED_DISCOUNT_BP);
        // The cap ranges from "binds on every deposit" to "never binds".
        let max_bonus_units = if rng.pick(8) == 0 {
            0 // bonuses disabled entirely
        } else {
            rng.logrange(1, initial_units.max(1))
        };
        // At seeding, escrow is 0 and the pool is empty, so the whole supply is
        // available headroom.
        let subsidy_units = if rng.pick(8) == 0 {
            0
        } else {
            rng.logrange(1, initial_units.max(1))
        };
        v.discount_on = discount_on;
        v.discount_bp = discount_bp;
        v.locked_discount_bp = locked_discount_bp;
        v.max_bonus_units = max_bonus_units;
        v.subsidy_units = subsidy_units;

        // A clock for the locked path, independent of `last_accrual`. Every op
        // that needs a time carries it explicitly so the replay cannot drift.
        let mut clock: i64 = rng.irange(0, 2_000_000_000);

        let mut ops: Vec<Value> = Vec::new();
        let mut res: Vec<Value> = Vec::new();
        let nops = 1 + rng.pick(14);

        for _ in 0..nops {
            clock += rng.irange(0, 20 * 24 * 60 * 60);
            match rng.pick(14) {
                // ------------------------------------------------- deposit
                0 | 1 => {
                    // Scale off the live vault so deposits are neither absurd nor
                    // always dust, then jitter each leg so the ratio rarely matches.
                    let num = if real { rng.logrange(10_000, 2_000_000) } else { rng.logrange(100, 2_000_000) };
                    let mut d = [0u128; NLEGS];
                    for i in 0..NLEGS {
                        let base = v.holdings[i].saturating_mul(num) / 1_000_000;
                        let jitter = rng.range(5_000, 20_000); // 0.5x .. 2x
                        d[i] = base.saturating_mul(jitter) / 10_000;
                    }
                    ops.push(json!(["dep", arr(&d)]));
                    res.push(match deposit(&mut v, d) {
                        Ok(o) => json!([
                            "ok",
                            n(o.to_depositor),
                            n(o.to_creator),
                            arr(&o.refunds),
                            n(o.gross),
                            arr(&o.required),
                            n(o.k)
                        ]),
                        Err(_) => json!(["err"]),
                    });
                }
                // -------------------------------------------------- redeem
                2 => {
                    // Only the FLOAT is redeemable. The seed units sit at an
                    // unspendable address, so no caller can ever present them —
                    // modelling otherwise would manufacture a supply < dead
                    // state the chain cannot reach.
                    //
                    // The subsidy pool and the escrow are excluded for the same
                    // reason: both are unit balances held by program PDAs, and
                    // no caller can present them to `redeem`. Allowing it here
                    // would burn supply out from under the pool and break
                    // `subsidy + escrow <= supply` in a way the chain cannot.
                    let float = v
                        .supply
                        .saturating_sub(v.dead_units)
                        .saturating_sub(v.subsidy_units)
                        .saturating_sub(v.escrow_units);
                    if float == 0 {
                        continue;
                    }
                    let u = rng.logrange(1, float);
                    ops.push(json!(["red", n(u)]));
                    res.push(match redeem(&mut v, u) {
                        Ok(o) => json!(["ok", arr(&o.payouts), n(o.burned), n(o.to_creator)]),
                        Err(_) => json!(["err"]),
                    });
                }
                // -------------------------------------------------- accrue
                3 => {
                    let dt = rng.irange(-1000, 200 * 24 * 60 * 60);
                    let now = v.last_accrual + dt;
                    ops.push(json!(["acc", now]));
                    res.push(match accrue(&mut v, now) {
                        Ok(m) => json!(["ok", n(m), v.last_accrual]),
                        Err(_) => json!(["err"]),
                    });
                }
                // ----------------------------------------------- rebalance
                4 => {
                    let p = gen_prices(&mut rng);
                    ops.push(json!(["reb", arr(&p)]));
                    res.push(match rebalance_plan(&v.holdings, &p) {
                        Ok(None) => json!(["ok", null]),
                        Ok(Some(pl)) => json!([
                            "ok",
                            pl.leg,
                            arr(&pl.weights_bp),
                            n(pl.total_value),
                            n(pl.sell_value_wanted),
                            n(pl.sell_value_placed),
                            pl.capped,
                            arr(&pl.alloc_value),
                            n(pl.sell_tokens),
                            arr(&pl.buy_tokens),
                            match apply_plan(&v.holdings, &pl) {
                                Ok(h) => arr(&h),
                                Err(_) => Value::Null,
                            }
                        ]),
                        Err(_) => json!(["err"]),
                    });
                }
                // ------------------------------------------------- auction
                5 => {
                    let p = gen_prices(&mut rng);
                    let elapsed = rng.irange(-3600, AUCTION_WINDOW + 3600);
                    let sell_num = rng.range(1, 10_000);
                    ops.push(json!(["auc", arr(&p), elapsed, n(sell_num)]));
                    let r = (|| -> Res<Option<Value>> {
                        let pl = match rebalance_plan(&v.holdings, &p)? {
                            None => return Ok(None),
                            Some(pl) => pl,
                        };
                        let fair = fair_ratio(&pl)?;
                        let amt = (pl.sell_tokens * sell_num / 10_000).max(1);
                        let need = fill_required(&fair, amt, elapsed)?;
                        Ok(Some(json!([
                            ni(margin_bp(elapsed)),
                            arr(&fair),
                            arr(&offered_ratio(&fair, elapsed)?),
                            n(amt),
                            arr(&need)
                        ])))
                    })();
                    res.push(match r {
                        Ok(None) => json!(["ok", null]),
                        Ok(Some(vv)) => json!(["ok", vv]),
                        Err(_) => json!(["err"]),
                    });
                }
                // -------------------------------------------- fund the pool
                6 => {
                    // Mostly legal amounts, but one in seven deliberately asks
                    // for more than exists so both sides exercise the refusal
                    // that stops a pool being funded out of nothing.
                    let headroom = v
                        .supply
                        .saturating_sub(v.subsidy_units)
                        .saturating_sub(v.escrow_units);
                    let amt = if rng.pick(7) == 0 {
                        rng.range(headroom + 1, headroom.saturating_mul(2) + 16)
                    } else if headroom == 0 {
                        rng.range(1, 16)
                    } else {
                        rng.logrange(1, headroom)
                    };
                    ops.push(json!(["fund", n(amt)]));
                    res.push(match fund_subsidy(&mut v, amt) {
                        Ok(()) => json!(["ok", n(v.subsidy_units)]),
                        Err(_) => json!(["err"]),
                    });
                }
                // ---------------------------------------- creator withdrawal
                7 => {
                    let amt = if rng.pick(7) == 0 || v.subsidy_units == 0 {
                        rng.range(v.subsidy_units + 1, v.subsidy_units + 16)
                    } else {
                        rng.logrange(1, v.subsidy_units)
                    };
                    ops.push(json!(["wd", n(amt)]));
                    res.push(match withdraw_subsidy(&mut v, amt) {
                        Ok(()) => json!(["ok", n(v.subsidy_units)]),
                        Err(_) => json!(["err"]),
                    });
                }
                // ----------------------------------------- move the dials
                8 | 9 => {
                    // One in nine attempts a rate above its immutable ceiling,
                    // so both sides exercise the refusal.
                    let over = rng.pick(9) == 0;
                    let dbp = if over {
                        rng.range(MAX_DISCOUNT_BP + 1, MAX_DISCOUNT_BP + 50)
                    } else {
                        rng.range(0, MAX_DISCOUNT_BP)
                    };
                    let lbp = if rng.pick(9) == 0 {
                        rng.range(MAX_LOCKED_DISCOUNT_BP + 1, MAX_LOCKED_DISCOUNT_BP + 50)
                    } else {
                        rng.range(0, MAX_LOCKED_DISCOUNT_BP)
                    };
                    let cap = if rng.pick(6) == 0 {
                        0
                    } else {
                        rng.logrange(1, v.supply.max(1))
                    };
                    let on = rng.pick(4) != 0;
                    // The toggle is assigned FIRST and unconditionally — it is a
                    // separate field, not part of `set_discount`, so a rejected
                    // rate must not roll it back. The replay does the same.
                    v.discount_on = on;
                    ops.push(json!(["dial", on, n(dbp), n(lbp), n(cap)]));
                    res.push(match set_discount(&mut v, dbp, lbp, cap) {
                        Ok(()) => json!([
                            "ok",
                            n(v.discount_bp),
                            n(v.locked_discount_bp),
                            n(v.max_bonus_units)
                        ]),
                        Err(_) => json!(["err"]),
                    });
                }
                // ------------------------------ liquid discounted deposit
                10 => {
                    let num = if real { rng.logrange(10_000, 2_000_000) } else { rng.logrange(100, 2_000_000) };
                    let mut d = [0u128; NLEGS];
                    for i in 0..NLEGS {
                        let base = v.holdings[i].saturating_mul(num) / 1_000_000;
                        let jitter = rng.range(5_000, 20_000);
                        d[i] = base.saturating_mul(jitter) / 10_000;
                    }
                    ops.push(json!(["depd", arr(&d)]));
                    res.push(match deposit_discounted(&mut v, d) {
                        Ok(o) => json!([
                            "ok",
                            n(o.to_depositor),
                            n(o.to_creator),
                            arr(&o.refunds),
                            n(o.bonus),
                            n(o.minted_to_depositor),
                            n(v.subsidy_units)
                        ]),
                        Err(_) => json!(["err"]),
                    });
                }
                // ---------------------------------------- locked deposit
                11 | 12 => {
                    let num = if real { rng.logrange(10_000, 2_000_000) } else { rng.logrange(100, 2_000_000) };
                    let mut d = [0u128; NLEGS];
                    for i in 0..NLEGS {
                        let base = v.holdings[i].saturating_mul(num) / 1_000_000;
                        let jitter = rng.range(5_000, 20_000);
                        d[i] = base.saturating_mul(jitter) / 10_000;
                    }
                    ops.push(json!(["depl", arr(&d), clock]));
                    res.push(match deposit_locked(&mut v, d, clock) {
                        Ok(o) => json!([
                            "ok",
                            o.position_id,
                            n(o.principal),
                            n(o.bonus),
                            n(o.to_creator),
                            arr(&o.refunds),
                            o.unlock_at,
                            n(v.subsidy_units),
                            n(v.escrow_units)
                        ]),
                        Err(_) => json!(["err"]),
                    });
                }
                // -------------------------------------- close a position
                13 => {
                    if v.positions.is_empty() {
                        continue;
                    }
                    // BTreeMap iterates in key order, so the pick is stable and
                    // the replay lands on the same position.
                    let keys: Vec<u64> = v.positions.keys().copied().collect();
                    let pid = keys[rng.pick(keys.len())];
                    let unlock = v.positions[&pid].unlock_at;
                    // Straddle maturity deliberately: a third of the attempts
                    // land on the wrong side of the boundary so both sides
                    // exercise NotMatured / AlreadyMatured, and the exact
                    // instant `now == unlock_at` is sampled explicitly.
                    let when = match rng.pick(6) {
                        0 => unlock,
                        1 => unlock - 1,
                        2 => unlock + 1,
                        3 => unlock - rng.irange(1, LOCK_PERIOD),
                        4 => unlock + rng.irange(1, LOCK_PERIOD),
                        _ => clock,
                    };
                    if rng.pick(2) == 0 {
                        ops.push(json!(["clm", pid, when]));
                        res.push(match claim_locked(&mut v, pid, when) {
                            Ok(out) => json!(["ok", n(out), n(v.escrow_units), n(v.subsidy_units)]),
                            Err(_) => json!(["err"]),
                        });
                    } else {
                        ops.push(json!(["exl", pid, when]));
                        res.push(match exit_locked_early(&mut v, pid, when) {
                            Ok(out) => json!(["ok", n(out), n(v.escrow_units), n(v.subsidy_units)]),
                            Err(_) => json!(["err"]),
                        });
                    }
                }
                _ => unreachable!(),
            }
        }

        // -------------------------------------------------- pyth (stateless)
        let mut feeds: Vec<Value> = Vec::new();
        let mut fv: Vec<(i128, i128, i32)> = Vec::new();
        for _ in 0..NLEGS {
            let price = rng.logrange(1, 1_000_000_000_000) as i128;
            let conf = rng.range(0, (price as u128) / 5) as i128;
            let expo = rng.irange(-12, 2) as i32;
            feeds.push(json!([ni(price), ni(conf), expo]));
            fv.push((price, conf, expo));
        }
        let cand = rng.pick(NLEGS);
        ops.push(json!(["pyth", feeds, cand]));
        res.push(match conservative_prices(&fv, cand) {
            Ok(p) => json!(["ok", p.iter().map(|&x| ni(x)).collect::<Vec<_>>()]),
            Err(_) => json!(["err"]),
        });

        writeln!(
            w,
            "{}",
            json!({
                "id": id,
                "seed": {
                    "amounts": arr(&amounts),
                    "initial_units": n(initial_units),
                    "entry_bp": entry_bp,
                    "exit_bp": exit_bp,
                    "stream_bp": stream_bp,
                    "discount_on": discount_on,
                    "discount_bp": n(discount_bp),
                    "locked_discount_bp": n(locked_discount_bp),
                    "max_bonus_units": n(max_bonus_units),
                    "subsidy_units": n(subsidy_units),
                },
                "ops": ops,
                "res": res,
                "final": {
                    "holdings": arr(&v.holdings),
                    "supply": n(v.supply),
                    "creator_units": n(v.creator_units),
                    "dead_units": n(v.dead_units),
                    "last_accrual": v.last_accrual,
                    "subsidy_units": n(v.subsidy_units),
                    "escrow_units": n(v.escrow_units),
                    "next_position_id": v.next_position_id,
                    "discount_on": v.discount_on,
                    "discount_bp": n(v.discount_bp),
                    "locked_discount_bp": n(v.locked_discount_bp),
                    "max_bonus_units": n(v.max_bonus_units),
                    // Sorted by id, so the comparison is order-independent on
                    // the Python side too.
                    "positions": v.positions.values()
                        .map(|p| json!([p.owner, n(p.principal), n(p.bonus), p.unlock_at]))
                        .collect::<Vec<_>>(),
                    "inv": invariants(&v),
                }
            })
        )
        .unwrap();
    }
    w.flush().unwrap();
}
