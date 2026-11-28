"""Test harness for the Permafrost v1 program.

Runs the real compiled program inside LiteSVM (an in-process Solana runtime) with
mainnet's feature set, against:

  * the REAL Token-2022 and Associated Token programs, dumped from mainnet
    (fixtures/token_2022.so, fixtures/associated_token.so) — not whatever version
    ships with the test runtime, so the four mints' newer extensions (pausable,
    scaled UI amount) behave exactly as they do live;
  * the REAL four xStock mint accounts, copied byte-for-byte from mainnet
    (fixtures/mint_*.json) — real addresses, real extension set, real dividend
    multipliers — with exactly one change: every Backed authority key is swapped
    for a test "issuer" key. That lets the tests mint test tokens, and lets them
    ACT AS THE ISSUER: drain a leg with the permanent delegate, pause a token,
    freeze an account, move a multiplier. Nothing else in the mint bytes changes.

Our program is installed as an UPGRADEABLE program whose ProgramData names a test
admin key as upgrade authority, so the on-chain admin check (layout choice L6) is
exercised for real rather than stubbed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from pathlib import Path

from solders.account import Account
from solders.compute_budget import set_compute_unit_limit
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.litesvm import LiteSVM
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FIX = ROOT / "fixtures"

# The devnet build and the mainnet build declare different program ids.
PROGRAM_ID = Pubkey.from_string("48N2qchckkoWhmT9anrDU7bRt83QPLNBqj9JLZA7kEF2" if os.environ.get("PF_DEVNET") == "1"
                                else "EDp2AwfRiWEbM3K1bF5SY5SJGcFTjBZRPENKXk7Eoy61")
TOKEN_2022 = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ATA_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM = Pubkey.from_string("11111111111111111111111111111111")
LOADER_V3 = Pubkey.from_string("BPFLoaderUpgradeab1e11111111111111111111111")
INCINERATOR = Pubkey.from_string("1nc1nerator11111111111111111111111111111111")

LEGS = ["NVDAx", "GOOGLx", "MSFTx", "METAx"]
REAL_MINTS = [Pubkey.from_string(s) for s in (
    "Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh",
    "XsCPL9dNWBMvFtTmwcCA5v3xWPSMEBCszbQdiLLq6aN",
    "XspzcW1PRtgf6Wj92HCiZdjzKCyFekVD8P5Ueh3dRMX",
    "Xsa62P5mvPszXL1krVUnU5ar38bBSVcWAB6fmPCo5Zu",
)]
# Devnet replicas of the four mints (same addresses as `feature = "devnet"` in lib.rs).
DEVNET_MINTS = [Pubkey.from_string(s) for s in (
    "A15QnvzoNKst2X2aVaQANvr48kzKVRyYHzP6DXHjppFz",
    "5dy1EDc71EC9tsJzbbY78ZTh1vdhGMeXQNyPiUKtpGzn",
    "JCs5xPjRmSBHtYKAWB6PviCAE5TUMPV4UibbKkPpRMGP",
    "DRcvvyhjF7chPrLbUyoX8DG67j1HYix2WKkHEaHUHHB9",
)]
# PF_DEVNET=1: test the `devnet` build against replica mints that the harness
# CREATES with real Token-2022 instructions (tests/replica.py), instead of the
# byte-copied mainnet mints. Same suite, both builds.
DEVNET = os.environ.get("PF_DEVNET") == "1"
# PF_V1=1: build instructions for the v1 program (admin = upgrade authority for
# set_cap; no init_v3 at launch). Default on this branch: v3.
V1_LAYOUT = os.environ.get("PF_V1") == "1"
LEG_MINTS = DEVNET_MINTS if DEVNET else REAL_MINTS
# The devnet build goes to its own folder so it can never be mistaken for the mainnet one.
PROGRAM_SO = ROOT / "target" / ("deploy-devnet" if DEVNET else "deploy") / "permafrost.so"
LEG_DECIMALS = 8
UNIT = 10**9
SEED_UNITS = 25 * UNIT
CAP_50K = 50_000 * UNIT

# Backed's real authority keys inside the mint bytes — each is replaced by ISSUER.
BACKED_KEYS = {
    "mint authority": "7pt9tkctJPK7PPNQJ77GKg8ZffSF6QxoMiCFYHxrtaCj",
    "permanent delegate / hook / metadata": "5aMNNLQJwAEeoemTEMkv5NVjqKwvvefRYCQ5Z67HFvEq",
    "freeze / pause": "JDq14BWvqCRFNu1krb12bcRpbGtJZ1FLEakMw6FdxJNs",
    "scaled-UI multiplier": "S7vYFFWH6BjJyEsdrPQpqpYTqLTrPRK6KW3VwsJuRaS",
}


def det_keypair(label: str) -> Keypair:
    """Deterministic, worthless test keys — no keypair files to leak or commit."""
    return Keypair.from_seed(hashlib.sha256(b"permafrost-test:" + label.encode()).digest())


ADMIN = det_keypair("admin")      # the program's upgrade authority in tests
ISSUER = det_keypair("issuer")    # stands in for Backed on all four mints


def pda(seeds, program=PROGRAM_ID) -> Pubkey:
    return Pubkey.find_program_address(seeds, program)[0]


VAULT = pda([b"vault"])
UNIT_MINT = pda([b"unit_mint"])
PROGRAM_DATA = pda([bytes(PROGRAM_ID)], LOADER_V3)


def ata(owner: Pubkey, mint: Pubkey) -> Pubkey:
    return Pubkey.find_program_address([bytes(owner), bytes(TOKEN_2022), bytes(mint)], ATA_PROGRAM)[0]


VAULT_LEGS = [ata(VAULT, m) for m in LEG_MINTS]
# L4 (revised): seed units are held by a program PDA that never signs — NOT the
# incinerator, which Token-2022 lets anyone burn from (see test A20).
DEAD_OWNER = pda([b"dead"])
DEAD_UNITS = ata(DEAD_OWNER, UNIT_MINT)

# v2 — locked bonus
POOL = pda([b"pool"])
ESCROW = pda([b"escrow"])
DAY = 86_400


# v3 — fees go to this fixed wallet's PAIF account (FEE_OWNER in lib.rs).
FEE_OWNER = Pubkey.from_string("E3FTvAEzUU8v91cFCbykxQzcqRo7Zc2Jp3HxtwTZmYyt")
FEE_ACCOUNT = ata(FEE_OWNER, UNIT_MINT)


def position_pda(pid: int) -> Pubkey:
    return pda([b"position", struct.pack("<Q", pid)])

# Anchor error codes for this program (declaration order in PfErr, from 6000).
ERR = {n: 6000 + i for i, n in enumerate([
    "Unauthorized", "AlreadySeeded", "CapExceeded", "SlippageUnits", "SlippageLeg",
    "BadAccount", "MetadataTooLong", "Overflow", "UnexpectedFee", "CoreUnseeded",
    "CoreEmptyLeg", "CoreDust", "CoreBadRedeem", "CoreBadSeed", "CoreArithmetic",
    # v2
    "DiscountsNotReady", "DiscountsOff", "RateTooHigh", "LockOutOfRange",
    "LockLongerThanAccepted", "BonusBelowAccepted", "ZeroAmount", "BadAmount",
    "NotMatured", "AlreadyMatured", "NotPositionOwner",
    # v3
    "FeeTooHigh", "BadFeeAccount",
])}


def anchor_disc(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


def borsh_str(s: str) -> bytes:
    b = s.encode()
    return struct.pack("<I", len(b)) + b


class TxResult:
    def __init__(self, raw, ok: bool):
        self.raw = raw
        self.ok = ok
        meta = raw if ok else _call(raw.meta)
        self.logs = list(_call(meta.logs))
        self.cu = _call(meta.compute_units_consumed)
        self.err = None if ok else _call(raw.err)

    def custom_code(self):
        """The custom program error code, if the failure was one."""
        s = str(self.err)
        if "Custom(" in s:
            return int(s.split("Custom(")[1].split(")")[0])
        return None

    def anchor_error(self):
        for line in self.logs:
            if "AnchorError" in line or "Error Code:" in line:
                return line.split("Program log: ")[-1]
        return None

    def reason(self):
        return self.anchor_error() or str(self.err)


class World:
    """One fresh chain per test: programs, real mints (issuer-swapped), our program."""

    def __init__(self, upgrade_authority: Pubkey | None = None):
        self.svm = LiteSVM().with_mainnet_features()
        self.svm.add_program(TOKEN_2022, (FIX / "token_2022.so").read_bytes())
        self.svm.add_program(ATA_PROGRAM, (FIX / "associated_token.so").read_bytes())
        self._install_program(ADMIN.pubkey() if upgrade_authority is None else upgrade_authority)
        for kp in (ADMIN, ISSUER):
            self.svm.airdrop(kp.pubkey(), 100 * 10**9)
        for real, m in zip(REAL_MINTS, LEG_MINTS):
            if DEVNET:
                self._create_replica(real, m)
            else:
                self._install_mint(m)

    # ---------------------------------------------------------- installation
    def _install_program(self, authority: Pubkey):
        elf = PROGRAM_SO.read_bytes()
        # UpgradeableLoaderState::ProgramData = tag 3, slot u64, Option<Pubkey>, ELF.
        # The metadata header is always 45 bytes; the ELF starts at offset 45.
        pd = struct.pack("<IQ", 3, 0) + b"\x01" + bytes(authority) + elf
        rent = self.svm.minimum_balance_for_rent_exemption(len(pd))
        self.svm.set_account(PROGRAM_DATA, Account(rent, pd, LOADER_V3, False))
        prog = struct.pack("<I", 2) + bytes(PROGRAM_DATA)  # tag 2 = Program
        self.svm.set_account(PROGRAM_ID, Account(10**9, prog, LOADER_V3, True))

    def _install_mint(self, mint: Pubkey):
        rec = json.loads((FIX / f"mint_{mint}.json").read_text())["account"]
        data = bytearray(base64.b64decode(rec["data"][0]))
        swapped = 0
        for key in BACKED_KEYS.values():
            k = bytes(Pubkey.from_string(key))
            n = data.count(k)
            data = bytearray(bytes(data).replace(k, bytes(ISSUER.pubkey())))
            swapped += n
        assert swapped >= 6, f"expected Backed keys in {mint}, swapped {swapped}"
        self.svm.set_account(mint, Account(rec["lamports"], bytes(data), TOKEN_2022, False))

    def _create_replica(self, real: Pubkey, target: Pubkey):
        """Create a replica with real instructions, then move it to its devnet address.

        The devnet mint keypairs are not in the repo, so the replica is created at a
        throwaway address and its bytes (which name the mint's own address in the
        metadata pointer and metadata) are moved to the devnet address.
        """
        from replica import check_replica, replica_ixs
        tmp = det_keypair(f"replica:{real}")
        ixs, _ = replica_ixs(ISSUER.pubkey(), tmp.pubkey(), ISSUER.pubkey(), real,
                             self.svm.minimum_balance_for_rent_exemption)
        r = self.send(ixs, [ISSUER, tmp])
        assert r.ok, f"replica creation failed: {r.reason()} {r.logs[-5:]}"
        acc = self.svm.get_account(tmp.pubkey())
        data = bytes(acc.data).replace(bytes(tmp.pubkey()), bytes(target))
        check_replica(data, real, ISSUER.pubkey())
        self.svm.set_account(target, Account(acc.lamports, data, TOKEN_2022, False))

    # ---------------------------------------------------------------- basics
    def send(self, ixs, signers, cu: int = 1_400_000) -> TxResult:
        ixs = [set_compute_unit_limit(cu)] + list(ixs)
        payer = signers[0].pubkey()
        tx = Transaction(signers, Message(ixs, payer), self.svm.latest_blockhash())
        r = self.svm.send_transaction(tx)
        self.svm.expire_blockhash()
        return TxResult(r, type(r).__name__ == "TransactionMetadata")

    def new_user(self, label: str, sol: int = 10) -> Keypair:
        kp = det_keypair(label)
        self.svm.airdrop(kp.pubkey(), sol * 10**9)
        return kp

    def account_data(self, addr: Pubkey) -> bytes | None:
        a = self.svm.get_account(addr)
        return None if a is None else bytes(a.data)

    def balance(self, token_account: Pubkey) -> int:
        d = self.account_data(token_account)
        return 0 if d is None else struct.unpack_from("<Q", d, 64)[0]

    def supply(self, mint: Pubkey = UNIT_MINT) -> int:
        return struct.unpack_from("<Q", self.account_data(mint), 36)[0]

    def holdings(self):
        return [self.balance(a) for a in VAULT_LEGS]

    def vault_cap(self) -> int:
        d = self.account_data(VAULT)
        return struct.unpack_from("<Q", d, 8 + 3)[0]

    # --------------------------------------------------------- token helpers
    def create_ata_ix(self, payer: Pubkey, owner: Pubkey, mint: Pubkey) -> Instruction:
        return Instruction(ATA_PROGRAM, bytes([1]), [  # 1 = CreateIdempotent
            AccountMeta(payer, True, True),
            AccountMeta(ata(owner, mint), False, True),
            AccountMeta(owner, False, False),
            AccountMeta(mint, False, False),
            AccountMeta(SYSTEM, False, False),
            AccountMeta(TOKEN_2022, False, False),
        ])

    def mint_legs(self, user: Keypair, amounts):
        """Issuer mints real-mint test tokens to the user's ATAs."""
        ixs = [self.create_ata_ix(user.pubkey(), user.pubkey(), m) for m in LEG_MINTS]
        r = self.send(ixs, [user])
        assert r.ok, r.reason()
        mix = []
        for m, amt in zip(LEG_MINTS, amounts):
            if amt:
                mix.append(Instruction(TOKEN_2022, bytes([14]) + struct.pack("<QB", amt, LEG_DECIMALS), [
                    AccountMeta(m, False, True),
                    AccountMeta(ata(user.pubkey(), m), False, True),
                    AccountMeta(ISSUER.pubkey(), True, False),
                ]))
        if mix:
            r = self.send(mix, [ISSUER])
            assert r.ok, r.reason()

    def ensure_unit_ata(self, user: Keypair):
        r = self.send([self.create_ata_ix(user.pubkey(), user.pubkey(), UNIT_MINT)], [user])
        assert r.ok, r.reason()

    def transfer_checked_ix(self, src, mint, dst, authority, amount, decimals=LEG_DECIMALS):
        return Instruction(TOKEN_2022, bytes([12]) + struct.pack("<QB", amount, decimals), [
            AccountMeta(src, False, True),
            AccountMeta(mint, False, False),
            AccountMeta(dst, False, True),
            AccountMeta(authority, True, False),
        ])

    # ------------------------------------------------- issuer powers (tests)
    def issuer_drain(self, leg: int, amount: int, to_owner: Pubkey) -> TxResult:
        """Permanent delegate moves tokens out of the vault without the vault's consent."""
        dst = ata(to_owner, LEG_MINTS[leg])
        ixs = [self.create_ata_ix(ISSUER.pubkey(), to_owner, LEG_MINTS[leg]),
               self.transfer_checked_ix(VAULT_LEGS[leg], LEG_MINTS[leg], dst, ISSUER.pubkey(), amount)]
        return self.send(ixs, [ISSUER])

    def issuer_pause(self, leg: int, pause: bool = True) -> TxResult:
        # TokenInstruction 44 = PausableExtension; sub 1 = Pause, 2 = Resume.
        ix = Instruction(TOKEN_2022, bytes([44, 1 if pause else 2]), [
            AccountMeta(LEG_MINTS[leg], False, True),
            AccountMeta(ISSUER.pubkey(), True, False),
        ])
        return self.send([ix], [ISSUER])

    def issuer_freeze(self, account: Pubkey, leg: int, freeze: bool = True) -> TxResult:
        # 10 = FreezeAccount, 11 = ThawAccount
        ix = Instruction(TOKEN_2022, bytes([10 if freeze else 11]), [
            AccountMeta(account, False, True),
            AccountMeta(LEG_MINTS[leg], False, False),
            AccountMeta(ISSUER.pubkey(), True, False),
        ])
        return self.send([ix], [ISSUER])

    def issuer_set_multiplier(self, leg: int, multiplier: float, effective_ts: int = 0) -> TxResult:
        # TokenInstruction 43 = ScaledUiAmountExtension; sub 1 = UpdateMultiplier(f64, i64).
        ix = Instruction(TOKEN_2022, bytes([43, 1]) + struct.pack("<dq", multiplier, effective_ts), [
            AccountMeta(LEG_MINTS[leg], False, True),
            AccountMeta(ISSUER.pubkey(), True, False),
        ])
        return self.send([ix], [ISSUER])

    # ---------------------------------------------------- program instructions
    def admin_metas(self, admin: Pubkey):
        return [
            AccountMeta(admin, True, True),
            AccountMeta(PROGRAM_ID, False, False),
            AccountMeta(PROGRAM_DATA, False, False),
        ]

    def ix_initialize(self, admin: Keypair, cap=CAP_50K, name="Permafrost", symbol="PERMA",
                      uri="", override=None) -> Instruction:
        metas = self.admin_metas(admin.pubkey()) + [
            AccountMeta(VAULT, False, True),
            AccountMeta(UNIT_MINT, False, True),
            *[AccountMeta(m, False, False) for m in LEG_MINTS],
            *[AccountMeta(v, False, True) for v in VAULT_LEGS],
            AccountMeta(DEAD_OWNER, False, False),
            AccountMeta(DEAD_UNITS, False, True),
            AccountMeta(TOKEN_2022, False, False),
            AccountMeta(ATA_PROGRAM, False, False),
            AccountMeta(SYSTEM, False, False),
        ]
        data = anchor_disc("initialize") + struct.pack("<Q", cap) + borsh_str(name) + borsh_str(symbol) + borsh_str(uri)
        return Instruction(PROGRAM_ID, data, _apply(metas, override))

    def ix_seed(self, admin: Keypair, amounts, override=None) -> Instruction:
        metas = self.admin_metas(admin.pubkey()) + [
            AccountMeta(VAULT, False, False),
            AccountMeta(UNIT_MINT, False, True),
            *[AccountMeta(m, False, False) for m in LEG_MINTS],
            *[AccountMeta(v, False, True) for v in VAULT_LEGS],
            *[AccountMeta(ata(admin.pubkey(), m), False, True) for m in LEG_MINTS],
            AccountMeta(DEAD_OWNER, False, False),
            AccountMeta(DEAD_UNITS, False, True),
            AccountMeta(TOKEN_2022, False, False),
        ]
        metas[0] = AccountMeta(admin.pubkey(), True, False)  # Seed's admin is not mut
        data = anchor_disc("seed") + struct.pack("<4Q", *amounts)
        return Instruction(PROGRAM_ID, data, _apply(metas, override))

    def user_metas(self, user: Pubkey, user_unit=None):
        # v3: the vault is written (fee clock) and the fee account comes last. A v1
        # program ignores both differences, so the same builders serve v1 and v3.
        return [
            AccountMeta(user, True, False),
            AccountMeta(VAULT, False, True),
            AccountMeta(UNIT_MINT, False, True),
            AccountMeta(user_unit or ata(user, UNIT_MINT), False, True),
            *[AccountMeta(m, False, False) for m in LEG_MINTS],
            *[AccountMeta(v, False, True) for v in VAULT_LEGS],
            *[AccountMeta(ata(user, m), False, True) for m in LEG_MINTS],
            AccountMeta(TOKEN_2022, False, False),
            AccountMeta(FEE_ACCOUNT, False, True),
        ]

    def ix_deposit(self, user: Keypair, max_in, min_units_out=0, override=None) -> Instruction:
        data = anchor_disc("deposit") + struct.pack("<4QQ", *max_in, min_units_out)
        return Instruction(PROGRAM_ID, data, _apply(self.user_metas(user.pubkey()), override))

    def ix_redeem(self, user: Keypair, units, min_out=(0, 0, 0, 0), override=None) -> Instruction:
        data = anchor_disc("redeem") + struct.pack("<Q4Q", units, *min_out)
        return Instruction(PROGRAM_ID, data, _apply(self.user_metas(user.pubkey()), override))

    def ix_set_cap(self, admin: Keypair, new_cap, override=None) -> Instruction:
        if V1_LAYOUT:
            metas = self.admin_metas(admin.pubkey()) + [AccountMeta(VAULT, False, True)]
        else:  # v3: the stored admin key; no program / program data accounts
            metas = [AccountMeta(admin.pubkey(), True, False), AccountMeta(VAULT, False, True)]
        metas[0] = AccountMeta(admin.pubkey(), True, False)
        data = anchor_disc("set_cap") + struct.pack("<Q", new_cap)
        return Instruction(PROGRAM_ID, data, _apply(metas, override))

    # ------------------------------------------------- v2: locked bonus
    def vault_v2(self) -> dict:
        d = self.account_data(VAULT)
        o = 8 + 3 + 8
        ready, on, bp, days, mx, nxt = struct.unpack_from("<??HHQQ", d, o)
        return dict(ready=ready, on=on, locked_bp=bp, lock_days=days, max_bonus=mx, next_position=nxt)

    def position(self, pid: int):
        d = self.account_data(position_pda(pid))
        if d is None:
            return None
        pid_, owner, principal, bonus, opened, unlock = struct.unpack_from("<Q32sQQqq", d, 8)
        return dict(id=pid_, owner=Pubkey(owner), principal=principal, bonus=bonus,
                    opened_at=opened, unlock_at=unlock)

    def now(self) -> int:
        return self.svm.get_clock().unix_timestamp

    def warp(self, seconds: int):
        c = self.svm.get_clock()
        c.unix_timestamp = c.unix_timestamp + seconds
        c.slot = c.slot + max(1, seconds // 1)
        self.svm.set_clock(c)
        self.svm.expire_blockhash()

    def ix_init_v3(self, admin: Keypair, override=None) -> Instruction:
        metas = self.admin_metas(admin.pubkey()) + [
            AccountMeta(VAULT, False, True),
            AccountMeta(UNIT_MINT, False, False),
            AccountMeta(FEE_OWNER, False, False),
            AccountMeta(FEE_ACCOUNT, False, True),
            AccountMeta(POOL, False, True),
            AccountMeta(ESCROW, False, True),
            AccountMeta(TOKEN_2022, False, False),
            AccountMeta(ATA_PROGRAM, False, False),
            AccountMeta(SYSTEM, False, False),
        ]
        return Instruction(PROGRAM_ID, anchor_disc("init_v3"), _apply(metas, override))

    def ix_set_fees(self, admin: Keypair, entry_bp=0, exit_bp=0, stream_bp=0, override=None) -> Instruction:
        metas = [AccountMeta(admin.pubkey(), True, False), AccountMeta(VAULT, False, True),
                 AccountMeta(UNIT_MINT, False, True), AccountMeta(FEE_ACCOUNT, False, True),
                 AccountMeta(TOKEN_2022, False, False)]
        data = anchor_disc("set_fees") + struct.pack("<HHH", entry_bp, exit_bp, stream_bp)
        return Instruction(PROGRAM_ID, data, _apply(metas, override))

    def ix_accrue(self, override=None) -> Instruction:
        metas = [AccountMeta(VAULT, False, True), AccountMeta(UNIT_MINT, False, True),
                 AccountMeta(FEE_ACCOUNT, False, True), AccountMeta(TOKEN_2022, False, False)]
        return Instruction(PROGRAM_ID, anchor_disc("accrue"), _apply(metas, override))

    def ix_propose_admin(self, admin: Keypair, new_admin: Pubkey) -> Instruction:
        metas = [AccountMeta(admin.pubkey(), True, False), AccountMeta(VAULT, False, True)]
        return Instruction(PROGRAM_ID, anchor_disc("propose_admin") + bytes(new_admin), metas)

    def ix_accept_admin(self, new_admin: Keypair) -> Instruction:
        metas = [AccountMeta(new_admin.pubkey(), True, False), AccountMeta(VAULT, False, True)]
        return Instruction(PROGRAM_ID, anchor_disc("accept_admin"), metas)

    def fees(self) -> dict:
        d = self.account_data(VAULT)
        o = 8 + 3 + 8 + 24 + 64
        e, x, st, pe, px, ps, pat, last = struct.unpack_from("<HHHHHHqq", d, o)
        return dict(entry=e, exit=x, stream=st, pending=(pe, px, ps), pending_at=pat, last_accrual=last)

    def admin_key(self) -> Pubkey:
        return Pubkey(self.account_data(VAULT)[8 + 3 + 8 + 24: 8 + 3 + 8 + 24 + 32])

    def ix_set_discount(self, admin: Keypair, on=True, locked_bp=1000, max_bonus=10 * UNIT,
                        lock_days=120, override=None) -> Instruction:
        metas = [AccountMeta(admin.pubkey(), True, False), AccountMeta(VAULT, False, True)]
        data = anchor_disc("set_discount") + struct.pack("<?HQH", on, locked_bp, max_bonus, lock_days)
        return Instruction(PROGRAM_ID, data, _apply(metas, override))

    def ix_fund_pool(self, funder: Keypair, amount: int, src=None, override=None) -> Instruction:
        metas = [
            AccountMeta(funder.pubkey(), True, False),
            AccountMeta(VAULT, False, False),
            AccountMeta(UNIT_MINT, False, False),
            AccountMeta(src or ata(funder.pubkey(), UNIT_MINT), False, True),
            AccountMeta(POOL, False, True),
            AccountMeta(TOKEN_2022, False, False),
        ]
        return Instruction(PROGRAM_ID, anchor_disc("fund_pool") + struct.pack("<Q", amount), _apply(metas, override))

    def ix_withdraw_pool(self, admin: Keypair, amount: int, destination: Pubkey, override=None) -> Instruction:
        metas = [
            AccountMeta(admin.pubkey(), True, False),
            AccountMeta(VAULT, False, False),
            AccountMeta(UNIT_MINT, False, False),
            AccountMeta(POOL, False, True),
            AccountMeta(destination, False, True),
            AccountMeta(TOKEN_2022, False, False),
        ]
        return Instruction(PROGRAM_ID, anchor_disc("withdraw_pool") + struct.pack("<Q", amount), _apply(metas, override))

    def ix_deposit_locked(self, user: Keypair, max_in, min_units_out=0, max_lock_days=365,
                          min_bonus=0, pid=None, override=None) -> Instruction:
        pid = self.vault_v2()["next_position"] if pid is None else pid
        u = user.pubkey()
        metas = [
            AccountMeta(u, True, True),
            AccountMeta(VAULT, False, True),
            AccountMeta(UNIT_MINT, False, True),
            AccountMeta(POOL, False, True),
            AccountMeta(ESCROW, False, True),
            AccountMeta(position_pda(pid), False, True),
            *[AccountMeta(m, False, False) for m in LEG_MINTS],
            *[AccountMeta(v, False, True) for v in VAULT_LEGS],
            *[AccountMeta(ata(u, m), False, True) for m in LEG_MINTS],
            AccountMeta(TOKEN_2022, False, False),
            AccountMeta(SYSTEM, False, False),
            AccountMeta(FEE_ACCOUNT, False, True),
        ]
        data = anchor_disc("deposit_locked") + struct.pack("<4QQHQ", *max_in, min_units_out, max_lock_days, min_bonus)
        return Instruction(PROGRAM_ID, data, _apply(metas, override))

    def _settle(self, name, owner: Keypair, pid: int, dest=None, override=None) -> Instruction:
        o = owner.pubkey()
        metas = [
            AccountMeta(o, True, True),
            AccountMeta(VAULT, False, False),
            AccountMeta(UNIT_MINT, False, False),
            AccountMeta(POOL, False, True),
            AccountMeta(ESCROW, False, True),
            AccountMeta(position_pda(pid), False, True),
            AccountMeta(dest or ata(o, UNIT_MINT), False, True),
            AccountMeta(TOKEN_2022, False, False),
        ]
        return Instruction(PROGRAM_ID, anchor_disc(name), _apply(metas, override))

    def ix_claim_locked(self, owner, pid, dest=None, override=None):
        return self._settle("claim_locked", owner, pid, dest, override)

    def ix_exit_locked(self, owner, pid, dest=None, override=None):
        return self._settle("exit_locked", owner, pid, dest, override)

    def init_v3(self):
        r = self.send([self.ix_init_v3(ADMIN)], [ADMIN])
        assert r.ok, "init_v3: " + r.reason()
        return self

    def enable_discounts(self, locked_bp=1000, max_bonus=10 * UNIT, lock_days=120, fund_units=0):
        r = self.send([self.ix_set_discount(ADMIN, True, locked_bp, max_bonus, lock_days)], [ADMIN])
        assert r.ok, "set_discount: " + r.reason()
        if fund_units:
            # The creator gets units the ordinary way, then parks them in the pool.
            amts = [x * 40 for x in SEED_AMOUNTS]
            self.mint_legs(ADMIN, amts)
            self.ensure_unit_ata(ADMIN)
            r = self.send([self.ix_deposit(ADMIN, amts)], [ADMIN])
            assert r.ok, "creator deposit: " + r.reason()
            r = self.send([self.ix_fund_pool(ADMIN, fund_units)], [ADMIN])
            assert r.ok, "fund_pool: " + r.reason()
        return self

    # ------------------------------------------------------------ fixtures
    def launch(self, seed_amounts=None, cap=CAP_50K, v3=None):
        """Initialise + seed, as the mainnet launch would; then (v3 builds) init_v3."""
        v3 = (not V1_LAYOUT) if v3 is None else v3
        seed_amounts = seed_amounts or SEED_AMOUNTS
        r = self.send([self.ix_initialize(ADMIN, cap)], [ADMIN])
        assert r.ok, "initialize: " + r.reason()
        self.mint_legs(ADMIN, seed_amounts)
        r = self.send([self.ix_seed(ADMIN, seed_amounts)], [ADMIN])
        assert r.ok, "seed: " + r.reason()
        if v3:
            self.init_v3()
        return self


def _call(x):
    """solders exposes some metadata fields as methods, some as attributes."""
    return x() if callable(x) else x


def _apply(metas, override):
    """override = {index: Pubkey or AccountMeta} — swap any account for an attacker's."""
    if not override:
        return metas
    metas = list(metas)
    for i, v in override.items():
        if isinstance(v, AccountMeta):
            metas[i] = v
        else:
            old = metas[i]
            metas[i] = AccountMeta(v, old.is_signer, old.is_writable)
    return metas


# The launch seed: $25 split equally by dollars at the 17 Oct prices
# ($214.39 / $339.65 / $530.01 / $709.76) — $6.25 a leg, in 8-decimal base units.
SEED_AMOUNTS = [int(6.25 / p * 10**8) for p in (214.39, 339.65, 530.01, 709.76)]

# Account-list indices in Deposit / Redeem, for adversarial overrides.
IDX = dict(user=0, vault=1, unit_mint=2, user_unit=3,
           mint0=4, mint1=5, mint2=6, mint3=7,
           vault_leg0=8, vault_leg1=9, vault_leg2=10, vault_leg3=11,
           user_leg0=12, user_leg1=13, user_leg2=14, user_leg3=15,
           token_program=16)


# ====================================================================== v4
# Orders: invest with a stablecoin / withdraw to a stablecoin, one signature.
# PF_MOCK=1 → the `mockswap` build (SWAP_PROGRAM = the mock), and the mock swap
# program is installed in each World. Tests create the real USDC mint address
# with a test mint authority, so the program's STABLE_MINTS check passes.
import random  # noqa: E402

MOCK = os.environ.get("PF_MOCK") == "1"
if MOCK:
    PROGRAM_SO = ROOT / "target" / "deploy-mock" / "permafrost.so"
MOCK_SO = ROOT / "target" / "deploy-mock" / "mockswap.so"
MOCK_SWAP = Pubkey.from_string("E9KwSa1oVgko2b3AqLsD8zU3ppKWhth12U8tnFWNpXzG")
JUPITER = Pubkey.from_string("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4")
SWAP_PROGRAM = MOCK_SWAP if MOCK else JUPITER
MOCK_POOL = pda([b"pool"], MOCK_SWAP)
TOKEN = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
USDC = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
USDT = Pubkey.from_string("Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB")
STABLE_DECIMALS = 6
STABLE_AUTH = det_keypair("stable-mint-authority")
KIND_INVEST, KIND_REDEEM = 0, 1

ERR.update({n: 6000 + 28 + i for i, n in enumerate([
    "BadStableMint", "BadExpiry", "BadBudget", "BadLeg", "OrderExpired", "OrderNotExpired",
    "LegAlreadyFilled", "LegNotFilled", "BudgetExceeded", "FillBelowMinimum", "OrderDrained",
    "NotSwapProgram", "FeeAboveMax", "BadOrderKind",
])})


def ata_classic(owner: Pubkey, mint: Pubkey) -> Pubkey:
    return Pubkey.find_program_address([bytes(owner), bytes(TOKEN), bytes(mint)], ATA_PROGRAM)[0]


def order_pda(owner: Pubkey, oid: int) -> Pubkey:
    return pda([b"order", bytes(owner), struct.pack("<Q", oid)])


def order_accounts(owner: Pubkey, oid: int, stable: Pubkey = USDC) -> dict:
    o = order_pda(owner, oid)
    return {"order": o, "stable": ata_classic(o, stable), "legs": [ata(o, m) for m in LEG_MINTS]}


def decode_order(d: bytes) -> dict:
    """Order account layout (Anchor): disc 8 | kind u8 | bump u8 | filled u8 | owner 32 |
    payer 32 | stable_mint 32 | id u64 | amount_in u64 | max_fee u64 | min_units u64 |
    min_out 4×u64 | budget 4×u64 | created_at i64 | expires_at i64."""
    o = 8
    kind, bump, filled = d[o], d[o + 1], d[o + 2]
    o += 3
    owner, payer, stable = Pubkey(d[o:o + 32]), Pubkey(d[o + 32:o + 64]), Pubkey(d[o + 64:o + 96])
    o += 96
    oid, amount_in, max_fee, min_units = struct.unpack_from("<4Q", d, o)
    o += 32
    min_out = struct.unpack_from("<4Q", d, o); o += 32
    budget = struct.unpack_from("<4Q", d, o); o += 32
    created_at, expires_at = struct.unpack_from("<2q", d, o)
    return dict(kind=kind, bump=bump, filled=filled, owner=owner, payer=payer, stable=stable, id=oid,
                amount_in=amount_in, max_fee=max_fee, min_units=min_units, min_out=list(min_out),
                budget=list(budget), created_at=created_at, expires_at=expires_at)


ORDER_SIZE = 8 + 3 + 96 + 32 + 32 + 32 + 16


class OrdersMixin:
    """Mixed into World below (kept apart so the v1/v3 harness reads unchanged)."""

    def install_v4(self):
        if MOCK:
            self.svm.add_program(MOCK_SWAP, MOCK_SO.read_bytes())
        self.svm.airdrop(STABLE_AUTH.pubkey(), 10 * 10**9)
        for m in (USDC, USDT):
            self.create_stable_mint(m)

    def create_stable_mint(self, mint: Pubkey):
        # Classic SPL mint, 82 bytes: mint_authority COption, supply, decimals, initialized, freeze COption.
        data = struct.pack("<I32sQB?I32s", 1, bytes(STABLE_AUTH.pubkey()), 0, STABLE_DECIMALS, True, 0, bytes(32))
        assert len(data) == 82
        self.svm.set_account(mint, Account(self.svm.minimum_balance_for_rent_exemption(82), data, TOKEN, False))

    def create_ata_classic_ix(self, payer: Pubkey, owner: Pubkey, mint: Pubkey) -> Instruction:
        return Instruction(ATA_PROGRAM, bytes([1]), [
            AccountMeta(payer, True, True), AccountMeta(ata_classic(owner, mint), False, True),
            AccountMeta(owner, False, False), AccountMeta(mint, False, False),
            AccountMeta(SYSTEM, False, False), AccountMeta(TOKEN, False, False),
        ])

    def mint_stable(self, owner: Pubkey, amount: int, mint: Pubkey = USDC, payer: Keypair | None = None):
        payer = payer or STABLE_AUTH
        ixs = [self.create_ata_classic_ix(payer.pubkey(), owner, mint)]
        if amount:
            ixs.append(Instruction(TOKEN, bytes([14]) + struct.pack("<QB", amount, STABLE_DECIMALS), [
                AccountMeta(mint, False, True), AccountMeta(ata_classic(owner, mint), False, True),
                AccountMeta(STABLE_AUTH.pubkey(), True, False)]))
        r = self.send(ixs, [payer, STABLE_AUTH] if payer is not STABLE_AUTH else [STABLE_AUTH])
        assert r.ok, r.reason()
        return ata_classic(owner, mint)

    def stable_balance(self, owner: Pubkey, mint: Pubkey = USDC) -> int:
        return self.balance(ata_classic(owner, mint))

    # ---- the mock's liquidity: pool PDA token accounts, funded by the test
    def fund_mock_pool(self, legs, stable: int, mint: Pubkey = USDC):
        self.mint_stable(MOCK_POOL, stable, mint)
        ixs = [self.create_ata_ix(STABLE_AUTH.pubkey(), MOCK_POOL, m) for m in LEG_MINTS]
        r = self.send(ixs, [STABLE_AUTH])
        assert r.ok, r.reason()
        mix = [Instruction(TOKEN_2022, bytes([14]) + struct.pack("<QB", amt, LEG_DECIMALS), [
            AccountMeta(m, False, True), AccountMeta(ata(MOCK_POOL, m), False, True), AccountMeta(ISSUER.pubkey(), True, False)])
            for m, amt in zip(LEG_MINTS, legs) if amt]
        if mix:
            r = self.send(mix, [ISSUER])
            assert r.ok, r.reason()

    # ---- builders
    def ix_order_invest(self, user: Keypair, payer: Keypair, oid: int, amount: int, budget, min_out,
                        min_units=0, max_fee=0, expires_at=None, stable: Pubkey = USDC, override=None) -> Instruction:
        expires_at = self.now() + 600 if expires_at is None else expires_at
        oa = order_accounts(user.pubkey(), oid, stable)
        metas = [
            AccountMeta(user.pubkey(), True, False), AccountMeta(payer.pubkey(), True, True),
            AccountMeta(oa["order"], False, True),
            AccountMeta(stable, False, False), AccountMeta(ata_classic(user.pubkey(), stable), False, True),
            AccountMeta(oa["stable"], False, True),
            *[AccountMeta(m, False, False) for m in LEG_MINTS],
            *[AccountMeta(l, False, True) for l in oa["legs"]],
            AccountMeta(TOKEN, False, False), AccountMeta(TOKEN_2022, False, False),
            AccountMeta(ATA_PROGRAM, False, False), AccountMeta(SYSTEM, False, False),
        ]
        data = anchor_disc("order_invest") + struct.pack("<QQ4Q4QQQq", oid, amount, *budget, *min_out, min_units, max_fee, expires_at)
        return Instruction(PROGRAM_ID, data, _apply(metas, override))

    def ix_order_redeem(self, user: Keypair, payer: Keypair, oid: int, units: int, min_out=(0, 0, 0, 0),
                        max_fee=0, expires_at=None, stable: Pubkey = USDC, override=None) -> Instruction:
        expires_at = self.now() + 600 if expires_at is None else expires_at
        oa = order_accounts(user.pubkey(), oid, stable)
        metas = [
            AccountMeta(user.pubkey(), True, False), AccountMeta(payer.pubkey(), True, True),
            AccountMeta(oa["order"], False, True),
            AccountMeta(VAULT, False, True), AccountMeta(UNIT_MINT, False, True),
            AccountMeta(ata(user.pubkey(), UNIT_MINT), False, True), AccountMeta(FEE_ACCOUNT, False, True),
            AccountMeta(stable, False, False), AccountMeta(oa["stable"], False, True),
            *[AccountMeta(m, False, False) for m in LEG_MINTS],
            *[AccountMeta(v, False, True) for v in VAULT_LEGS],
            *[AccountMeta(l, False, True) for l in oa["legs"]],
            AccountMeta(TOKEN, False, False), AccountMeta(TOKEN_2022, False, False),
            AccountMeta(ATA_PROGRAM, False, False), AccountMeta(SYSTEM, False, False),
        ]
        data = anchor_disc("order_redeem") + struct.pack("<QQ4QQq", oid, units, *min_out, max_fee, expires_at)
        return Instruction(PROGRAM_ID, data, _apply(metas, override))

    def mock_swap_metas(self, order: Pubkey, src: Pubkey, dst: Pubkey, src_mint: Pubkey, dst_mint: Pubkey):
        """The mock's accounts for a swap by `order`: src/dst are the escrow's accounts."""
        src_prog = TOKEN if src_mint in (USDC, USDT) else TOKEN_2022
        dst_prog = TOKEN if dst_mint in (USDC, USDT) else TOKEN_2022
        pool_src = ata_classic(MOCK_POOL, src_mint) if src_prog == TOKEN else ata(MOCK_POOL, src_mint)
        pool_dst = ata_classic(MOCK_POOL, dst_mint) if dst_prog == TOKEN else ata(MOCK_POOL, dst_mint)
        return [
            AccountMeta(order, False, False),   # the escrow signs via CPI; not a tx-level signer
            AccountMeta(src, False, True), AccountMeta(dst, False, True),
            AccountMeta(pool_src, False, True), AccountMeta(pool_dst, False, True),
            AccountMeta(MOCK_POOL, False, False),
            AccountMeta(src_mint, False, False), AccountMeta(dst_mint, False, False),
            AccountMeta(src_prog, False, False), AccountMeta(dst_prog, False, False),
        ]

    def mock_swap_data(self, amount_in: int, amount_out: int) -> bytes:
        return hashlib.sha256(b"global:swap").digest()[:8] + struct.pack("<QQ", amount_in, amount_out)

    def ix_fill_leg(self, filler: Keypair, owner: Pubkey, oid: int, leg: int, amount_in: int, amount_out: int,
                    stable: Pubkey = USDC, swap_metas=None, swap_data=None, swap_program=None, override=None) -> Instruction:
        """A fill through the MOCK: buy (invest) or sell (redeem) `leg` at the given amounts.
        swap_metas / swap_data override the forwarded accounts and data — for the attack tests."""
        oa = order_accounts(owner, oid, stable)
        od = decode_order(self.account_data(oa["order"]))
        li = min(leg, 3)  # leg ≥ 4 is for the program to refuse; use leg 0's accounts to get there
        if od["kind"] == KIND_INVEST:
            src, dst, sm, dm = oa["stable"], oa["legs"][li], stable, LEG_MINTS[li]
        else:
            src, dst, sm, dm = oa["legs"][li], oa["stable"], LEG_MINTS[li], stable
        swap_metas = self.mock_swap_metas(oa["order"], src, dst, sm, dm) if swap_metas is None else swap_metas
        swap_data = self.mock_swap_data(amount_in, amount_out) if swap_data is None else swap_data
        metas = [
            AccountMeta(filler.pubkey(), True, False), AccountMeta(oa["order"], False, True),
            AccountMeta(stable, False, False), AccountMeta(oa["stable"], False, True),
            *[AccountMeta(m, False, False) for m in LEG_MINTS],
            *[AccountMeta(l, False, True) for l in oa["legs"]],
            AccountMeta(swap_program or SWAP_PROGRAM, False, False),
            AccountMeta(TOKEN, False, False), AccountMeta(TOKEN_2022, False, False),
        ]
        data = anchor_disc("fill_leg") + bytes([leg]) + struct.pack("<I", len(swap_data)) + swap_data
        return Instruction(PROGRAM_ID, data, _apply(metas, override) + list(swap_metas))

    def ix_settle_invest(self, settler: Keypair, owner: Pubkey, oid: int, fee: int = 0, fee_to=None,
                         stable: Pubkey = USDC, override=None) -> Instruction:
        oa = order_accounts(owner, oid, stable)
        od = decode_order(self.account_data(oa["order"]))
        metas = [
            AccountMeta(settler.pubkey(), True, True), AccountMeta(oa["order"], False, True),
            AccountMeta(owner, False, False), AccountMeta(od["payer"], False, True),
            AccountMeta(VAULT, False, True), AccountMeta(UNIT_MINT, False, True),
            AccountMeta(ata(owner, UNIT_MINT), False, True), AccountMeta(FEE_ACCOUNT, False, True),
            AccountMeta(stable, False, False), AccountMeta(oa["stable"], False, True),
            AccountMeta(ata_classic(owner, stable), False, True),
            AccountMeta(fee_to or ata_classic(settler.pubkey(), stable), False, True),
            *[AccountMeta(m, False, False) for m in LEG_MINTS],
            *[AccountMeta(v, False, True) for v in VAULT_LEGS],
            *[AccountMeta(l, False, True) for l in oa["legs"]],
            AccountMeta(TOKEN, False, False), AccountMeta(TOKEN_2022, False, False),
            AccountMeta(ATA_PROGRAM, False, False), AccountMeta(SYSTEM, False, False),
        ]
        return Instruction(PROGRAM_ID, anchor_disc("settle_invest") + struct.pack("<Q", fee), _apply(metas, override))

    def ix_settle_redeem(self, settler: Keypair, owner: Pubkey, oid: int, fee: int = 0, fee_to=None,
                         stable: Pubkey = USDC, override=None) -> Instruction:
        oa = order_accounts(owner, oid, stable)
        od = decode_order(self.account_data(oa["order"]))
        metas = [
            AccountMeta(settler.pubkey(), True, True), AccountMeta(oa["order"], False, True),
            AccountMeta(owner, False, False), AccountMeta(od["payer"], False, True),
            AccountMeta(VAULT, False, False),
            AccountMeta(stable, False, False), AccountMeta(oa["stable"], False, True),
            AccountMeta(ata_classic(owner, stable), False, True),
            AccountMeta(fee_to or ata_classic(settler.pubkey(), stable), False, True),
            *[AccountMeta(m, False, False) for m in LEG_MINTS],
            *[AccountMeta(v, False, True) for v in VAULT_LEGS],
            *[AccountMeta(l, False, True) for l in oa["legs"]],
            AccountMeta(TOKEN, False, False), AccountMeta(TOKEN_2022, False, False),
            AccountMeta(ATA_PROGRAM, False, False), AccountMeta(SYSTEM, False, False),
        ]
        return Instruction(PROGRAM_ID, anchor_disc("settle_redeem") + struct.pack("<Q", fee), _apply(metas, override))

    def ix_cancel_order(self, canceller: Keypair, owner: Pubkey, oid: int, stable: Pubkey = USDC, override=None) -> Instruction:
        oa = order_accounts(owner, oid, stable)
        od = decode_order(self.account_data(oa["order"]))
        metas = [
            AccountMeta(canceller.pubkey(), True, True), AccountMeta(oa["order"], False, True),
            AccountMeta(owner, False, False), AccountMeta(od["payer"], False, True),
            AccountMeta(stable, False, False), AccountMeta(oa["stable"], False, True),
            AccountMeta(ata_classic(owner, stable), False, True),
            *[AccountMeta(m, False, False) for m in LEG_MINTS],
            *[AccountMeta(l, False, True) for l in oa["legs"]],
            *[AccountMeta(ata(owner, m), False, True) for m in LEG_MINTS],
            AccountMeta(TOKEN, False, False), AccountMeta(TOKEN_2022, False, False),
            AccountMeta(ATA_PROGRAM, False, False), AccountMeta(SYSTEM, False, False),
        ]
        return Instruction(PROGRAM_ID, anchor_disc("cancel_order"), _apply(metas, override))

    def order(self, owner: Pubkey, oid: int) -> dict | None:
        d = self.account_data(order_pda(owner, oid))
        return None if d is None else decode_order(d)


# Graft the v4 methods onto World and run install_v4 after its constructor.
for _name, _fn in list(vars(OrdersMixin).items()):
    if callable(_fn) and not _name.startswith("__"):
        setattr(World, _name, _fn)
_orig_init = World.__init__


def _init_with_v4(self, *a, **k):
    _orig_init(self, *a, **k)
    self.install_v4()


World.__init__ = _init_with_v4
