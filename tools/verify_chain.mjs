#!/usr/bin/env node
/* Verify the read layer against live mainnet, before the program is deployed.
 *
 *   node tools/verify_chain.mjs [--rpc URL] [--program PROGRAM_ID] [--devnet]
 *
 * --devnet: the leg checks are mainnet facts (Backed's authorities) and are
 * skipped; the vault is read with the replica mints.
 *
 * With no --program it checks everything that does not need our vault:
 * the four leg mints decode, the issuer powers read back as recorded, the
 * scaled-UI multiplier in force is the new one, a real token account decodes,
 * and the price feed answers. With --program it additionally reads the vault.
 */
import { Rpc, LEGS, readFund, value, jupiterPrices } from '../site/js/chain.js';
import {
  decodeMint as mintOf, decodeTokenAccount as accountOf, fromBase64 as b64,
} from '../site/js/t22.js';

const arg = (flag, dflt) => {
  const i = process.argv.indexOf(flag);
  return i > 0 ? process.argv[i + 1] : dflt;
};

const RPC_URL = arg('--rpc', 'https://api.mainnet-beta.solana.com');
const PROGRAM = arg('--program', null);
const DEVNET = process.argv.includes('--devnet');

const EXPECTED = {
  permanentDelegate: '5aMNNLQJwAEeoemTEMkv5NVjqKwvvefRYCQ5Z67HFvEq',
  freezeAuthority: 'JDq14BWvqCRFNu1krb12bcRpbGtJZ1FLEakMw6FdxJNs',
  pausableAuthority: 'JDq14BWvqCRFNu1krb12bcRpbGtJZ1FLEakMw6FdxJNs',
};

let failures = 0;
const check = (ok, label, detail = '') => {
  console.log(`${ok ? '  pass' : '  FAIL'}  ${label}${detail ? '  ' + detail : ''}`);
  if (!ok) failures++;
};

const rpc = new Rpc(RPC_URL);
const now = Math.floor(Date.now() / 1000);

/* 0. The base58 encoder, against web3.js as the oracle. A silent bug here
 * reads as "no permanent delegate" / "no transfer hook", which is the most
 * dangerous possible direction to be wrong in. */
{
  const { PublicKey } = await import('@solana/web3.js');
  const { toBase58 } = await import('../site/js/t22.js');
  const cases = [
    new Uint8Array(32),                                        // all zero
    Uint8Array.from({ length: 32 }, (_, i) => (i === 31 ? 1 : 0)),
    Uint8Array.from({ length: 32 }, (_, i) => i),              // leading zero byte
    Uint8Array.from({ length: 32 }, () => 255),                // max
  ];
  console.log('base58 encoder');
  for (const bytes of cases) {
    const want = new PublicKey(bytes).toBase58();
    const got = toBase58(bytes);
    check(got === want, `encodes ${want.slice(0, 12)}...`, got === want ? '' : `got ${got}`);
  }
  for (let i = 0; i < 200; i++) {
    const bytes = new Uint8Array(32);
    for (let j = 0; j < 32; j++) bytes[j] = (Math.random() * 256) | 0;
    if (toBase58(bytes) !== new PublicKey(bytes).toBase58()) {
      check(false, 'random round trip', toBase58(bytes));
      break;
    }
    if (i === 199) check(true, '200 random keys match web3.js');
  }
  console.log();
}

console.log(`rpc ${RPC_URL}`);
console.log(`now ${new Date(now * 1000).toISOString()}\n`);

if (!DEVNET) {
/* 1. The four leg mints. */
console.log('leg mints');
const mintAccounts = await rpc.accounts(LEGS.map((l) => l.mint));
const mints = {};
LEGS.forEach((leg, i) => {
  const entry = mintAccounts.value[i];
  if (!entry) { check(false, `${leg.symbol} exists`); return; }
  const mint = mintOf(b64(entry.data[0]), now);
  mints[leg.symbol] = mint;
  check(mint.decimals === 8, `${leg.symbol} decimals = 8`, `got ${mint.decimals}`);
  check(mint.permanentDelegate === EXPECTED.permanentDelegate,
    `${leg.symbol} permanent delegate unchanged`, mint.permanentDelegate ?? 'none');
  check(mint.freezeAuthority === EXPECTED.freezeAuthority,
    `${leg.symbol} freeze authority unchanged`, mint.freezeAuthority ?? 'none');
  check(mint.pausableAuthority === EXPECTED.pausableAuthority,
    `${leg.symbol} pausable authority unchanged`, mint.pausableAuthority ?? 'none');
  check(mint.paused === false, `${leg.symbol} not paused`);
  check(mint.transferHookProgram === null, `${leg.symbol} no transfer hook set`,
    mint.transferHookProgram ?? '');
  check(mint.defaultAccountState === 1, `${leg.symbol} new accounts not frozen by default`);
});

/* 2. The scaled-UI trap: new_multiplier is already in force on all four. */
console.log('\nscaled-ui multiplier');
for (const leg of LEGS) {
  const mint = mints[leg.symbol];
  if (!mint) continue;
  const s = mint.scaledUi;
  const stale = s.multiplier;
  const live = mint.multiplier;
  const bps = ((live / stale) - 1) * 10000;
  check(s.effectiveTimestamp <= now && live === s.newMultiplier,
    `${leg.symbol} using new_multiplier`,
    `${live.toFixed(10)} vs stale ${stale.toFixed(10)} (${bps.toFixed(1)} bps)`);
}

/* 3. A real token account decodes. */
console.log('\ntoken account decode');
let sample = null;
try {
  const largest = await rpc.call('getTokenLargestAccounts', [LEGS[0].mint, { commitment: 'confirmed' }], 6);
  sample = largest.value?.[0]?.address ?? null;
} catch (err) {
  // The public endpoint rate-limits this method hard. Not a code defect —
  // and a standing reminder that the site needs the Helius mainnet URL.
  console.log(`  skip  no sample holder (${err.message})`);
}
if (sample) {
  const { value: got } = await rpc.accounts([sample]);
  const account = accountOf(b64(got[0].data[0]));
  check(account.mint === LEGS[0].mint, 'decoded mint matches', account.mint);
  check(account.rawAmount > 0n, 'decoded a non-zero balance',
    `${account.rawAmount} base units, owner ${account.owner}`);
  check(account.state === 1 || account.state === 2, 'state byte sane', String(account.state));
}

/* 4. Prices. */
console.log('\nprice feed');
try {
  const prices = await jupiterPrices(LEGS.map((l) => l.mint));
  for (const leg of LEGS) {
    const p = prices[leg.mint];
    check(p !== null && p > 0, `${leg.symbol} priced`, p === null ? 'unpriceable' : `$${p.toFixed(2)}`);
  }
} catch (err) {
  check(false, 'price feed reachable', err.message);
}

} // !DEVNET

/* 5. The vault, if a program id was given. */
if (PROGRAM) {
  console.log('\nvault');
  const { derive } = await import('./derive_addresses.mjs');
  const addresses = derive(PROGRAM, { devnet: DEVNET });
  const fund = await readFund(rpc, addresses, now);
  check(fund.initialized, 'vault initialized');
  if (fund.initialized) {
    check(fund.unitMint.freezeAuthority === null, 'unit token has no freeze authority (L3)');
    check(fund.unitMint.permanentDelegate === null, 'unit token has no permanent delegate (L3)');
    check(fund.cap !== null, 'cap decoded (offset 11)', String(fund.cap));
    check(fund.version === 3 && fund.v3, 'program v3 switched on (init_v3 run)', `version ${fund.version}`);
    check(fund.admin !== null, 'admin key set', fund.admin ?? 'none');
    const f = fund.fees;
    check(f.entryBp <= 100 && f.exitBp <= 100 && f.streamBp <= 100, 'fees within caps',
      `entry ${f.entryBp} bp, exit ${f.exitBp} bp, management ${f.streamBp} bp/yr${f.pending ? `, pending from ${new Date(f.pending.at * 1000).toISOString()}` : ''}`);
    const b = fund.lockedBonus;
    console.log(`  locked bonus ${b.on ? 'on' : 'off'}: ${b.bonusBp / 100}%, max ${b.maxBonusUnits} units, ${b.lockDays} d; pool ${b.poolUnits}, escrow ${b.escrowUnits}, ${b.positionsOpened} opened`);
    check(fund.unitMint.supply >= 25, 'supply at least the 25 seed units', String(fund.unitMint.supply));
    console.log(`\n  supply ${fund.unitMint.supply} units, cap ${fund.cap}, remaining ${fund.capRemaining}`);
    for (const leg of fund.legs) {
      console.log(`  ${leg.symbol.padEnd(7)} ${leg.amount} (${leg.perUnit} per unit)`);
    }
    if (fund.warnings.length) {
      console.log('\n  warnings:');
      fund.warnings.forEach((w) => console.log(`    - ${w}`));
    }
    const prices = DEVNET ? Object.fromEntries(fund.legs.map((l) => [l.mint, null]))
      : await jupiterPrices(LEGS.map((l) => l.mint));
    const v = value(fund, prices);
    console.log(v.priced
      ? `\n  aum $${v.total.toFixed(2)}, nav $${v.navPerUnit.toFixed(6)} per unit`
      : '\n  unpriceable — site must show the in-kind basket with no dollar figure');
  }
} else {
  console.log('\nvault: skipped (no --program; not deployed yet)');
}

console.log(failures ? `\n${failures} FAILED` : '\nall checks passed');
process.exit(failures ? 1 : 0);
