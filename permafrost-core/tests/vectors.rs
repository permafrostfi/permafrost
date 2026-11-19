//! Port of every case in `permafrost_test_vectors.json`.
//!
//! The vectors were produced by `gen_vectors.py` walking ONE vault through
//! V1..V8 in order, so this file walks the same vault through the same
//! sequence and asserts each recorded expectation. Where the JSON records a
//! `given` (delivered amounts, units_in) we assert that the Rust chain produced
//! exactly that value too — otherwise a divergence early in the chain could be
//! masked by feeding the JSON's inputs back in.
//!
//! No expected value in this file has been adjusted to make a test pass.

use permafrost_core::*;

const USDC: u128 = 1_000_000;

fn p_t0() -> [u128; 4] {
    [220 * USDC, 340 * USDC, 510 * USDC, 575 * USDC]
}
fn p_t1() -> [u128; 4] {
    [300 * USDC, 380 * USDC, 560 * USDC, 650 * USDC]
}
fn p_t2() -> [u128; 4] {
    [380 * USDC, 420 * USDC, 600 * USDC, 700 * USDC]
}
fn p_t3() -> [u128; 4] {
    [340 * USDC, 400 * USDC, 580 * USDC, 675 * USDC]
}

/// `gen_vectors.buy_ratio` — what the router aims for: k such that the basket
/// costs `budget`.
fn buy_ratio(v: &Vault, budget_usdc: u128, prices: &[u128; 4]) -> [u128; 4] {
    let total: u128 = (0..4).map(|i| v.holdings[i] * prices[i] / TOK).sum();
    let k = budget_usdc * K / total;
    let mut out = [0u128; 4];
    for i in 0..4 {
        out[i] = v.holdings[i] * k / K;
    }
    out
}

fn unit_value_usdc(v: &Vault, prices: &[u128; 4]) -> f64 {
    let total: u128 = (0..4).map(|i| v.holdings[i] * prices[i] / TOK).sum();
    (total as f64 * UNIT as f64 / v.supply as f64) / USDC as f64
}

const NOW: i64 = 1_767_225_600;

/// The whole V1..V8 chain, so later cases start from exactly the state
/// `gen_vectors.py` had. Returns the vault plus the values later cases need.
struct Chain {
    v: Vault,
    v2_units: u128,
}

fn run_chain() -> Chain {
    // ------------------------------------------------------------- V1 seed
    let seed_usd: u128 = 25;
    let per_leg = seed_usd * USDC / 4;
    let mut seed_tokens = [0u128; 4];
    for i in 0..4 {
        seed_tokens[i] = per_leg * TOK / p_t0()[i];
    }
    let initial_units = 25 * UNIT;
    let mut v = seed(seed_tokens, initial_units, NOW).expect("seed");

    assert_eq!(
        v.holdings,
        [2_840_909, 1_838_235, 1_225_490, 1_086_956],
        "V1 holdings"
    );
    assert_eq!(v.supply, 25_000_000_000, "V1 supply");
    assert_eq!(v.dead_units, 25_000_000_000, "V1 dead_units");
    assert!(
        (unit_value_usdc(&v, &p_t0()) - 0.99999976).abs() < 5e-10,
        "V1 unit_value_usdc = {}",
        unit_value_usdc(&v, &p_t0())
    );
    assert!(invariants(&v));

    // -------------------------------------------------- V2 exact deposit
    let before_price = unit_value_usdc(&v, &p_t1());
    let delivered = buy_ratio(&v, 2000 * USDC, &p_t1());
    assert_eq!(
        delivered,
        [193_022_905, 124_897_158, 83_264_772, 73_852_209],
        "V2 delivered (given)"
    );
    let d2 = deposit(&mut v, delivered).expect("V2 deposit");
    assert_eq!(d2.to_depositor, 1_698_601_599_328, "V2 units_to_depositor");
    assert_eq!(d2.to_creator, 0, "V2 units_to_creator");
    assert_eq!(d2.refunds, [1, 1, 1, 1], "V2 refunds");
    assert_eq!(v.supply, 1_723_601_599_328, "V2 supply");
    // The JSON labels this array `holdings_before`, but gen_vectors.py computes
    // it as `[h - 0 for h in v.holdings]` AFTER the deposit. It is the post-state.
    assert_eq!(
        v.holdings,
        [195_863_813, 126_735_392, 84_490_261, 74_939_164],
        "V2 holdings (JSON's mislabelled `holdings_before`)"
    );
    let after_price = unit_value_usdc(&v, &p_t1());
    assert!((before_price - 1.17743912).abs() < 5e-9, "V2 uv before");
    assert!((after_price - 1.177439123).abs() < 5e-10, "V2 uv after");
    assert!(invariants(&v));

    // ----------------------------------------- V3 imperfect ratio deposit
    let h_before = v.holdings;
    assert_eq!(
        h_before,
        [195_863_813, 126_735_392, 84_490_261, 74_939_164],
        "V3 holdings_before"
    );
    let mut skewed = buy_ratio(&v, 500 * USDC, &p_t2());
    // gen_vectors.py uses Python floats here: int(x * 1.10) / int(x * 1.03).
    skewed[1] = (skewed[1] as f64 * 1.10) as u128;
    skewed[3] = (skewed[3] as f64 * 1.03) as u128;
    assert_eq!(
        skewed,
        [42_429_905, 30_200_105, 18_303_093, 16_721_064],
        "V3 delivered (given)"
    );
    let d3 = deposit(&mut v, skewed).expect("V3 deposit");
    assert_eq!(d3.to_depositor, 373_383_136_357, "V3 units_to_depositor");
    assert_eq!(d3.refunds, [2, 2_745_466, 1, 487_022], "V3 refunds");
    assert_eq!(v.supply, 2_096_984_735_685, "V3 supply");
    assert!(invariants(&v));

    // --------------------------------------------------- V4 dust deposit
    let h_before = v.holdings;
    assert_eq!(
        h_before,
        [238_293_716, 154_190_031, 102_793_353, 91_173_206],
        "V4 holdings_before"
    );
    let sup_before = v.supply;
    let creator_before = v.creator_units;
    let err = deposit(&mut v, [1, 1, 1, 1]).unwrap_err();
    assert_eq!(err, PfError::DustDeposit, "V4 must be REJECTED");
    // The reference raises before any mutation; so must we.
    assert_eq!(v.holdings, h_before, "V4 must not move holdings");
    assert_eq!(v.supply, sup_before, "V4 must not move supply");
    assert_eq!(v.creator_units, creator_before, "V4 must not move creator");
    assert!(invariants(&v));

    // ----------------------------------------------- V5 entry fee 100 bp
    v.entry_bp = 100;
    let d5_in = buy_ratio(&v, 1000 * USDC, &p_t3());
    assert_eq!(
        d5_in,
        [90_311_370, 58_436_761, 38_957_840, 34_553_899],
        "V5 delivered (given)"
    );
    let d5 = deposit(&mut v, d5_in).expect("V5 deposit");
    assert_eq!(d5.to_depositor, 786_792_631_793, "V5 units_to_depositor");
    assert_eq!(d5.to_creator, 7_947_400_321, "V5 units_to_creator");
    assert_eq!(d5.gross, 794_740_032_114, "V5 gross");
    assert_eq!(
        d5.to_depositor + d5.to_creator,
        794_740_032_114,
        "V5 depositor + creator == gross"
    );
    assert_eq!(d5.to_creator * BP / d5.gross, 99, "V5 creator_share_bp");
    v.entry_bp = 0;
    assert!(invariants(&v));

    // -------------------------------------------------------- V6 redeem
    let h_before = v.holdings;
    assert_eq!(
        h_before,
        [328_605_084, 212_626_791, 141_751_192, 125_727_104],
        "V6 holdings_before"
    );
    assert_eq!(v.supply, 2_891_724_767_799, "V6 supply_before");
    let half = d2.to_depositor / 2;
    assert_eq!(half, 849_300_799_664, "V6 units_in (given)");
    let r6 = redeem(&mut v, half).expect("V6 redeem");
    assert_eq!(
        r6.payouts,
        [96_511_453, 62_448_579, 41_632_385, 36_926_104],
        "V6 payouts"
    );
    assert_eq!(r6.burned, 849_300_799_664, "V6 units_burned");
    assert_eq!(r6.to_creator, 0, "V6 units_to_creator");
    assert_eq!(v.supply, 2_042_423_968_135, "V6 supply_after");
    assert!(invariants(&v));

    // -------------------------------------------------- V7 exit fee 100bp
    v.exit_bp = 100;
    let h_before = v.holdings;
    assert_eq!(
        h_before,
        [232_093_631, 150_178_212, 100_118_807, 88_801_000],
        "V7 holdings_before"
    );
    assert_eq!(v.supply, 2_042_423_968_135, "V7 supply_before");
    let units_in7 = half / 2;
    assert_eq!(units_in7, 424_650_399_832, "V7 units_in (given)");
    let r7 = redeem(&mut v, units_in7).expect("V7 redeem");
    assert_eq!(
        r7.payouts,
        [47_773_169, 30_912_046, 20_608_031, 18_278_421],
        "V7 payouts"
    );
    assert_eq!(r7.burned, 420_403_895_834, "V7 units_burned");
    assert_eq!(r7.to_creator, 4_246_503_998, "V7 units_to_creator");
    v.exit_bp = 0;
    assert!(invariants(&v));

    // ------------------------------------------------- V8 streaming fee
    v.stream_bp = 100;
    assert_eq!(v.supply, 1_622_020_072_301, "V8 supply_before");
    let minted = accrue(&mut v, NOW + 365 * 24 * 3600).expect("V8 accrue");
    assert_eq!(minted, 16_384_041_134, "V8 units_minted");
    assert_eq!(v.supply, 1_638_404_113_435, "V8 supply_after");
    assert_eq!(minted * BP / v.supply, 99, "V8 creator_share_bp");
    v.stream_bp = 0;
    assert!(invariants(&v));

    Chain {
        v,
        v2_units: d2.to_depositor,
    }
}

#[test]
fn v1_to_v8_chain() {
    let c = run_chain();
    assert!(c.v2_units > 0);
}

#[test]
fn v9_no_rebalance_needed() {
    let c = run_chain();
    assert_eq!(
        c.v.holdings,
        [184_320_462, 119_266_166, 79_510_776, 70_522_579],
        "V9 holdings (given)"
    );
    let w = weights_bp(&c.v.holdings, &p_t3()).expect("weights");
    assert_eq!(w.w, [3070, 2337, 2259, 2332], "V9 weights_bp");
    assert!(
        rebalance_plan(&c.v.holdings, &p_t3()).expect("plan").is_none(),
        "V9 plan must be None"
    );
}

fn h10() -> [u128; 4] {
    let p = p_t3();
    let mut h = [0u128; 4];
    for (i, usd) in [550u128, 200, 150, 100].into_iter().enumerate() {
        h[i] = usd * USDC * TOK / p[i] * 1000;
    }
    h
}

fn h11() -> [u128; 4] {
    let p = p_t3();
    let mut h = [0u128; 4];
    for (i, usd) in [505u128, 445, 30, 20].into_iter().enumerate() {
        h[i] = usd * USDC * TOK / p[i] * 1000;
    }
    h
}

#[test]
fn v10_rebalance_cap_does_not_bind() {
    let h = h10();
    let w = weights_bp(&h, &p_t3()).expect("weights");
    assert_eq!(w.w, [5500, 2000, 1499, 999], "V10 weights_before_bp");
    assert_eq!(w.total, 999_999_985_900, "V10 total_value_usdc");

    let plan = rebalance_plan(&h, &p_t3()).expect("plan").expect("some plan");
    assert_eq!(plan.leg, 0, "V10 leg == NVDAx");
    assert_eq!(plan.sell_value_placed, 250_000_001_230, "V10 sell_value");
    assert_eq!(plan.alloc_value[1], 83_333_333_743, "V10 alloc GOOGLx");
    assert_eq!(plan.alloc_value[2], 83_333_333_743, "V10 alloc MSFTx");
    assert_eq!(plan.alloc_value[3], 83_333_333_744, "V10 alloc METAx");
    assert!(!plan.capped, "V10 capped == false");

    let after = apply_plan(&h, &plan).expect("apply");
    let wa = weights_bp(&after, &p_t3()).expect("weights after");
    assert_eq!(wa.w, [3000, 2833, 2333, 1833], "V10 weights_after_bp");
}

#[test]
fn v11_rebalance_cap_binds() {
    let h = h11();
    let w = weights_bp(&h, &p_t3()).expect("weights");
    assert_eq!(w.w, [5050, 4450, 299, 199], "V11 weights_before_bp");
    assert_eq!(w.total, 999_999_986_300, "V11 total_value_usdc");

    let plan = rebalance_plan(&h, &p_t3()).expect("plan").expect("some plan");
    assert_eq!(plan.leg, 0, "V11 leg == NVDAx");
    assert_eq!(plan.sell_value_placed, 205_000_001_510, "V11 sell_value");
    assert_eq!(plan.alloc_value[1], 0, "V11 alloc GOOGLx (already over cap)");
    assert_eq!(plan.alloc_value[2], 102_500_000_755, "V11 alloc MSFTx");
    assert_eq!(plan.alloc_value[3], 102_500_000_755, "V11 alloc METAx");
    assert!(!plan.capped, "V11 capped == false");

    let after = apply_plan(&h, &plan).expect("apply");
    let wa = weights_bp(&after, &p_t3()).expect("weights after");
    assert_eq!(wa.w, [3000, 4450, 1324, 1224], "V11 weights_after_bp");
}

fn fair_v10() -> [u128; 4] {
    let h = h10();
    let plan = rebalance_plan(&h, &p_t3()).unwrap().unwrap();
    fair_ratio(&plan).unwrap()
}

#[test]
fn v13_decay_schedule() {
    let fr = fair_v10();
    assert_eq!(
        fr,
        [0, 28_333_333, 19_540_229, 16_790_123],
        "V13 buy_ratio_fair"
    );
    assert_eq!(AUCTION_START_BP, 25);
    assert_eq!(AUCTION_FLOOR_BP, -150);
    assert_eq!(AUCTION_DECAY, 21600);

    let expected: [(i64, i128, [u128; 4]); 6] = [
        (0, 25, [0, 28_404_167, 19_589_080, 16_832_099]),
        (1, -4, [0, 28_322_000, 19_532_413, 16_783_407]),
        (3, -62, [0, 28_157_667, 19_419_080, 16_686_025]),
        (6, -150, [0, 27_908_334, 19_247_126, 16_538_272]),
        (24, -150, [0, 27_908_334, 19_247_126, 16_538_272]),
        (72, -150, [0, 27_908_334, 19_247_126, 16_538_272]),
    ];
    for (hours, m, ratio) in expected {
        let t = hours * 3600;
        assert_eq!(margin_bp(t), m, "V13 margin_bp at {hours}h");
        assert_eq!(
            offered_ratio(&fr, t).expect("offered"),
            ratio,
            "V13 offered_ratio at {hours}h"
        );
    }
}

#[test]
fn v14_partial_fill() {
    let h = h10();
    let plan = rebalance_plan(&h, &p_t3()).unwrap().unwrap();
    let fr = fair_ratio(&plan).unwrap();
    let sell_amt = plan.sell_tokens / 10;
    assert_eq!(sell_amt, 7_352_941_212, "V14 sell_amount_tokens");
    assert_eq!(
        fill_required(&fr, sell_amt, 0).expect("fill t0"),
        [0, 2_088_541_702, 1_440_373_537, 1_237_654_345],
        "V14 required_at_t0"
    );
    assert_eq!(
        fill_required(&fr, sell_amt, AUCTION_DECAY).expect("fill floor"),
        [0, 2_052_083_393, 1_415_229_860, 1_216_049_418],
        "V14 required_at_floor"
    );
    assert_eq!(AUCTION_START_BP - AUCTION_FLOOR_BP, 175, "V14 delta_bp");
}

#[test]
fn v12_pyth_normalisation() {
    let table: [(i128, i32, i128, i128); 5] = [
        (22_043_750_000, -8, 220_437_500, 220_437_500),
        (34_055_000_000, -8, 340_550_000, 340_550_000),
        (50_999_000, -5, 509_990_000, 509_990_000),
        (57_443_123_456, -9, 57_443_123, 57_443_124),
        (340_550_000, -6, 340_550_000, 340_550_000),
    ];
    for (price, expo, floor, ceil) in table {
        assert_eq!(
            normalise(price, expo, false).expect("floor"),
            floor,
            "V12 floor price={price} expo={expo}"
        );
        assert_eq!(
            normalise(price, expo, true).expect("ceil"),
            ceil,
            "V12 ceil price={price} expo={expo}"
        );
    }
}

#[test]
fn v12b_conservative_prices() {
    let feeds = [
        (22_043_750_000i128, 5_000_000i128, -8i32),
        (34_055_000_000, 8_000_000, -8),
        (50_999_000_000, 12_000_000, -8),
        (57_443_000_000, 14_000_000, -8),
    ];
    assert_eq!(
        conservative_prices(&feeds, 0).expect("conservative"),
        vec![220_387_500i128, 340_630_000, 510_110_000, 574_570_000],
        "V12b prices_usdc"
    );
}

#[test]
fn v12c_feed_ids() {
    assert_eq!(
        PYTH_FEEDS,
        [
            (
                "NVDAx",
                "b1073854ed24cbc755dc527418f52b7d271f6cc967bbf8d8129112b18860a593"
            ),
            (
                "GOOGLx",
                "5a48c03e9b9cb337801073ed9d166817473697efff0d138874e0f6a33d6d5aa6"
            ),
            (
                "MSFTx",
                "d0ca23c1cc005e004ccf1db5bf76aeb6a49218f43dac3d4b275e92de12ded4d1"
            ),
            (
                "METAx",
                "78a3e3b8e676a8f73c439f5d749737034b139bbbe899ba5775216fba596607fe"
            ),
        ]
    );
    assert_eq!(LEGS, ["NVDAx", "GOOGLx", "MSFTx", "METAx"]);
}
