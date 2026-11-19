#!/usr/bin/env python3
"""
Permafrost — JupiterZ capacity probe.

Samples Jupiter Ultra quotes for the five xStocks in both directions across a
size ladder, records which router filled (jupiterz = RFQ market maker,
metis/okx = AMM aggregator), and computes the effective price per share and the
round-trip cost.

Run it on a machine with plain internet access. No wallet, no keys, no funds:
these are read-only quotes and nothing is ever signed or submitted.

    python3 jupiterz_probe.py                 # one sample, prints a table
    python3 jupiterz_probe.py --csv out.csv   # append a sample to CSV

For a week of coverage, sample every 30 minutes:
    */30 * * * * cd /path && /usr/bin/python3 jupiterz_probe.py --csv jz.csv >> jz.log 2>&1

What to look for in the CSV:
  * router == "jupiterz"  -> a market maker quoted you; size barely matters
  * router != "jupiterz"  -> you fell back to AMM pools; size matters a lot
  * the size at which router flips is your real per-leg capacity
  * whether that size differs on weekends / outside US market hours
"""

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.request

ULTRA = "https://lite-api.jup.ag/ultra/v1/order"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DEC = 6

# Backed xStocks on Solana. All Token-2022, 8 decimals.
LEGS = {
    "NVDAx":  "Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh",
    "GOOGLx": "XsCPL9dNWBMvFtTmwcCA5v3xWPSMEBCszbQdiLLq6aN",
    "MSFTx":  "XspzcW1PRtgf6Wj92HCiZdjzKCyFekVD8P5Ueh3dRMX",
    "METAx":  "Xsa62P5mvPszXL1krVUnU5ar38bBSVcWAB6fmPCo5Zu",
    "SPCXx":  "Xs3oZwbHvqis4NYcf4YKWmEia2eC84wSiVrcYcTqpH8",
}
LEG_DEC = 8

# Dollars per leg. A $20k basket deposit is $4k per leg; $100k is $20k per leg.
SIZES_USD = [200, 1_000, 4_000, 20_000, 50_000]

FIELDS = [
    "ts_utc", "leg", "direction", "size_usd", "router", "swap_type",
    "in_amount", "out_amount", "price_per_share", "price_impact_pct",
    "round_trip_cost_pct",
]


def quote(input_mint, output_mint, amount, retries=3):
    """One Ultra quote. Returns the parsed JSON, or None if it never answered."""
    url = f"{ULTRA}?inputMint={input_mint}&outputMint={output_mint}&amount={int(amount)}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "permafrost-probe/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as exc:  # noqa: BLE001 - we want to survive any transport failure
            if attempt == retries - 1:
                print(f"  ! {input_mint[:6]}->{output_mint[:6]} @ {amount}: {exc}", file=sys.stderr)
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def sample():
    """Probe every leg at every size, buy then sell back exactly what was bought."""
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    rows = []

    for leg, mint in LEGS.items():
        for usd in SIZES_USD:
            buy = quote(USDC, mint, usd * 10**USDC_DEC)
            if not buy or "outAmount" not in buy:
                continue
            tokens_raw = int(buy["outAmount"])
            tokens = tokens_raw / 10**LEG_DEC
            buy_px = usd / tokens if tokens else float("nan")

            rows.append({
                "ts_utc": ts, "leg": leg, "direction": "buy", "size_usd": usd,
                "router": buy.get("router"), "swap_type": buy.get("swapType"),
                "in_amount": buy.get("inAmount"), "out_amount": buy.get("outAmount"),
                "price_per_share": round(buy_px, 4),
                "price_impact_pct": buy.get("priceImpactPct"),
                "round_trip_cost_pct": "",
            })

            # Sell back exactly what the buy produced: the gap is the full spread.
            sell = quote(mint, USDC, tokens_raw)
            if not sell or "outAmount" not in sell:
                continue
            back = int(sell["outAmount"]) / 10**USDC_DEC
            sell_px = back / tokens if tokens else float("nan")

            rows.append({
                "ts_utc": ts, "leg": leg, "direction": "sell", "size_usd": usd,
                "router": sell.get("router"), "swap_type": sell.get("swapType"),
                "in_amount": sell.get("inAmount"), "out_amount": sell.get("outAmount"),
                "price_per_share": round(sell_px, 4),
                "price_impact_pct": sell.get("priceImpactPct"),
                "round_trip_cost_pct": round((usd - back) / usd * 100, 4),
            })
            time.sleep(0.35)  # be polite to the public endpoint

    return ts, rows


def print_table(ts, rows):
    print(f"\nJupiterZ capacity probe — {ts}\n")
    print(f"{'leg':8}{'size':>9}  {'buy via':<10}{'buy px':>10}  {'sell via':<10}{'round trip':>11}")
    print("-" * 62)
    buys = {(r["leg"], r["size_usd"]): r for r in rows if r["direction"] == "buy"}
    for r in rows:
        if r["direction"] != "sell":
            continue
        b = buys.get((r["leg"], r["size_usd"]))
        if not b:
            continue
        print(f"{r['leg']:8}${r['size_usd']:>8,}  {str(b['router']):<10}{b['price_per_share']:>10.2f}  "
              f"{str(r['router']):<10}{r['round_trip_cost_pct']:>10.3f}%")
    rfq = sum(1 for r in rows if r["router"] == "jupiterz")
    print(f"\n{rfq}/{len(rows)} quotes filled by an RFQ market maker.\n")


def main():
    ap = argparse.ArgumentParser(description="Probe JupiterZ RFQ capacity for the Permafrost basket.")
    ap.add_argument("--csv", help="append results to this CSV file")
    ap.add_argument("--quiet", action="store_true", help="suppress the table")
    args = ap.parse_args()

    ts, rows = sample()
    if not rows:
        print("No quotes returned — check network access to lite-api.jup.ag", file=sys.stderr)
        return 1

    if args.csv:
        new = not os.path.exists(args.csv)
        with open(args.csv, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerows(rows)
        print(f"Appended {len(rows)} rows to {args.csv}")

    if not args.quiet:
        print_table(ts, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
