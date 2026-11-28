"""The v4 fill against REAL Jupiter routes, in LiteSVM with mainnet state cloned.

    python3 -I tests/sim_fill_mainnet.py [--amount 10] [--rpc URL]

What it proves before the mainnet upgrade: that Jupiter accepts the escrow PDA as
the swapping user with the parameters the gas service uses (direct routes, no
shared accounts, pinned destination, no setup/cleanup); that the CPI depth and
the transaction size are within limits for the DEXes Jupiter picks today; and
that fill_leg's guards pass on an honest real route — in both directions.

Cloned from mainnet, verbatim: Jupiter v6 and its program data, each DEX program
on the route and its program data, every account the swap instruction names, the
lookup tables it uses, and the USDC mint. The four xStock mints are the harness's
issuer-swapped copies (so the seed can be minted); the pools hold the real tokens
of those same mint addresses, which is all the DEXes care about.
"""
from __future__ import annotations

import base64
import json
import os
import struct
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

os.environ.setdefault("PF_MOCK", "0")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402
import test_v1 as V1  # noqa: E402
from solders.account import Account  # noqa: E402
from solders.address_lookup_table_account import AddressLookupTableAccount  # noqa: E402
from solders.compute_budget import set_compute_unit_limit  # noqa: E402
from solders.instruction import AccountMeta, Instruction  # noqa: E402
from solders.message import MessageV0  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

arg = lambda f, d: (sys.argv[sys.argv.index(f) + 1] if f in sys.argv else d)  # noqa: E731
RPC = arg("--rpc", "https://api.mainnet-beta.solana.com")
AMOUNT = int(float(arg("--amount", "10")) * 10**6)
JUP = "https://lite-api.jup.ag"
EXCL = ("&excludeDexes=" + urllib.parse.quote(os.environ["EXCLUDE_DEXES"])) if os.environ.get("EXCLUDE_DEXES") else ""
LOADER_V3 = H.LOADER_V3
BPF_LOADER_V2 = Pubkey.from_string("BPFLoader2111111111111111111111111111111111")
GAS = H.det_keypair("gas-service")


def rpc(method, params):
    for attempt in range(8):
        req = urllib.request.Request(RPC, json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(), {"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                out = json.loads(r.read())
            if "error" in out and "rate" in json.dumps(out["error"]).lower():
                raise RuntimeError("rate")
            if "error" in out:
                raise RuntimeError(json.dumps(out["error"])[:300])
            return out["result"]
        except Exception as e:  # noqa: BLE001
            if attempt == 7:
                raise
            time.sleep(2.5 * (attempt + 1))


def jup(path, body=None):
    req = urllib.request.Request(JUP + path, json.dumps(body).encode() if body else None, {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def fetch_accounts(keys):
    out = {}
    for i in range(0, len(keys), 50):
        chunk = keys[i:i + 50]
        got = rpc("getMultipleAccounts", [[str(k) for k in chunk], {"encoding": "base64", "commitment": "confirmed"}])["value"]
        for k, v in zip(chunk, got):
            out[k] = None if v is None else (v["lamports"], base64.b64decode(v["data"][0]), Pubkey.from_string(v["owner"]), v["executable"])
    return out


def install_program(w, pid: Pubkey, acc):
    """A mainnet program, verbatim: upgradeable (Program + ProgramData) or loader-v2."""
    lamports, data, owner, executable = acc
    if owner == LOADER_V3:
        pd_key = Pubkey(data[4:36])
        pd = fetch_accounts([pd_key])[pd_key]
        w.svm.set_account(pd_key, Account(pd[0], pd[1], LOADER_V3, False))
        w.svm.set_account(pid, Account(lamports, data, LOADER_V3, True))
    else:
        w.svm.add_program(pid, data)


def main():
    print(f"rpc {RPC}\norder of ${AMOUNT / 1e6:.2f} USDC, direct routes")
    w = H.World().launch()
    w.svm.airdrop(GAS.pubkey(), 100 * 10**9)
    # USDC: the real mint, verbatim; the user's and gas wallet's accounts set directly.
    usdc_acc = fetch_accounts([H.USDC])[H.USDC]
    w.svm.set_account(H.USDC, Account(usdc_acc[0], usdc_acc[1], H.TOKEN, False))
    user = w.new_user("sim-user")

    def set_classic(owner, mint, amount):
        d = bytes(mint) + bytes(owner) + struct.pack("<Q", amount) + b"\0" * 36 + b"\x01" + b"\0" * 12 + struct.pack("<Q", 0) + b"\0" * 36
        w.svm.set_account(H.ata_classic(owner, mint), Account(2_039_280, d, H.TOKEN, False))
    set_classic(user.pubkey(), H.USDC, 10 * AMOUNT)
    set_classic(GAS.pubkey(), H.USDC, 1_000_000)
    # Lookup-table addresses are only usable after the slot they were added in,
    # and DEX maths may read the clock: run at mainnet's slot and time.
    slot = rpc("getSlot", [{"commitment": "confirmed"}]) + int(os.environ.get("SLOT_OFFSET", "2"))
    w.svm.warp_to_slot(slot)
    clk = w.svm.get_clock()
    clk.slot = slot
    clk.unix_timestamp = int(time.time())
    w.svm.set_clock(clk)

    # Plan like the site: split by the vault's CURRENT weights (holdings × live
    # price), so the four legs land in proportion; 1% slippage floors.
    pj = jup("/price/v3?ids=" + ",".join(str(m) for m in H.LEG_MINTS))
    live = [float(pj[str(m)]["usdPrice"]) for m in H.LEG_MINTS]
    hold = w.holdings()
    vals = [h / 1e8 * p for h, p in zip(hold, live)]
    budget = [int(AMOUNT * v / sum(vals)) for v in vals]
    print("vault weights now:", ", ".join(f"{H.LEGS[i]} {vals[i] / sum(vals) * 100:.1f}%" for i in range(4)), "→ budgets", [f"${b / 1e6:.2f}" for b in budget])
    quotes, min_out = [], []
    for m, per in zip(H.LEG_MINTS, budget):
        q = jup(f"/swap/v1/quote?inputMint={H.USDC}&outputMint={m}&amount={per}&slippageBps=100&onlyDirectRoutes=true&maxAccounts=24" + EXCL)
        assert "routePlan" in q, q
        quotes.append(q)
        min_out.append(int(q["otherAmountThreshold"]) * 99 // 100)
    labels = [" → ".join(r["swapInfo"]["label"] for r in q["routePlan"]) for q in quotes]
    print("routes:", ", ".join(f"{H.LEGS[i]} via {labels[i]}" for i in range(4)))

    r = w.send([w.ix_order_invest(user, GAS, 1, AMOUNT, budget, min_out, 1, 0)], [user, GAS])
    assert r.ok, "order_invest: " + r.reason()
    oa = H.order_accounts(user.pubkey(), 1)
    print(f"  ✓ order_invest  escrow {oa['order']}")

    sizes, cus, failed = [], [], []
    for leg in range(4):
        s = jup("/swap/v1/swap-instructions", {"quoteResponse": quotes[leg], "userPublicKey": str(oa["order"]), "wrapAndUnwrapSol": False,
                                                 "useSharedAccounts": False, "destinationTokenAccount": str(oa["legs"][leg]),
                                                 "skipUserAccountsRpcCalls": True, "dynamicComputeUnitLimit": False})
        assert "swapInstruction" in s, s
        ix = s["swapInstruction"]
        assert ix["programId"] == str(H.JUPITER), ix["programId"]
        # Jupiter adds a create-idempotent for the destination when told not to check
        # the chain; the escrow's accounts exist already, and only the swap is forwarded.
        for si in s.get("setupInstructions") or []:
            assert si["programId"] == str(H.ATA_PROGRAM) and si["data"] == "AQ==", f"unexpected setup instruction: {si}"
        swap_metas = [AccountMeta(Pubkey.from_string(a["pubkey"]), a["isSigner"] and a["pubkey"] != str(oa["order"]), a["isWritable"]) for a in ix["accounts"]]
        swap_data = base64.b64decode(ix["data"])
        # Clone: every account the swap names (programs included), the lookup tables.
        keys = [m.pubkey for m in swap_metas]
        alt_keys = [Pubkey.from_string(a) for a in s.get("addressLookupTableAddresses", [])]
        fetched = fetch_accounts(list(dict.fromkeys(keys + alt_keys + [H.JUPITER])))
        programs = []
        for k, acc in fetched.items():
            if acc is None:
                continue
            if acc[3]:  # executable → a program
                if w.svm.get_account(k) is None:
                    install_program(w, k, acc)
                    programs.append(str(k)[:4])
            elif k not in (H.USDC, *H.LEG_MINTS) and k != oa["order"] and k not in oa["legs"] and k != oa["stable"]:
                w.svm.set_account(k, Account(acc[0], acc[1], acc[2], False))
        alts = [AddressLookupTableAccount(k, H.lookup_addresses(fetched[k][1])) for k in alt_keys if fetched.get(k)]
        fill = w.ix_fill_leg(GAS, user.pubkey(), 1, leg, 0, 0, swap_metas=swap_metas, swap_data=swap_data, swap_program=H.JUPITER)
        cu_ix = set_compute_unit_limit(min(1_400_000, int(s.get("computeUnitLimit", 400_000)) + 150_000))
        msg = MessageV0.try_compile(GAS.pubkey(), [cu_ix, fill], alts, w.svm.latest_blockhash())
        tx = VersionedTransaction(msg, [GAS])
        size = len(bytes(tx))
        sizes.append(size)
        before = w.balance(oa["legs"][leg])
        res = w.svm.send_transaction(tx)
        w.svm.expire_blockhash()
        ok = type(res).__name__ == "TransactionMetadata"
        logs = res.logs() if ok else res.meta().logs()
        if not ok:
            print(f"  ✗ fill leg {leg} {H.LEGS[leg]} via {labels[leg]}: {res.err()}")
            print("    " + "\n    ".join(l for l in logs[-8:] if "failed" in l or "Error" in l or "error" in l))
            failed.append(leg)
            continue
        got = w.balance(oa["legs"][leg]) - before
        cus.append(res.compute_units_consumed())
        print(f"  ✓ fill leg {leg} {H.LEGS[leg]} via {labels[leg]}: got {got:,} (min {min_out[leg]:,}, quote {quotes[leg]['outAmount']}), "
              f"{size} bytes, {cus[-1]:,} CU, programs cloned {programs}")
    o = w.order(user.pubkey(), 1)
    if failed:
        print(f"\nlegs that failed: {[H.LEGS[i] for i in failed]} — see above")
        sys.exit(1)
    assert o["filled"] == 0b1111
    r = w.send([w.ix_settle_invest(GAS, user.pubkey(), 1, fee=0)], [GAS])
    assert r.ok, "settle_invest: " + r.reason() + " ".join(r.logs[-6:])
    units = V1.units_of(w, user)
    nav = sum(h / 1e8 * p for h, p in zip(w.holdings(), live)) / (w.supply() / 1e9)
    print(f"  ✓ settle_invest: {units / 1e9:.6f} PAIF for ${AMOUNT / 1e6:.2f} (worth ${units / 1e9 * nav:.4f} at live prices; "
          f"leftover above the binding leg stayed in the vault); escrow closed: {w.account_data(oa['order']) is None}")

    # Now the way back: a redeem order sold through real routes.
    r = w.send([w.ix_order_redeem(user, GAS, 2, units, (0, 0, 0, 0), 0)], [user, GAS])
    assert r.ok, "order_redeem: " + r.reason()
    o = w.order(user.pubkey(), 2)
    oa = H.order_accounts(user.pubkey(), 2)
    print(f"  ✓ order_redeem  escrowed {o['budget']}")
    usdc0 = w.stable_balance(user.pubkey())
    for leg in range(4):
        if o["filled"] & (1 << leg):
            continue
        q = jup(f"/swap/v1/quote?inputMint={H.LEG_MINTS[leg]}&outputMint={H.USDC}&amount={o['budget'][leg]}&slippageBps=100&onlyDirectRoutes=true&maxAccounts=24")
        assert "routePlan" in q, q
        s = jup("/swap/v1/swap-instructions", {"quoteResponse": q, "userPublicKey": str(oa["order"]), "wrapAndUnwrapSol": False,
                                                 "useSharedAccounts": False, "destinationTokenAccount": str(oa["stable"]),
                                                 "skipUserAccountsRpcCalls": True, "dynamicComputeUnitLimit": False})
        ix = s["swapInstruction"]
        swap_metas = [AccountMeta(Pubkey.from_string(a["pubkey"]), a["isSigner"] and a["pubkey"] != str(oa["order"]), a["isWritable"]) for a in ix["accounts"]]
        keys = [m.pubkey for m in swap_metas]
        alt_keys = [Pubkey.from_string(a) for a in s.get("addressLookupTableAddresses", [])]
        fetched = fetch_accounts(list(dict.fromkeys(keys + alt_keys)))
        for k, acc in fetched.items():
            if acc is None:
                continue
            if acc[3]:
                if w.svm.get_account(k) is None:
                    install_program(w, k, acc)
            elif k not in (H.USDC, *H.LEG_MINTS) and k != oa["order"] and k not in oa["legs"] and k != oa["stable"]:
                w.svm.set_account(k, Account(acc[0], acc[1], acc[2], False))
        alts = [AddressLookupTableAccount(k, H.lookup_addresses(fetched[k][1])) for k in alt_keys if fetched.get(k)]
        fill = w.ix_fill_leg(GAS, user.pubkey(), 2, leg, 0, 0, swap_metas=swap_metas, swap_data=base64.b64decode(ix["data"]), swap_program=H.JUPITER)
        msg = MessageV0.try_compile(GAS.pubkey(), [set_compute_unit_limit(min(1_400_000, int(s.get("computeUnitLimit", 400_000)) + 150_000)), fill], alts, w.svm.latest_blockhash())
        tx = VersionedTransaction(msg, [GAS])
        res = w.svm.send_transaction(tx)
        w.svm.expire_blockhash()
        if type(res).__name__ != "TransactionMetadata":
            print(f"  ✗ sell leg {leg}: {res.err()}\n    " + "\n    ".join(res.meta().logs()[-12:]))
            sys.exit(1)
        print(f"  ✓ sell leg {leg} {H.LEGS[leg]} via {' → '.join(r['swapInfo']['label'] for r in q['routePlan'])}: {len(bytes(tx))} bytes, {res.compute_units_consumed():,} CU")
    r = w.send([w.ix_settle_redeem(GAS, user.pubkey(), 2, fee=0)], [GAS])
    assert r.ok, "settle_redeem: " + r.reason() + " ".join(r.logs[-6:])
    back = w.stable_balance(user.pubkey()) - usdc0
    print(f"  ✓ settle_redeem: ${back / 1e6:.4f} back of ${AMOUNT / 1e6:.2f} (round trip through eight real routes)")
    print(f"\nfill transactions: {max(sizes)} bytes at most (limit 1,232), {max(cus):,} CU at most")
    print("MAINNET ROUTES OK")


if __name__ == "__main__":
    main()
