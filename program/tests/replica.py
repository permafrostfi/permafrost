"""Build a devnet replica of a real xStock mint.

The four real xStock mints exist only on mainnet. For devnet we create look-alike
mints with the SAME Token-2022 extension set, in the same order, with the same
decimals and dividend multiplier — only the authorities differ (our test issuer
instead of Backed). The extension list is read from the mainnet fixture, so a
replica cannot silently drift from the real thing; `check_replica()` compares the
two byte layouts after creation.

Used two ways:
  * harness.World(replicas=True) builds replicas inside LiteSVM, so the whole test
    suite can run against the `devnet` build of the program;
  * tests/devnet.py sends the same instructions to devnet.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from pathlib import Path

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.system_program import CreateAccountParams, create_account

TOKEN_2022 = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
FIX = Path(__file__).resolve().parent.parent / "fixtures"
ZERO = bytes(32)

# Token-2022 extension type ids (spl_token_2022::extension::ExtensionType).
EXT = {
    4: "ConfidentialTransferMint", 6: "DefaultAccountState", 12: "PermanentDelegate",
    14: "TransferHook", 18: "MetadataPointer", 19: "TokenMetadata",
    25: "ScaledUiAmount", 26: "Pausable",
}
FIXED_LEN = {4: 65, 6: 1, 12: 32, 14: 64, 18: 64, 25: 56, 26: 33}


def mint_fixture(real_mint: Pubkey) -> bytes:
    rec = json.loads((FIX / f"mint_{real_mint}.json").read_text())["account"]
    return base64.b64decode(rec["data"][0])


def tlv(data: bytes):
    """[(type, value_bytes)] for a Token-2022 mint account."""
    out, off = [], 166
    while off + 4 <= len(data):
        t, n = struct.unpack_from("<HH", data, off)
        if t == 0 and n == 0:
            break
        out.append((t, data[off + 4: off + 4 + n]))
        off += 4 + n
    return out


def real_metadata(data: bytes):
    """(name, symbol, uri) from the TokenMetadata extension."""
    v = dict(tlv(data))[19]
    off = 64  # update_authority(32) + mint(32)
    vals = []
    for _ in range(3):
        n = struct.unpack_from("<I", v, off)[0]
        vals.append(v[off + 4: off + 4 + n].decode())
        off += 4 + n
    return tuple(vals)


def borsh_str(s: str) -> bytes:
    b = s.encode()
    return struct.pack("<I", len(b)) + b


def replica_ixs(payer: Pubkey, mint: Pubkey, issuer: Pubkey, real_mint: Pubkey, rent_fn):
    """Instructions that create `mint` as a replica of `real_mint`, issuer = `issuer`.

    `rent_fn(n_bytes) -> lamports`. Returns (instructions, (name, symbol, uri)).
    The mint keypair and payer must sign; `issuer` must sign the metadata step.
    """
    data = mint_fixture(real_mint)
    exts = tlv(data)
    types = [t for t, _ in exts]
    assert set(types) <= set(EXT), f"unexpected extension in real mint: {types}"
    assert types[-1] == 19, "TokenMetadata expected last (variable-length)"
    decimals = data[44]
    iss = bytes(issuer)
    m = lambda d: Instruction(TOKEN_2022, d, [AccountMeta(mint, False, True)])  # noqa: E731

    ext_ix = {}
    for t, v in exts:
        if t == 18:   # MetadataPointer: authority, address = the mint itself
            ext_ix[t] = m(bytes([39, 0]) + iss + bytes(mint))
        elif t == 12:  # PermanentDelegate
            ext_ix[t] = m(bytes([35]) + iss)
        elif t == 6:   # DefaultAccountState: same state as the real mint
            ext_ix[t] = m(bytes([28, 0, v[0]]))
        elif t == 25:  # ScaledUiAmount: authority + the real mint's latest multiplier
            mult = struct.unpack_from("<d", v, 48)[0]  # new_multiplier (its timestamp has passed)
            ext_ix[t] = m(bytes([43, 0]) + iss + struct.pack("<d", mult))
        elif t == 26:  # Pausable
            ext_ix[t] = m(bytes([44, 0]) + iss)
        elif t == 4:   # ConfidentialTransferMint: authority, auto_approve, auditor
            assert v[32] == 0 and v[33:65] == ZERO, "real mint auto-approves or has an auditor"
            ext_ix[t] = m(bytes([27, 0]) + iss + b"\x00" + ZERO)
        elif t == 14:  # TransferHook: authority, program = none (same as real)
            assert v[32:64] == ZERO, "real mint has a transfer-hook program set"
            ext_ix[t] = m(bytes([36, 0]) + iss + ZERO)

    fixed_space = 166 + sum(4 + FIXED_LEN[t] for t in types if t != 19)
    name, symbol, uri = real_metadata(data)
    name, symbol = f"{name} (devnet replica)", symbol
    meta_len = 4 + 32 + 32 + len(borsh_str(name)) + len(borsh_str(symbol)) + len(borsh_str(uri)) + 4
    lamports = rent_fn(fixed_space + meta_len)

    ixs = [create_account(CreateAccountParams(
        from_pubkey=payer, to_pubkey=mint, lamports=lamports, space=fixed_space, owner=TOKEN_2022))]
    ixs += [ext_ix[t] for t in types if t != 19]
    # InitializeMint2: decimals, mint authority, freeze authority (real mints have one).
    ixs.append(m(bytes([20, decimals]) + iss + b"\x01" + iss))
    disc = hashlib.sha256(b"spl_token_metadata_interface:initialize_account").digest()[:8]
    ixs.append(Instruction(TOKEN_2022, disc + borsh_str(name) + borsh_str(symbol) + borsh_str(uri), [
        AccountMeta(mint, False, True),     # metadata (the mint itself)
        AccountMeta(issuer, False, False),  # update authority
        AccountMeta(mint, False, False),    # mint
        AccountMeta(issuer, True, False),   # mint authority (signs)
    ]))
    return ixs, (name, symbol, uri)


def check_replica(replica_data: bytes, real_mint: Pubkey, issuer: Pubkey):
    """Raise unless the replica matches the real mint in everything but authorities."""
    real = mint_fixture(real_mint)
    assert replica_data[44] == real[44], "decimals differ"
    rt, pt = tlv(real), tlv(replica_data)
    assert [t for t, _ in rt] == [t for t, _ in pt], f"extension order differs: {[t for t,_ in rt]} vs {[t for t,_ in pt]}"
    for (t, rv), (_, pv) in zip(rt, pt):
        if t != 19:
            assert len(rv) == len(pv), f"extension {EXT[t]} length differs"
    d = dict(pt)
    assert dict(rt)[6] == d[6], "default account state differs"
    assert d[26][32] == 0, "replica starts paused"
    assert d[12] == bytes(issuer), "permanent delegate is not the issuer"
    assert struct.unpack_from("<d", d[25], 32) == struct.unpack_from("<d", dict(rt)[25], 48), "multiplier differs"
    assert replica_data[46:50] == real[46:50] and replica_data[0:4] == real[0:4], "authority options differ"
