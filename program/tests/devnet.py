"""Devnet rehearsal for Permafrost v1 — the same steps as mainnet, on a real cluster.

    PF_DEVNET=1 python3 -I tests/devnet.py replicas   # create the 4 replica mints
    (deploy with `solana program deploy`, see the launch checklist)
    PF_DEVNET=1 python3 -I tests/devnet.py e2e        # initialize, seed, deposit, redeem, set_cap
    PF_DEVNET=1 python3 -I tests/devnet.py handoff <new-authority>   # rehearse the key handoff
    PF_DEVNET=1 python3 -I tests/devnet.py fund <owner> [sol]        # give a wallet replica tokens
    PF_DEVNET=1 python3 -I tests/devnet.py status

Every step is idempotent: it checks chain state first and skips what is done, so a
rerun after a dropped connection is safe. Keys live in ~/devnet (never in the repo):
deployer.json pays for everything and is the replicas' issuer; mint_<LEG>.json are
the replica mint keypairs. These are devnet-only keys and hold nothing of value.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import sys
import time
import urllib.request
from pathlib import Path

assert os.environ.get("PF_DEVNET") == "1", "run with PF_DEVNET=1 (devnet build + replica mints)"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from solders.compute_budget import set_compute_unit_limit  # noqa: E402
from solders.hash import Hash  # noqa: E402
from solders.instruction import AccountMeta, Instruction  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.message import Message  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.system_program import TransferParams, transfer  # noqa: E402
from solders.transaction import Transaction  # noqa: E402

import harness as H  # noqa: E402
from replica import check_replica, replica_ixs  # noqa: E402

RPC = os.environ.get("PF_RPC", "https://api.devnet.solana.com")
KEYS = Path.home() / "devnet"


def load_kp(name: str) -> Keypair:
    return Keypair.from_bytes(bytes(json.loads((KEYS / name).read_text())))


DEPLOYER = load_kp("deployer.json")


# ----------------------------------------------------------------- RPC plumbing
def rpc(method, params):
    for attempt in range(6):
        req = urllib.request.Request(RPC, json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                                      "params": params}).encode(),
                                     {"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.loads(r.read())
        except Exception as e:  # 429s and timeouts on the public endpoint
            if attempt == 5:
                raise
            time.sleep(2 * (attempt + 1))
            continue
        if "error" in out:
            raise RuntimeError(f"{method}: {json.dumps(out['error'])[:2000]}")
        return out["result"]


def account(addr: Pubkey):
    v = rpc("getAccountInfo", [str(addr), {"encoding": "base64", "commitment": "confirmed"}])["value"]
    return None if v is None else (v["lamports"], base64.b64decode(v["data"][0]), v["owner"])


def data(addr: Pubkey):
    a = account(addr)
    return None if a is None else a[1]


def balance(token_account: Pubkey) -> int:
    d = data(token_account)
    return 0 if d is None else struct.unpack_from("<Q", d, 64)[0]


def rent(n: int) -> int:
    return rpc("getMinimumBalanceForRentExemption", [n])


def send(ixs, signers, cu=400_000, label=""):
    ixs = [set_compute_unit_limit(cu)] + list(ixs)
    bh = Hash.from_string(rpc("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"])
    tx = Transaction(signers, Message(ixs, signers[0].pubkey()), bh)
    raw = base64.b64encode(bytes(tx)).decode()
    sig = rpc("sendTransaction", [raw, {"encoding": "base64", "preflightCommitment": "confirmed"}])
    for _ in range(60):
        st = rpc("getSignatureStatuses", [[sig]])["value"][0]
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            if st.get("err"):
                raise RuntimeError(f"{label} failed on chain: {st['err']} {sig}")
            print(f"  ✓ {label}  {sig}")
            return sig
        time.sleep(1)
    raise RuntimeError(f"{label}: not confirmed in 60 s ({sig})")


def simulate(ixs, signers, cu=400_000):
    """(err, logs) without sending — for checks that a transaction is REFUSED."""
    ixs = [set_compute_unit_limit(cu)] + list(ixs)
    bh = Hash.from_string(rpc("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"])
    tx = Transaction(signers, Message(ixs, signers[0].pubkey()), bh)
    v = rpc("simulateTransaction", [base64.b64encode(bytes(tx)).decode(),
                                    {"encoding": "base64", "commitment": "confirmed"}])["value"]
    return v["err"], v["logs"] or []


class Remote(H.World):
    """Reuse the harness's instruction builders; no LiteSVM."""

    def __init__(self):
        pass


W = Remote()


def mint_to_ixs(owner: Pubkey, amounts, payer: Pubkey):
    ixs = [W.create_ata_ix(payer, owner, m) for m in H.LEG_MINTS]
    for m, amt in zip(H.LEG_MINTS, amounts):
        if amt:
            ixs.append(Instruction(H.TOKEN_2022, bytes([14]) + struct.pack("<QB", amt, H.LEG_DECIMALS), [
                AccountMeta(m, False, True),
                AccountMeta(H.ata(owner, m), False, True),
                AccountMeta(DEPLOYER.pubkey(), True, False),
            ]))
    return ixs


def legs_of(owner: Pubkey):
    return [balance(H.ata(owner, m)) for m in H.LEG_MINTS]


def units_of(owner: Pubkey):
    return balance(H.ata(owner, H.UNIT_MINT))


def supply():
    return struct.unpack_from("<Q", data(H.UNIT_MINT), 36)[0]


def holdings():
    return [balance(v) for v in H.VAULT_LEGS]


# ------------------------------------------------------------------ commands
def cmd_replicas():
    for leg, real, target in zip(H.LEGS, H.REAL_MINTS, H.LEG_MINTS):
        kp = load_kp(f"mint_{leg}.json")
        assert kp.pubkey() == target, f"{leg}: keypair {kp.pubkey()} != expected {target}"
        d = data(target)
        if d is None:
            ixs, meta = replica_ixs(DEPLOYER.pubkey(), target, DEPLOYER.pubkey(), real, rent)
            send(ixs, [DEPLOYER, kp], label=f"create {leg} replica ({meta[0]})")
            d = data(target)
        check_replica(d, real, DEPLOYER.pubkey())
        print(f"  {leg}: {target} matches the real mint's extensions")


def cmd_e2e():
    me = DEPLOYER
    # 1. initialize — skipped if the vault exists
    if data(H.VAULT) is None:
        send([W.ix_initialize(me, H.CAP_50K, "Permafrost (devnet)", "PFDEV", "")], [me], 300_000, "initialize")
    else:
        print("  · vault already initialized")
    # 2. seed
    if supply() == 0:
        send(mint_to_ixs(me.pubkey(), H.SEED_AMOUNTS, me.pubkey()), [me], label="mint seed legs to admin")
        send([W.ix_seed(me, H.SEED_AMOUNTS)], [me], label="seed")
    else:
        print("  · already seeded")
    assert balance(H.DEAD_UNITS) == H.SEED_UNITS, "seed units not at the dead-units account"
    assert data(H.DEAD_OWNER) is None, "dead owner PDA should hold no account"
    print(f"  seed units at the never-signing PDA: {balance(H.DEAD_UNITS):,}")

    # 3. a user deposits and redeems
    alice = Keypair.from_seed(bytes(32 * [7]))  # devnet-only throwaway
    if (account(alice.pubkey()) or (0,))[0] < 30_000_000:
        send([transfer(TransferParams(from_pubkey=me.pubkey(), to_pubkey=alice.pubkey(),
                                      lamports=50_000_000))], [me], label="fund alice 0.05 SOL")
    give = [3 * x for x in H.SEED_AMOUNTS]
    send(mint_to_ixs(alice.pubkey(), give, me.pubkey()), [me], label="mint replica legs to alice")
    h0, s0, a0 = holdings(), supply(), legs_of(alice.pubkey())
    ixs = [W.create_ata_ix(alice.pubkey(), alice.pubkey(), H.UNIT_MINT), W.ix_deposit(alice, give, 1)]
    send(ixs, [alice], label="alice deposits")
    got = units_of(alice.pubkey())
    paid = [a - b for a, b in zip(a0, legs_of(alice.pubkey()))]
    h1, s1 = holdings(), supply()
    assert s1 - s0 == got > 0
    assert [x - y for x, y in zip(h1, h0)] == paid
    # proportionality: every leg paid at most units/supply of holdings, rounded up by ≤1
    for p, h in zip(paid, h0):
        assert p * s0 >= got * h - s0 and p * s0 <= got * h + s0, f"deposit not pro rata: {paid} vs {h0}"
    print(f"  alice paid {paid} and got {got:,} units")

    a1 = legs_of(alice.pubkey())
    send([W.ix_redeem(alice, got)], [alice], label="alice redeems everything")
    back = [b - a for a, b in zip(a1, legs_of(alice.pubkey()))]
    assert units_of(alice.pubkey()) == 0 and supply() == s0
    assert all(b <= p for b, p in zip(back, paid)), "redeem returned more than was paid in"
    print(f"  alice got back {back} (paid {paid}; rounding favours the vault)")

    # 4. set_cap, then put it back
    send([W.ix_set_cap(me, 60_000 * H.UNIT)], [me], label="set_cap 60,000")
    assert struct.unpack_from("<Q", data(H.VAULT), 11)[0] == 60_000 * H.UNIT
    send([W.ix_set_cap(me, H.CAP_50K)], [me], label="set_cap back to 50,000")

    # 5. refusals on a real cluster
    err, logs = simulate([W.ix_set_cap(alice, 1)], [alice])
    assert err and any("Unauthorized" in l for l in logs), (err, logs[-3:])
    print("  ✓ a stranger's set_cap is refused (Unauthorized)")
    err, logs = simulate([W.ix_initialize(me)], [me], 300_000)
    assert err, "second initialize should fail"
    print("  ✓ a second initialize is refused")
    print("e2e OK")


def cmd_handoff(new_authority: str):
    """After `solana program set-upgrade-authority`, confirm the admin check followed."""
    pd = data(H.PROGRAM_DATA)
    auth = Pubkey(pd[13:45]) if pd[12] == 1 else None
    print(f"  upgrade authority on chain: {auth}")
    assert str(auth) == new_authority, f"expected {new_authority}"
    err, logs = simulate([W.ix_set_cap(DEPLOYER, H.CAP_50K)], [DEPLOYER])
    assert err and any("Unauthorized" in l for l in logs), "old key still accepted as admin!"
    print("  ✓ the old key is no longer admin (set_cap refused)")


def cmd_fund(owner: str, sol: str = "0"):
    o = Pubkey.from_string(owner)
    lam = int(float(sol) * 10**9)
    if lam:
        send([transfer(TransferParams(from_pubkey=DEPLOYER.pubkey(), to_pubkey=o, lamports=lam))],
             [DEPLOYER], label=f"send {sol} SOL")
    send(mint_to_ixs(o, [2 * x for x in H.SEED_AMOUNTS], DEPLOYER.pubkey()), [DEPLOYER],
         label="mint replica legs (2× the seed)")
    print(f"  {owner} now holds {legs_of(o)}")


def cmd_status():
    print(f"  rpc {RPC}\n  program {H.PROGRAM_ID}  deployer {DEPLOYER.pubkey()}")
    print(f"  deployer SOL {(account(DEPLOYER.pubkey()) or (0,))[0] / 1e9:.4f}")
    pd = data(H.PROGRAM_DATA)
    print(f"  program deployed: {pd is not None}" + (f", authority {Pubkey(pd[13:45])}" if pd and pd[12] else ""))
    for leg, m in zip(H.LEGS, H.LEG_MINTS):
        print(f"  {leg} replica {m}: {'exists' if data(m) else 'missing'}")
    if data(H.VAULT):
        print(f"  vault: supply {supply():,}, holdings {holdings()}")


if __name__ == "__main__":
    cmd, *args = sys.argv[1:] or ["status"]
    {"replicas": cmd_replicas, "e2e": cmd_e2e, "handoff": cmd_handoff,
     "fund": cmd_fund, "status": cmd_status}[cmd](*args)
