# Permafrost

An immutable, non-custodial Solana vault holding tokenised Nvidia, Alphabet,
Microsoft and Meta (Backed xStocks, Token-2022).

**Status:** arithmetic core complete and tested. Anchor program not started.
Unaudited.

## Layout

| path | what it is |
|---|---|
| `permafrost_reference.py` | Executable reference. The Rust must match it bit for bit. |
| `permafrost-core/` | Rust arithmetic crate, no Solana dependencies. Drops into the Anchor program as a library. |
| `gen_vectors.py`, `permafrost_test_vectors.json` | Spec vectors V1–V16. **Frozen.** |
| `gen_discount_vectors.py`, `permafrost_discount_vectors.json` | Discount-mode vectors D0–D6. |
| `diffcheck.py`, `run_campaign.sh` | Differential harness: Rust generator vs Python reference. |
| `discount_fuzz.py` | Reference-side fuzz of discount mode, properties P1–P5. |
| `pyth_feed_check.py`, `jupiterz_probe.py` | Read-only probes of live oracle and router state. |

## Run the tests

```bash
cd permafrost-core && cargo test --release          # 46 tests
cd .. && python3 discount_fuzz.py 20000             # discount mode, P1–P5
./run_campaign.sh 250000 40                         # ~12 min, must end 0 MISMATCHES
```

Every number in the differential harness crosses as a decimal string:
intermediates reach 10^36 and a JSON number stops being exact above 2^53.

## Known open issue

The liquid discount path is farmable at zero fees by cycling deposit → redeem.
Fix pending a design decision; see the project notes. The locked path is
unaffected.
