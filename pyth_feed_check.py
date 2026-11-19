#!/usr/bin/env python3
"""
Permafrost — Pyth equity feed verification.

Confirms that Pyth publishes feeds for the four basket names, and prints the
feed IDs that will be hard-coded into the vault program. Run it anywhere with
plain internet access. Read-only, no wallet, no keys.

    python3 pyth_feed_check.py

Two things to look for in the output:

  1. Every ticker resolves to at least one feed. If any does not, that leg
     cannot be priced and the rebalance design needs rethinking.

  2. Whether a ticker has BOTH a market-hours feed and a 24/7 index. Pyth now
     publishes both for many names, and they are used for different jobs:

       market-hours feed  -> the rebalance trigger. Its staleness at weekends
                             is what enforces weekday-only rebalancing for
                             free, with no calendar logic in the contract.

       24/7 index         -> the dashboard. Users checking a balance on a
                             Sunday see a live number instead of a stale
                             Friday print. Display only, never settlement.

The IDs printed here go straight into the spec and the test vectors. Copy them
exactly — a wrong feed ID in an immutable contract cannot be corrected.
"""

import json
import sys
import urllib.request

HERMES = "https://hermes.pyth.network/v2/price_feeds"
TICKERS = ["NVDA", "GOOGL", "MSFT", "META"]


def fetch(query, asset_type=None):
    url = f"{HERMES}?query={query}"
    if asset_type:
        url += f"&asset_type={asset_type}"
    req = urllib.request.Request(url, headers={"User-Agent": "permafrost-feed-check/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        print(f"  ! {query}: {exc}", file=sys.stderr)
        return []


def classify(schedule):
    """market hours / 24-7 / unknown, decided by PARSING the schedule.

    The previous version asked `"24" in schedule`, which matches the "1224" in
    every US holiday calendar (Christmas Eve, a half day) and therefore labelled
    every equity feed a 24/7 index. That is not a cosmetic slip: the design uses
    the distinction to decide which feed settles a rebalance and which one only
    draws a dashboard, and the answer it was giving was wrong for all eight.

    Pyth's format is `TZ;<7 day ranges, comma separated>;<holiday overrides>`,
    where a day is `C` for closed or `HHMM-HHMM` for open. A genuine 24/7 feed
    has all seven days open around the clock.
    """
    s = str(schedule)
    if not s:
        return "no schedule"
    parts = s.split(";")
    if len(parts) < 2:
        return "unknown"
    days = [d.strip() for d in parts[1].split(",")]
    if len(days) != 7:
        return "unknown"
    if all(d == "0000-2359" or d == "0000-0000" for d in days):
        return "24/7"
    closed = [i for i, d in enumerate(days) if d.upper() == "C"]
    if closed:
        # Mon..Sun; the weekend is indices 5 and 6.
        return "market hours, closed %s" % ("weekends" if closed == [5, 6]
                                            else f"days {closed}")
    return "market hours"


def main():
    print("\nPyth equity feed check — Permafrost basket\n")
    missing = []

    for ticker in TICKERS:
        entries = fetch(ticker, asset_type="equity") or fetch(ticker)
        # Keep only feeds whose symbol actually names this ticker, so a search
        # for META does not match METAV or similar.
        hits = []
        for e in entries:
            attrs = e.get("attributes", {})
            sym = attrs.get("symbol", "")
            base = attrs.get("base", "")
            if base.upper() == ticker or f".{ticker}/" in sym.upper():
                hits.append((sym, e.get("id", ""), attrs))

        print(f"{ticker}  —  {len(hits)} feed(s)")
        if not hits:
            missing.append(ticker)
            print("   NONE FOUND\n")
            continue

        for sym, fid, attrs in sorted(hits):
            schedule = attrs.get("schedule") or attrs.get("generic_symbol") or ""
            atype = attrs.get("asset_type", "")
            kind = classify(schedule)
            print(f"   {sym:34} {atype:10} [{kind}]")
            print(f"     id: {fid}")
            if schedule:
                print(f"     schedule: {schedule}")
        print()

    print("-" * 62)
    if missing:
        print(f"MISSING: {', '.join(missing)} — these legs cannot be priced by Pyth.")
        return 1
    print("All four basket names resolve to Pyth feeds.")
    print("Record the market-hours IDs for the rebalance trigger.")
    print()
    print("NOTE: as of the last run, BOTH variants Pyth publishes for these")
    print("names (Equity.US.* and Equity.Index.*) are market-hours feeds that")
    print("close at weekends. There is no 24/7 equity index to fall back on, so")
    print("a weekend dashboard has no live price from Pyth — it shows the last")
    print("Friday print, and it should say so rather than implying it is live.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
