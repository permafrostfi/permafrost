/* Permafrost — read layer (program v3; corrected 24 Nov 2026).
 *
 * Everything the public site displays comes from here. No sample figures.
 * Dependency-free (plain fetch + the decoders in t22.js); web3.js is only
 * needed to *sign*, which this module deliberately does not do.
 *
 * One RPC round trip gets the whole fund: vault, unit mint, the four vault
 * token accounts and the four leg mints. A connected wallet costs one more.
 */

import {
  decodeMint, decodeTokenAccount, fromBase64, uiAmount, rawToDecimalString, toBase58,
} from './t22.js';

/* Mainnet legs. A devnet addresses.json carries its own `legMints` (the
 * replica mints) and readFund uses those instead — see legsFor(). */
export const LEGS = [
  { symbol: 'nvdax', name: 'nvidia',    mint: 'Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh' },
  { symbol: 'googlx', name: 'alphabet', mint: 'XsCPL9dNWBMvFtTmwcCA5v3xWPSMEBCszbQdiLLq6aN' },
  { symbol: 'msftx', name: 'microsoft', mint: 'XspzcW1PRtgf6Wj92HCiZdjzKCyFekVD8P5Ueh3dRMX' },
  { symbol: 'metax', name: 'meta',      mint: 'Xsa62P5mvPszXL1krVUnU5ar38bBSVcWAB6fmPCo5Zu' },
];

export const UNIT_DECIMALS = 9;
const BP = 10000;
const YEAR = 365 * 24 * 60 * 60;

export function legsFor(addresses) {
  if (!addresses.legMints) return LEGS;
  return LEGS.map((leg, i) => ({ ...leg, mint: addresses.legMints[i] }));
}

/* VaultState, v3 — program/programs/permafrost/src/lib.rs. 275 bytes.
 * Confirmed 24 Nov against the program (tests/test_v3.py reads the same
 * offsets): discriminator 8, version u8, vault_bump u8, unit_mint_bump u8 —
 * TWO bumps, so the cap is at 11 (an earlier draft of this file said 12). */
export const VAULT_DISCRIMINATOR = [228, 196, 82, 165, 98, 210, 235, 152];
export const VAULT_SIZE = 275;
export const VAULT_CAP_OFFSET = 8 + 3;
export const POSITION_SIZE = 81;

export class Rpc {
  constructor(url) { this.url = url; this.id = 0; }

  /* Retries 429 and 5xx with backoff. A rate-limited read must never render
   * as a zero balance. */
  async call(method, params, attempts = 4) {
    let lastError;
    for (let attempt = 0; attempt < attempts; attempt++) {
      if (attempt) await new Promise((r) => setTimeout(r, 400 * 2 ** (attempt - 1)));
      let res;
      try {
        res = await fetch(this.url, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ jsonrpc: '2.0', id: ++this.id, method, params }),
        });
      } catch (err) {
        lastError = new Error(`rpc ${method}: ${err.message}`);
        continue;
      }
      if (res.status === 429 || res.status >= 500) {
        lastError = new Error(`rpc ${method}: http ${res.status}`);
        continue;
      }
      if (!res.ok) throw new Error(`rpc ${method}: http ${res.status}`);
      const body = await res.json();
      if (body.error) throw new Error(`rpc ${method}: ${body.error.message}`);
      return body.result;
    }
    throw lastError;
  }

  accounts(addresses) {
    return this.call('getMultipleAccounts', [addresses, { encoding: 'base64', commitment: 'confirmed' }]);
  }
}

const decodeAccount = (entry) => (entry ? fromBase64(entry.data[0]) : null);

/* Every field of the vault the site shows. Returns null if the bytes are not
 * a Permafrost vault — never a half-decoded object. */
export function decodeVault(d) {
  if (!d || d.length !== VAULT_SIZE || !VAULT_DISCRIMINATOR.every((b, i) => d[i] === b)) return null;
  const dv = new DataView(d.buffer, d.byteOffset, d.byteLength);
  const pk = (o) => toBase58(d.subarray(o, o + 32));
  const zero = (o) => d.subarray(o, o + 32).every((b) => b === 0);
  const v3 = d[19] === 1; // discounts_ready: set by init_v3
  return {
    version: d[8],
    rawCap: dv.getBigUint64(VAULT_CAP_OFFSET, true),
    v3,
    discount: {
      on: d[20] === 1,
      bonusBp: dv.getUint16(21, true),
      lockDays: dv.getUint16(23, true),
      rawMaxBonus: dv.getBigUint64(25, true),
    },
    positionsOpened: dv.getBigUint64(33, true),
    admin: v3 ? pk(43) : null,
    pendingAdmin: v3 && !zero(75) ? pk(75) : null,
    fees: {
      entryBp: dv.getUint16(107, true),
      exitBp: dv.getUint16(109, true),
      streamBp: dv.getUint16(111, true),
      pending: dv.getBigInt64(119, true) === 0n ? null : {
        entryBp: dv.getUint16(113, true),
        exitBp: dv.getUint16(115, true),
        streamBp: dv.getUint16(117, true),
        at: Number(dv.getBigInt64(119, true)),
      },
      lastAccrual: Number(dv.getBigInt64(127, true)),
    },
  };
}

/* Management-fee units owed but not yet minted — the program charges lazily, on
 * the next deposit / redemption / accrue. Exactly permafrost_reference.accrue
 * (m = S·f/(1−f), f = rate·elapsed/(BP·YEAR), floor), including a pending rate
 * that has come into force: the old rate up to its time, the new rate after.
 * The site must add these to supply before dividing, or NAV per unit is
 * overstated by the fee owed. */
export function owedFeeUnits(vault, rawSupply, now) {
  if (!vault || !vault.v3) return 0n;
  const accrue = (s, bp, from, to) => {
    const elapsed = BigInt(Math.max(0, to - from));
    if (elapsed === 0n || bp === 0) return 0n;
    const num = BigInt(bp) * elapsed;
    const den = BigInt(BP) * BigInt(YEAR);
    return (s * num) / (den - num);
  };
  const f = vault.fees;
  let s = rawSupply;
  let owed = 0n;
  let from = f.lastAccrual;
  let bp = f.streamBp;
  if (f.pending && now >= f.pending.at) {
    const m = accrue(s, bp, from, f.pending.at);
    owed += m; s += m; from = f.pending.at; bp = f.pending.streamBp;
  }
  owed += accrue(s, bp, from, now);
  return owed;
}

/* Read the whole fund. `addresses` comes from tools/derive_addresses.mjs. */
export async function readFund(rpc, addresses, nowSeconds) {
  const now = nowSeconds ?? Math.floor(Date.now() / 1000);
  const legDefs = legsFor(addresses);
  const keys = [
    addresses.vault,
    addresses.unitMint,
    ...addresses.vaultLegs,
    ...legDefs.map((l) => l.mint),
    addresses.pool,
    addresses.escrow,
    addresses.feeAccount,
  ];
  const { value } = await rpc.accounts(keys);

  const vaultData = decodeAccount(value[0]);
  const unitData = decodeAccount(value[1]);
  const vault = decodeVault(vaultData);

  if (!vault || !unitData) {
    return { initialized: false, now, legs: [], warnings: ['vault not initialized'] };
  }

  const unitMint = decodeMint(unitData, now);
  /* Supply as the program will see it on the next transaction: on-chain supply
   * plus management-fee units owed since last_accrual. All per-unit figures use
   * this; it equals on-chain supply while the management fee is 0. */
  const owed = owedFeeUnits(vault, unitMint.rawSupply, now);
  const rawEffectiveSupply = unitMint.rawSupply + owed;
  const unitSupply = uiAmount(rawEffectiveSupply, unitMint.decimals);
  const tokenBalance = (i) => {
    const d = decodeAccount(value[i]);
    return d ? decodeTokenAccount(d).rawAmount : 0n;
  };
  const rawPool = tokenBalance(10);
  const rawEscrow = tokenBalance(11);
  const rawFeeAccount = tokenBalance(12);

  const warnings = [];
  const legs = legDefs.map((leg, i) => {
    const holdingData = decodeAccount(value[2 + i]);
    const mintData = decodeAccount(value[6 + i]);
    if (!mintData) throw new Error(`leg mint missing: ${leg.symbol}`);

    const mint = decodeMint(mintData, now);
    const held = holdingData ? decodeTokenAccount(holdingData) : null;
    const raw = held ? held.rawAmount : 0n;

    /* These are not cosmetic. Each one changes what the program will do.
     * Tested in the v1 suite: E3 (pause), E4 (freeze), D1 (empty leg). */
    if (mint.paused) warnings.push(`${leg.symbol}: issuer has paused the token — deposits and redemptions both refuse`);
    if (held && held.frozen) warnings.push(`${leg.symbol}: the vault's account is frozen — deposits and redemptions both refuse`);
    if (mint.transferHookProgram) warnings.push(`${leg.symbol}: the issuer has set a transfer hook — the program has no hook support, so deposits and redemptions stop`);
    if (mint.defaultAccountState === 2) warnings.push(`${leg.symbol}: new holder accounts start frozen`);
    if (raw === 0n) warnings.push(`${leg.symbol}: holding is zero — deposits refuse permanently, redemptions continue on the other legs`);

    return {
      ...leg,
      decimals: mint.decimals,
      rawAmount: raw,
      rawAmountString: rawToDecimalString(raw, mint.decimals),
      multiplier: mint.multiplier,
      /* What the basket is actually worth in tokens: raw scaled by the
       * dividend multiplier in force. */
      amount: uiAmount(raw, mint.decimals, mint.multiplier),
      perUnit: unitSupply > 0 ? uiAmount(raw, mint.decimals, mint.multiplier) / unitSupply : 0,
      issuer: {
        paused: mint.paused,
        frozen: held ? held.frozen : false,
        permanentDelegate: mint.permanentDelegate,
        freezeAuthority: mint.freezeAuthority,
        mintAuthority: mint.mintAuthority,
        transferHookProgram: mint.transferHookProgram,
      },
    };
  });

  const cap = readCap(vaultData);
  const f = vault.fees;
  if (f.pending) {
    const when = new Date(f.pending.at * 1000).toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
    const pct = (bp) => `${bp / 100}%`;
    warnings.push(`fee change ${now >= f.pending.at ? 'in force since' : 'takes effect'} ${when}: entry ${pct(f.pending.entryBp)}, exit ${pct(f.pending.exitBp)}, management ${pct(f.pending.streamBp)} a year`);
  }
  /* Fees in force right now (a pending change whose time has passed is in force
   * even before a transaction writes it). */
  const live = f.pending && now >= f.pending.at ? f.pending : f;

  return {
    initialized: true,
    now,
    version: vault.version,
    v3: vault.v3,
    admin: vault.admin,
    pendingAdmin: vault.pendingAdmin,
    fees: {
      entryBp: live.entryBp, exitBp: live.exitBp, streamBp: live.streamBp,
      pending: f.pending && now < f.pending.at ? f.pending : null,
      lastAccrual: f.lastAccrual,
      owedUnits: uiAmount(owed, UNIT_DECIMALS),
      feeAccount: addresses.feeAccount,
      feeAccountUnits: uiAmount(rawFeeAccount, UNIT_DECIMALS),
    },
    lockedBonus: {
      on: vault.discount.on,
      bonusBp: vault.discount.bonusBp,
      lockDays: vault.discount.lockDays,
      maxBonusUnits: uiAmount(vault.discount.rawMaxBonus, UNIT_DECIMALS),
      poolUnits: uiAmount(rawPool, UNIT_DECIMALS),
      escrowUnits: uiAmount(rawEscrow, UNIT_DECIMALS),
      positionsOpened: Number(vault.positionsOpened),
    },
    unitMint: {
      address: addresses.unitMint,
      decimals: unitMint.decimals,
      supply: unitSupply,
      rawSupply: unitMint.rawSupply,
      rawEffectiveSupply,
      /* L3: our token must carry neither of these. If either appears, the
       * program has been upgraded or this is not our mint. */
      freezeAuthority: unitMint.freezeAuthority,
      permanentDelegate: unitMint.permanentDelegate,
    },
    cap,
    // The cap is checked against on-chain supply plus owed fee units — the same
    // number the program computes before minting.
    capRemaining: cap === null ? null : Math.max(0, cap - unitSupply),
    legs,
    warnings,
  };
}

/* Refuse an implausible cap rather than print a wrong one. */
export function readCap(vaultData) {
  if (!decodeVault(vaultData)) return null;
  const dv = new DataView(vaultData.buffer, vaultData.byteOffset, vaultData.byteLength);
  const raw = dv.getBigUint64(VAULT_CAP_OFFSET, true);
  const cap = Number(raw) / 10 ** UNIT_DECIMALS;
  if (!Number.isFinite(cap) || cap <= 0 || cap > 1e12) return null;
  return cap;
}

/* A connected wallet's unit balance. Derives nothing: asks the node for the
 * account by mint, so no PDA maths and no ed25519 in the read path. */
export async function readHolder(rpc, addresses, owner) {
  const res = await rpc.call('getTokenAccountsByOwner', [
    owner,
    { mint: addresses.unitMint },
    { encoding: 'base64', commitment: 'confirmed' },
  ]);
  let raw = 0n;
  let frozen = false;
  for (const entry of res.value) {
    const account = decodeTokenAccount(fromBase64(entry.account.data[0]));
    raw += account.rawAmount;
    if (account.frozen) frozen = true;
  }
  return {
    owner,
    rawUnits: raw,
    units: uiAmount(raw, UNIT_DECIMALS),
    frozen,
  };
}

/* What a holder would receive in kind for `units`, pro rata, before slippage.
 * Redeem pays the four tokens, not dollars. The exit fee is a slice of the
 * units handed back (to the fee account); the payout is on the rest. */
export function redemptionInKind(fund, units) {
  if (!fund.initialized || fund.unitMint.supply === 0) return [];
  const exitBp = fund.fees ? fund.fees.exitBp : 0;
  const feeUnits = (units * exitBp) / BP;
  const paid = units - feeUnits;
  return fund.legs.map((leg) => ({
    symbol: leg.symbol,
    amount: leg.perUnit * paid,
  }));
}

/* A wallet's locked-bonus positions (open ones only: a claimed or exited
 * position's account is closed). Needs an RPC that serves getProgramAccounts
 * (Helius does; the public endpoint often refuses). */
export async function readPositions(rpc, addresses, owner, nowSeconds) {
  const now = nowSeconds ?? Math.floor(Date.now() / 1000);
  const res = await rpc.call('getProgramAccounts', [addresses.programId, {
    encoding: 'base64',
    commitment: 'confirmed',
    filters: [{ dataSize: POSITION_SIZE }, { memcmp: { offset: 16, bytes: owner } }],
  }]);
  return res.map(({ pubkey, account }) => {
    const d = fromBase64(account.data[0]);
    const dv = new DataView(d.buffer, d.byteOffset, d.byteLength);
    const unlockAt = Number(dv.getBigInt64(72, true));
    return {
      address: pubkey,
      id: Number(dv.getBigUint64(8, true)),
      owner: toBase58(d.subarray(16, 48)),
      principal: uiAmount(dv.getBigUint64(48, true), UNIT_DECIMALS),
      bonus: uiAmount(dv.getBigUint64(56, true), UNIT_DECIMALS),
      openedAt: Number(dv.getBigInt64(64, true)),
      unlockAt,
      matured: now >= unlockAt,
    };
  }).sort((a, b) => a.id - b.id);
}

/* --- prices -------------------------------------------------------------
 * The protocol itself uses a Pyth market-hours feed. For display the site
 * needs a price too, and the two must not disagree visibly. Pending Vishal's
 * decision, this is a pluggable adapter with Jupiter as the default; it
 * returns null for any leg it cannot price rather than guessing, and the UI
 * must then show the in-kind basket with no dollar figure.
 */
export async function jupiterPrices(mints, fetchImpl = fetch) {
  const url = `https://lite-api.jup.ag/price/v3?ids=${mints.join(',')}`;
  const res = await fetchImpl(url);
  if (!res.ok) throw new Error(`price feed: http ${res.status}`);
  const body = await res.json();
  const out = {};
  for (const mint of mints) {
    const price = body?.[mint]?.usdPrice ?? body?.data?.[mint]?.price;
    out[mint] = Number.isFinite(Number(price)) ? Number(price) : null;
  }
  return out;
}

/* Fund value, NAV per unit, and the holdings weights the pie needs.
 * Returns priced:false if any leg is unpriceable — partial pricing would
 * understate NAV, which is the worse failure. */
export function value(fund, prices) {
  const parts = fund.legs.map((leg) => {
    const price = prices[leg.mint];
    return {
      symbol: leg.symbol,
      name: leg.name,
      amount: leg.amount,
      price,
      value: price === null || price === undefined ? null : leg.amount * price,
    };
  });

  if (parts.some((p) => p.value === null)) {
    return { priced: false, parts, total: null, navPerUnit: null };
  }

  const total = parts.reduce((sum, p) => sum + p.value, 0);
  return {
    priced: true,
    parts: parts.map((p) => ({ ...p, weight: total > 0 ? p.value / total : 0 })),
    total,
    navPerUnit: fund.unitMint.supply > 0 ? total / fund.unitMint.supply : null,
  };
}
