/* Build real invest and redeem bundles against mainnet Jupiter and simulate
 * the swap transactions, with no funds and no keys:
 *   - the "user" is a large public USDC holder; the "gas service" a public
 *     account with SOL and a USDC account (simulation needs real balances);
 *   - the vault isn't on mainnet yet, so its state is a stand-in (the launch
 *     seed); deposit/redeem transactions are built and size-checked, not run.
 *   node tools/test_router_mainnet.mjs [rpc]                                   */
import web3 from '@solana/web3.js';
import { makeRouter } from '../site/js/router.js';
import { check } from '../site/api/_lib/relay-core.js';
import { derive } from './derive_addresses.mjs';

const RPC = process.argv[2] || 'https://api.mainnet-beta.solana.com';
const USER = 'H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS';
const RELAYER = '9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM';
const TIP = '96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5';
const addresses = derive('EDp2AwfRiWEbM3K1bF5SY5SJGcFTjBZRPENKXk7Eoy61'); // the mainnet id (not deployed yet at test time)
const seedState = { holdings: [2915247n, 1840129n, 1179223n, 880579n], supply: 25_000_000_000n, exitBp: 0n,
  bonus: { on: true, bp: 90n, lockDays: 30, max: 900_000_000n, nextPosition: 0n } };

const router = makeRouter({ web3, rpc: RPC, addresses, relayer: RELAYER, fundOverride: seedState });
const rpc = async (method, params) => (await (await fetch(RPC, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }) })).json());

let failures = 0;
const ok = (c, label, detail = '') => { console.log(`${c ? '  pass' : '  FAIL'}  ${label}${detail ? '  ' + detail : ''}`); if (!c) failures++; };

async function simulate(tx, label) {
  const r = await rpc('simulateTransaction', [Buffer.from(tx.serialize()).toString('base64'), { encoding: 'base64', sigVerify: false, replaceRecentBlockhash: true, commitment: 'confirmed' }]);
  const v = r.result?.value;
  const err = r.error ? r.error.message : v?.err ? JSON.stringify(v.err) : null;
  ok(!err, `${label} simulates on mainnet`, err ? `${err} ${(v?.logs || []).slice(-3).join(' | ')}` : `${v.unitsConsumed.toLocaleString()} CU`);
}

async function resolveAlts(txs) {
  const keys = [...new Set(txs.flatMap((t) => t.message.addressTableLookups.map((l) => l.accountKey.toBase58())))];
  const got = keys.length ? (await rpc('getMultipleAccounts', [keys, { encoding: 'base64' }])).result.value : [];
  const out = {};
  got.forEach((v, i) => { out[keys[i]] = web3.AddressLookupTableAccount.deserialize(Buffer.from(v.data[0], 'base64')).addresses; });
  return out;
}

for (const [stable, lock] of [['USDC', false], ['USDT', false], ['USDC', true]]) {
  console.log(`\ninvest $20 of ${stable}${lock ? ', locked 30 days (setup-cost rebate)' : ''}`);
  const { txs, summary } = await router.buildInvest({ user: USER, stable, amount: 20_000_000, tipAccount: TIP, lock });
  console.log(`  gas reimbursement $${Number(summary.gas) / 1e6} · split ${summary.split.map((s) => '$' + (Number(s) / 1e6).toFixed(2)).join(' ')}`);
  txs.forEach((t, i) => ok(t.serialize().length <= 1232, `tx ${i} size`, `${t.serialize().length} bytes`));
  // Locked mode creates the user's PAIF account in swap 0; the PAIF mint only
  // exists once the vault is on mainnet, so that transaction can't simulate yet.
  for (let i = lock ? 1 : 0; i < 4; i++) await simulate(txs[i], `swap ${i}`);
  if (lock) console.log('  skip  swap 0 (creates the PAIF account; the PAIF mint is not on mainnet yet)');
  const alts = await resolveAlts(txs);
  const sol = Number(summary.solUsd);
  try {
    const r = check(txs, { relayer: RELAYER, programId: addresses.programId, tipAccounts: new Set([TIP]),
      stableAccounts: Object.fromEntries(['EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', 'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB']
        .map((m) => [router.ata(router.RELAYER, new web3.PublicKey(m), router.P.token).toBase58(), m])),
      solUsd: sol }, (k) => alts[k], new Set(), web3);
    ok(true, 'gas service accepts the bundle', `cost ${r.costLamports} lamports = $${(r.costLamports / 1e9 * sol).toFixed(4)}, paid $${r.paidUsd.toFixed(4)}`);
  } catch (e) { ok(false, 'gas service accepts the bundle', e.message); }
}

console.log('\nclaim a locked position');
const cl = await router.buildSettle({ user: USER, positionId: 0n, kind: 'claim', tipAccount: TIP });
ok(cl.txs[0].serialize().length <= 1232, 'claim tx size', `${cl.txs[0].serialize().length} bytes`);
try {
  const r = check(cl.txs, { relayer: RELAYER, programId: addresses.programId, tipAccounts: new Set([TIP]), stableAccounts: {}, solUsd: 69 }, () => null, new Set(), web3);
  ok(r.freeSettle, 'gas service accepts it without reimbursement', `cost ${r.costLamports} lamports`);
} catch (e) { ok(false, 'gas service accepts the claim', e.message); }

console.log('\nredeem 10 units to USDC');
const red = await router.buildRedeem({ user: USER, stable: 'USDC', units: 10_000_000_000, tipAccount: TIP });
red.txs.forEach((t, i) => ok(t.serialize().length <= 1232, `tx ${i} size`, `${t.serialize().length} bytes`));
console.log(`  payout per leg ${red.summary.payout.join(' / ')} · expected $${Number(red.summary.expected) / 1e6} − gas $${Number(red.summary.gas) / 1e6}`);
console.log(failures ? `\n${failures} FAILED` : '\nall checks passed');
process.exit(failures ? 1 : 0);
