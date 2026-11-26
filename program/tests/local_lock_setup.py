"""For tools/test_app_lock.mjs, on a LOCAL validator (never devnet/mainnet):
bonus on (0.9%, max 0.9 PAIF, 30 d), pool funded, and a test user with free PAIF and
one open locked position. Run again to open a fresh position for the next run.

    PF_DEVNET=1 PF_RPC=http://localhost:8899 python3 -I tests/local_lock_setup.py USER_KEY.json
"""
import json, os, struct, sys
assert "localhost" in os.environ.get("PF_RPC", "") or "127.0.0.1" in os.environ.get("PF_RPC", ""), "local validator only"
from pathlib import Path
sys.path.insert(0, str(Path.home() / "pf/program/tests"))
import devnet as D, harness as H
from solders.keypair import Keypair
from solders.system_program import transfer, TransferParams
W, me = D.W, D.DEPLOYER
kp_path = Path(sys.argv[1])
if not kp_path.exists():
    kp_path.write_text(json.dumps(list(bytes(Keypair()))))
u = Keypair.from_bytes(bytes(json.loads(kp_path.read_text())))
if D.data(H.VAULT)[20] == 1 and D.balance(H.POOL) > 0:  # already set up: just open a fresh position
    u = Keypair.from_bytes(bytes(json.loads(kp_path.read_text())))
    give = [3 * x for x in H.SEED_AMOUNTS]
    D.send(D.mint_to_ixs(u.pubkey(), give, me.pubkey()), [me], label="user legs")
    nxt = struct.unpack_from("<Q", D.data(H.VAULT), 33)[0]
    D.send([W.ix_deposit_locked(u, give, 1, 365, 0, pid=nxt)], [u], label=f"user locks (position {nxt})")
    sys.exit(0)
D.send([W.ix_set_discount(me, True, 90, 9 * H.UNIT // 10, 30)], [me], label="bonus on: 0.9%, max 0.9 PAIF, 30 days")
amts = [10 * x for x in H.SEED_AMOUNTS]
D.send(D.mint_to_ixs(me.pubkey(), amts, me.pubkey()), [me], label="admin legs")
D.send([W.create_ata_ix(me.pubkey(), me.pubkey(), H.UNIT_MINT), W.ix_deposit(me, amts, 1)], [me], label="admin deposits")
D.send([W.ix_fund_pool(me, 5 * H.UNIT)], [me], label="fund pool 5 PAIF")
D.send([transfer(TransferParams(from_pubkey=me.pubkey(), to_pubkey=u.pubkey(), lamports=1_000_000_000))], [me], label="user SOL")
give = [6 * x for x in H.SEED_AMOUNTS]
D.send(D.mint_to_ixs(u.pubkey(), give, me.pubkey()), [me], label="user legs")
D.send([W.create_ata_ix(u.pubkey(), u.pubkey(), H.UNIT_MINT), W.ix_deposit(u, [x // 2 for x in give], 1)], [u], label="user deposits (free PAIF)")
nxt = struct.unpack_from("<Q", D.data(H.VAULT), 33)[0]
D.send([W.ix_deposit_locked(u, [x // 2 for x in give], 1, 365, 0, pid=nxt)], [u], label=f"user locks (position {nxt})")
pos = D.data(H.position_pda(nxt)); principal, bonus = struct.unpack_from("<QQ", pos, 8 + 8 + 32)
print(json.dumps({"user": str(u.pubkey()), "position": nxt, "principal": principal, "bonus": bonus,
                  "free_units": D.units_of(u.pubkey()), "pool": D.balance(H.POOL), "escrow": D.balance(H.ESCROW)}))
