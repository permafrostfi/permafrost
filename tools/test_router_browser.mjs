/* The router, run INSIDE A BROWSER (Chromium, the site's vendored web3.js), against
 * mainnet Jupiter and RPC. tools/test_router_mainnet.mjs runs the same builders under
 * Node, which hides browser-only failures: on 26 Nov web3.js's browser build threw
 * "Buffer is not defined" from ComputeBudgetProgram, which would have broken every
 * investment. Here every bundle is built in the page, then serialized and checked from
 * Node: sizes, mainnet simulation of the swaps, the gas service's own check(), and a
 * byte-level comparison of the compute-budget / transfer encodings with web3.js's.
 *   node tools/test_router_browser.mjs                                           */
import http from 'node:http';
import { readFileSync, existsSync, statSync } from 'node:fs';
import { join, extname, resolve } from 'node:path';
import { createRequire } from 'node:module';
import web3 from '@solana/web3.js';
import { check } from '../site/api/_lib/relay-core.js';
import { derive } from './derive_addresses.mjs';

const { chromium } = createRequire(import.meta.url)('playwright');
const RPC = 'https://api.mainnet-beta.solana.com';
const USER = 'H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS';
const RELAYER = '9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM';
const TIP = '96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5';
const addresses = derive('EDp2AwfRiWEbM3K1bF5SY5SJGcFTjBZRPENKXk7Eoy61');
const SITE = resolve(new URL('../site', import.meta.url).pathname);

let failures = 0;
const ok = (c, label, detail = '') => { console.log(`${c ? '  pass' : '  FAIL'}  ${label}${detail ? '  ' + detail : ''}`); if (!c) failures++; };
const rpc = async (method, params) => {
  for (let i = 0; ; i++) {  // the public endpoint rate-limits; back off and retry
    const j = await (await fetch(RPC, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }) })).json();
    if (!/rate limit/i.test(j.error?.message || '') || i === 5) return j;
    await new Promise((r) => setTimeout(r, 2000 * (i + 1)));
  }
};

const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.json': 'application/json' };
const site = await new Promise((done) => {
  const s = http.createServer(async (req, res) => {
    // Like the live site's /api/rpc: the public endpoint refuses browser origins.
    if (req.url === '/rpc' || req.url.startsWith('/api/rpc')) {
      let b = ''; for await (const c of req) b += c;
      const up = await fetch(RPC, { method: 'POST', headers: { 'content-type': 'application/json' }, body: b });
      res.writeHead(up.status, { 'content-type': 'application/json' }); return res.end(await up.text());
    }
    if (req.url.startsWith('/api/relay')) {   // the page's own load(); not under test here
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ relayer: RELAYER, tipAccount: TIP, stableAccounts: {} }));
    }
    let p = join(SITE, new URL(req.url, 'http://x').pathname);
    if (existsSync(p) && statSync(p).isDirectory()) p = join(p, 'index.html');
    if (!p.startsWith(SITE) || !existsSync(p)) { res.writeHead(404); return res.end(); }
    res.setHeader('content-type', TYPES[extname(p)] || 'application/octet-stream');
    res.end(readFileSync(p));
  }).listen(0, '127.0.0.1', () => done(s));
});

const browser = await chromium.launch();
const page = await browser.newPage();
await page.route('**/fonts.g*/**', (r) => r.abort());
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));
await page.goto(`http://127.0.0.1:${site.address().port}/app/`);

const built = await page.evaluate(async ({ USER, RELAYER, TIP, addresses }) => {
  const { makeRouter } = await import('/js/router.js');
  const fundOverride = { holdings: [2915247n, 1840129n, 1179223n, 880579n], supply: 25_000_000_000n, exitBp: 0n,
    bonus: { on: true, bp: 90n, lockDays: 30, max: 900_000_000n, nextPosition: 0n } };
  const router = makeRouter({ web3: window.solanaWeb3, rpc: '/rpc', addresses, relayer: RELAYER, fundOverride });
  const enc = (txs) => txs.map((t) => btoa(String.fromCharCode(...t.serialize())));
  const out = {};
  const run = async (name, f) => {
    try { const r = await f(); out[name] = { txs: enc(r.txs), solUsd: r.summary ? Number(r.summary.solUsd) : null }; }
    catch (e) { out[name] = { error: `${e.message}\n${(e.stack || '').split('\n').slice(0, 4).join('\n')}` }; }
  };
  const amounts = [438495n, 236149n, 130634n, 103738n];   // about $0.65 of each
  await run('invest USDC (step 1: swaps)', () => router.buildInvest({ user: USER, stable: 'USDC', amount: 20_000_000n, tipAccount: TIP }));
  await run('invest USDT (step 1: swaps)', () => router.buildInvest({ user: USER, stable: 'USDT', amount: 20_000_000n, tipAccount: TIP }));
  await run('invest USDC locked (step 1: swaps)', () => router.buildInvest({ user: USER, stable: 'USDC', amount: 20_000_000n, tipAccount: TIP, lock: true }));
  await run('deposit (invest step 2)', () => router.buildDeposit({ user: USER, stable: 'USDC', amounts, tipAccount: TIP }));
  await run('redeem (withdraw step 1)', () => router.buildRedeem({ user: USER, stable: 'USDC', units: 10_000_000_000n, tipAccount: TIP }));
  await run('sell to USDC (withdraw step 2)', () => router.buildSell({ user: USER, stable: 'USDC', amounts, tipAccount: TIP }));
  await run('claim', () => router.buildSettle({ user: USER, positionId: 0n, kind: 'claim', tipAccount: TIP }));
  await run('exit', () => router.buildSettle({ user: USER, positionId: 0n, kind: 'exit', tipAccount: TIP }));
  return out;
}, { USER, RELAYER, TIP, addresses });
await browser.close();
site.close();

const stableAccounts = Object.fromEntries(['EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', 'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB'].map((m) => [
  web3.PublicKey.findProgramAddressSync([new web3.PublicKey(RELAYER).toBuffer(), new web3.PublicKey('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA').toBuffer(), new web3.PublicKey(m).toBuffer()],
    new web3.PublicKey('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'))[0].toBase58(), m]));

async function alts(txs) {
  const keys = [...new Set(txs.flatMap((t) => t.message.addressTableLookups.map((l) => l.accountKey.toBase58())))];
  const got = keys.length ? (await rpc('getMultipleAccounts', [keys, { encoding: 'base64' }])).result.value : [];
  const o = {};
  got.forEach((v, i) => { o[keys[i]] = web3.AddressLookupTableAccount.deserialize(Buffer.from(v.data[0], 'base64')).addresses; });
  return o;
}

for (const [name, r] of Object.entries(built)) {
  console.log(`\n${name}`);
  if (r.error) { ok(false, 'built in the browser', r.error); continue; }
  ok(true, 'built in the browser', `${r.txs.length} transaction(s)`);
  const txs = r.txs.map((b) => web3.VersionedTransaction.deserialize(Buffer.from(b, 'base64')));
  ok(txs.every((t) => t.serialize().length <= 1232), 'every transaction fits', txs.map((t) => t.serialize().length).join(' / ') + ' bytes');
  if (name.startsWith('invest')) {
    ok(txs.length === 4, 'four swap transactions, no deposit (that is step 2)', String(txs.length));
    for (let i = name.includes('locked') ? 1 : 0; i < 4; i++) {
      const s = await rpc('simulateTransaction', [r.txs[i], { encoding: 'base64', sigVerify: false, replaceRecentBlockhash: true, commitment: 'confirmed' }]);
      const v = s.result?.value;
      const err = s.error ? s.error.message : v?.err ? JSON.stringify(v.err) : null;
      ok(!err, `swap ${i} simulates on mainnet`, err ? `${err} ${(v?.logs || []).slice(-2).join(' | ')}` : `${v.unitsConsumed.toLocaleString()} CU`);
    }
  }
  {
    try {
      const a = await alts(txs);
      // Like the gas service: accounts the bundle "creates" that already exist cost nothing.
      const targets = [...new Set(txs.flatMap((t) => { const k = t.message.staticAccountKeys.map((x) => x.toBase58());
        return t.message.compiledInstructions.filter((ci) => k[ci.programIdIndex] === 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL').map((ci) => k[ci.accountKeyIndexes[1]]); }))];
      const existing = new Set();
      if (targets.length) (await rpc('getMultipleAccounts', [targets, { encoding: 'base64', dataSlice: { offset: 0, length: 0 } }])).result.value.forEach((v, i) => { if (v) existing.add(targets[i]); });
      const c = check(txs, { relayer: RELAYER, programId: addresses.programId, tipAccounts: new Set([TIP]), stableAccounts, solUsd: r.solUsd || 70 }, (k) => a[k], existing, web3);
      ok(true, 'gas service accepts it', c.freeSettle ? 'free (lone claim/exit)' : `paid $${c.paidUsd.toFixed(4)} ≥ need $${c.needUsd.toFixed(4)}`);
    } catch (e) { ok(false, 'gas service accepts it', e.message); }
  }
}

// Our hand-encoded instructions must be byte-identical to what web3.js produces.
{
  console.log('\nencodings');
  if (!built.claim.txs) { ok(false, 'claim built'); process.exit(1); }
  const claim = web3.VersionedTransaction.deserialize(Buffer.from(built.claim.txs[0], 'base64')).message;
  const keys = claim.staticAccountKeys.map((k) => k.toBase58());
  const ixs = claim.compiledInstructions.map((ci) => ({ prog: keys[ci.programIdIndex], data: Buffer.from(ci.data).toString('hex') }));
  const cb = ixs.filter((x) => x.prog === 'ComputeBudget111111111111111111111111111111');
  const want = [web3.ComputeBudgetProgram.setComputeUnitLimit({ units: Math.min(1_400_000, Math.ceil(60000 * 1.2) + 30_000) }),
    web3.ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 10000 })].map((i) => i.data.toString('hex'));
  ok(cb.length === 2 && cb[0].data === want[0] && cb[1].data === want[1], 'compute-budget bytes match web3.js', cb.map((x) => x.data).join(' '));
  const t = ixs.find((x) => x.prog === '11111111111111111111111111111111');
  const wantT = web3.SystemProgram.transfer({ fromPubkey: new web3.PublicKey(RELAYER), toPubkey: new web3.PublicKey(TIP), lamports: 20000 }).data.toString('hex');
  ok(t && t.data === wantT, 'tip transfer bytes match web3.js', t?.data);
}
ok(errors.length === 0, 'no page errors', errors.join(' | '));
console.log(failures ? `\n${failures} FAILED` : '\nall checks passed');
process.exit(failures ? 1 : 0);
