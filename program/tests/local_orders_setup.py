"""LOCAL VALIDATOR ONLY — set up a chain for the v4 order rehearsal (tools/test_app_orders.mjs).

Two commands:
  accounts OUTDIR            write the account files the validator is started with: the four
                             real xStock mints with the test issuer's keys (as the LiteSVM
                             harness does), the USDC/USDT mints with a test authority, and
                             funded USDC accounts for the test user and the gas wallet.
  launch USER_KEY GAS_KEY    (PF_RPC=http://localhost:8899) initialise + seed + init_v3 with
                             the harness ADMIN, fund the mock swap pool, mint stock to nobody.

Run with PF_MOCK=1 so the harness targets the mockswap build (SWAP_PROGRAM = the mock) and
the mainnet mint addresses.
"""
import base64
import json
import os
import struct
import sys
from pathlib import Path

assert os.environ.get("PF_MOCK") == "1", "PF_MOCK=1"
assert "localhost" in os.environ.get("PF_RPC", "localhost") or "127.0.0.1" in os.environ.get("PF_RPC", ""), "local validator only"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402
from solders.account import Account  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

TOKEN = H.TOKEN


def acct_json(pubkey, lamports, data, owner, out):
    json.dump({"pubkey": str(pubkey), "account": {"lamports": lamports, "data": [base64.b64encode(data).decode(), "base64"],
               "owner": str(owner), "executable": False, "rentEpoch": 0, "space": len(data)}}, open(out, "w"))


def classic_token_account(mint, owner, amount):
    d = bytes(mint) + bytes(owner) + struct.pack("<Q", amount) + b"\0" * 36 + b"\x01" + b"\0" * 12 + struct.pack("<Q", 0) + b"\0" * 36
    assert len(d) == 165
    return d


def cmd_accounts(outdir, user_key, gas_key):
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    w = H.World.__new__(H.World)   # only for the mint bytes; no chain
    from solders.litesvm import LiteSVM
    w.svm = LiteSVM()
    for m in H.REAL_MINTS:
        w._install_mint(m)
        a = w.svm.get_account(m)
        acct_json(m, a.lamports, bytes(a.data), H.TOKEN_2022, out / f"mint_{str(m)[:6]}.json")
    for m in (H.USDC, H.USDT):
        d = struct.pack("<I32sQB?I32s", 1, bytes(H.STABLE_AUTH.pubkey()), 0, H.STABLE_DECIMALS, True, 0, bytes(32))
        acct_json(m, 10**9, d, TOKEN, out / f"stable_{str(m)[:6]}.json")
    user = Keypair.from_bytes(bytes(json.load(open(user_key)))).pubkey()
    gas = Keypair.from_bytes(bytes(json.load(open(gas_key)))).pubkey()
    for who, o, amt in (("user", user, 1_000_000_000), ("gas", gas, 5_000_000), ("pool", H.MOCK_POOL, 10**12)):
        for m in (H.USDC, H.USDT):
            acct_json(H.ata_classic(o, m), 2_039_280, classic_token_account(m, o, amt), TOKEN, out / f"{who}_{str(m)[:4]}.json")
    print(f"  wrote {len(list(out.glob('*.json')))} account files to {out}")
    print("  issuer (mint authority for the four stocks):", H.ISSUER.pubkey())
    print("  stable mint authority:", H.STABLE_AUTH.pubkey())
    print("  mock pool PDA:", H.MOCK_POOL)


# --- RPC plumbing (devnet.py's, which cannot be imported outside PF_DEVNET)
import base64 as _b64  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402
from solders.compute_budget import set_compute_unit_limit  # noqa: E402
from solders.hash import Hash  # noqa: E402
from solders.instruction import AccountMeta, Instruction  # noqa: E402
from solders.message import Message  # noqa: E402
from solders.transaction import Transaction  # noqa: E402

RPC = os.environ.get("PF_RPC", "http://localhost:8899")


def rpc(method, params):
    req = urllib.request.Request(RPC, json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                                 {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read())
    if "error" in out:
        raise RuntimeError(f"{method}: {json.dumps(out['error'])[:1000]}")
    return out["result"]


def data(addr):
    v = rpc("getAccountInfo", [str(addr), {"encoding": "base64", "commitment": "confirmed"}])["value"]
    return None if v is None else _b64.b64decode(v["data"][0])


def balance(token_account):
    d = data(token_account)
    return 0 if d is None else struct.unpack_from("<Q", d, 64)[0]


def send(ixs, signers, label, cu=1_000_000):
    ixs = [set_compute_unit_limit(cu)] + list(ixs)
    bh = Hash.from_string(rpc("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"])
    tx = Transaction(signers, Message(ixs, signers[0].pubkey()), bh)
    sig = rpc("sendTransaction", [_b64.b64encode(bytes(tx)).decode(), {"encoding": "base64", "preflightCommitment": "confirmed"}])
    for _ in range(60):
        st = rpc("getSignatureStatuses", [[sig]])["value"][0]
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            if st.get("err"):
                raise RuntimeError(f"{label} failed on chain: {st['err']} {sig}")
            print(f"  ✓ {label}")
            return sig
        time.sleep(1)
    raise RuntimeError(f"{label}: not confirmed")


def cmd_launch():
    for kp in (H.ADMIN, H.ISSUER, H.STABLE_AUTH):
        rpc("requestAirdrop", [str(kp.pubkey()), 20 * 10**9])
    time.sleep(2)
    W = H.World.__new__(H.World)  # builders only
    if data(H.VAULT) is None:
        send([W.ix_initialize(H.ADMIN, H.CAP_50K, "Permafrost AI Fund", "PAIF", "")], [H.ADMIN], "initialize")
    mint_ix = lambda owner, m, amt: Instruction(H.TOKEN_2022, bytes([14]) + struct.pack("<QB", amt, H.LEG_DECIMALS), [  # noqa: E731
        AccountMeta(m, False, True), AccountMeta(H.ata(owner, m), False, True), AccountMeta(H.ISSUER.pubkey(), True, False)])
    if balance(H.DEAD_UNITS) == 0:
        ixs = [W.create_ata_ix(H.ADMIN.pubkey(), H.ADMIN.pubkey(), m) for m in H.LEG_MINTS]
        ixs += [mint_ix(H.ADMIN.pubkey(), m, amt) for m, amt in zip(H.LEG_MINTS, H.SEED_AMOUNTS)]
        send(ixs, [H.ADMIN, H.ISSUER], "mint seed legs to admin")
        send([W.ix_seed(H.ADMIN, H.SEED_AMOUNTS)], [H.ADMIN], "seed")
    if data(H.VAULT)[19] == 0:
        send([W.ix_init_v3(H.ADMIN)], [H.ADMIN], "init_v3")
    if balance(H.ata(H.MOCK_POOL, H.LEG_MINTS[0])) == 0:
        ixs = [W.create_ata_ix(H.ISSUER.pubkey(), H.MOCK_POOL, m) for m in H.LEG_MINTS]
        ixs += [mint_ix(H.MOCK_POOL, m, 10**12) for m in H.LEG_MINTS]
        send(ixs, [H.ISSUER], "fund mock pool with stock")
    print(f"  vault seed units {balance(H.DEAD_UNITS):,}, holdings {[balance(v) for v in H.VAULT_LEGS]}")
    print("launch OK")


def cmd_keys():
    print(json.dumps({"admin": str(H.ADMIN.pubkey()), "admin_secret": list(bytes(H.ADMIN)), "issuer": str(H.ISSUER.pubkey()),
                      "pool": str(H.MOCK_POOL), "program": str(H.PROGRAM_ID), "mock": str(H.MOCK_SWAP),
                      "legs": [str(m) for m in H.LEG_MINTS]}))


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    {"accounts": cmd_accounts, "launch": cmd_launch, "keys": cmd_keys}[cmd](*args)
