/* The app page's two-step invest/withdraw screens (28 Nov), end to end, against a
 * LOCAL validator — the parts that don't need Jupiter:
 *   - "Stock tokens in your wallet" → Add them to the fund (buildDeposit, invest step 2)
 *   - Withdraw → step 1 redeem rides free through the gas service → step 2 (selling)
 *     can't route on a local chain → the page says step 1 went through and offers
 *     the wallet box → Add them to the fund again.
 * Needs USDC on the local chain (cloned mint + preset accounts for the user and the
 * gas wallet passed with --relayer). Prices are stubbed for the replica mints.
 *
 *   node tools/test_app_local.mjs USER_KEY.json --relayer RELAYER_KEY.json
 *
 * (Header of the lock test, kept for the shared plumbing:)
 * The app page's locked-deposit screens, end to end, against a LOCAL validator.
 *
 * Real pieces: the app page, chain.js, router.js (buildSettle), the gas service
 * (site/api/relay.js) and the RPC proxy (site/api/rpc.js), the devnet build of the
 * program. Stand-ins: Phantom (signs with a local test key) and Jito (a shim that
 * forwards each bundled transaction to the local validator).
 *
 * Checks: the lock option shows (with the vault's real terms) only when the bonus is
 * on and the pool is funded; the Locked list shows the position with "Leave early";
 * Leave early → confirm → Phantom → gas service → on chain: the position closes, the
 * user gets the principal back, the bonus returns to the pool, the gas service is
 * not reimbursed (a lone exit rides free).
 *
 * Setup: a local validator with the devnet build at 48N2…, the replica mints and a
 * launched vault (tests/devnet.py replicas + e2e with PF_RPC=http://localhost:8899),
 * then program/tests/local_lock_setup.py USER_KEY.json (bonus on, pool funded, one
 * open position; run it again before each re-run).
 *
 *   node tools/test_app_lock.mjs USER_KEY.json [--rpc http://127.0.0.1:8899]
 */
import http from 'node:http';
import { readFileSync, existsSync, statSync } from 'node:fs';
import { join, extname, resolve } from 'node:path';
import web3 from '@solana/web3.js';
import { createRequire } from 'node:module';
import { derive } from './derive_addresses.mjs';

// The sandbox's Playwright is global (CommonJS), not a dependency of this repo.
const { chromium } = createRequire(import.meta.url)('playwright');

const arg = (f, d) => { const i = process.argv.indexOf(f); return i > 0 ? process.argv[i + 1] : d; };
const LOCAL = arg('--rpc', 'http://127.0.0.1:8899');
const PROGRAM = '48N2qchckkoWhmT9anrDU7bRt83QPLNBqj9JLZA7kEF2';
const SITE = resolve(new URL('../site', import.meta.url).pathname);
const user = web3.Keypair.fromSecretKey(Uint8Array.from(JSON.parse(readFileSync(process.argv[2], 'utf8'))));
const relayer = web3.Keypair.fromSecretKey(Uint8Array.from(JSON.parse(readFileSync(arg('--relayer'), 'utf8'))));
const tip = web3.Keypair.generate().publicKey;
const conn = new web3.Connection(LOCAL, 'confirmed');
const addresses = derive(PROGRAM, { devnet: true });

let failures = 0;
const ok = (c, label, detail = '') => { console.log(`${c ? '  pass' : '  FAIL'}  ${label}${detail ? '  ' + detail : ''}`); if (!c) failures++; };

await conn.confirmTransaction(await conn.requestAirdrop(tip, 1e9), 'confirmed');

const body = (req) => new Promise((done) => { let d = ''; req.on('data', (c) => { d += c; }); req.on('end', () => done(d)); });
const serve = (h) => new Promise((done) => { const s = http.createServer(h).listen(0, '127.0.0.1', () => done(s)); });

// Jito stand-in: tip accounts, and each bundled transaction straight to the validator.
let bundles = 0;
const jito = await serve(async (req, res) => {
  const j = JSON.parse(await body(req));
  let result = null;
  if (j.method === 'getTipAccounts') result = [tip.toBase58()];
  if (j.method === 'sendBundle') {
    bundles++;
    for (const b64 of j.params[0]) await conn.sendRawTransaction(Buffer.from(b64, 'base64'));
    result = `local-bundle-${bundles}`;
  }
  res.setHeader('content-type', 'application/json');
  res.end(JSON.stringify({ jsonrpc: '2.0', id: j.id ?? 1, result }));
});

process.env.RPC_URL = LOCAL;
process.env.PROGRAM_ID = PROGRAM;
process.env.JITO_URL = `http://127.0.0.1:${jito.address().port}/api/v1`;
process.env.RELAYER_SECRET_KEY = JSON.stringify([...relayer.secretKey]);
const relay = (await import('../site/api/relay.js')).default;
const rpcProxy = (await import('../site/api/rpc.js')).default;

// The site: static files, /api/* through the real handlers, addresses for the local vault.
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.json': 'application/json', '.css': 'text/css', '.svg': 'image/svg+xml' };
const site = await serve(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  if (url.pathname.startsWith('/api/')) {
    const raw = req.method === 'POST' ? await body(req) : '';
    const vres = {
      statusCode: 200,
      status(c) { this.statusCode = c; return this; },
      setHeader(k, v) { res.setHeader(k, v); return this; },
      json(o) { res.writeHead(this.statusCode, { 'content-type': 'application/json' }); res.end(JSON.stringify(o)); return this; },
      send(t) { res.writeHead(this.statusCode); res.end(t); return this; },
    };
    const vreq = { method: req.method, body: raw ? JSON.parse(raw) : undefined, query: Object.fromEntries(url.searchParams) };
    return (url.pathname === '/api/relay' ? relay : rpcProxy)(vreq, vres);
  }
  if (url.pathname === '/js/addresses.json') { res.setHeader('content-type', 'application/json'); return res.end(JSON.stringify(addresses)); }
  let p = join(SITE, url.pathname);
  if (existsSync(p) && statSync(p).isDirectory()) p = join(p, 'index.html');
  if (!p.startsWith(SITE) || !existsSync(p)) { res.writeHead(404); return res.end(); }
  res.setHeader('content-type', TYPES[extname(p)] || 'application/octet-stream');
  res.end(readFileSync(p));
});


const T22 = new web3.PublicKey(addresses.tokenProgram);
const ATAP = new web3.PublicKey('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL');
const TOKEN = new web3.PublicKey('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA');
const USDC = new web3.PublicKey('EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v');
const ataOf = (o, m, p) => web3.PublicKey.findProgramAddressSync([o.toBuffer(), p.toBuffer(), m.toBuffer()], ATAP)[0];
const bal = async (k) => { try { return BigInt((await conn.getTokenAccountBalance(k)).value.amount); } catch { return 0n; } };
const unitAta = ataOf(user.publicKey, new web3.PublicKey(addresses.unitMint), T22);
const legAtas = addresses.legMints.map((m) => ataOf(user.publicKey, new web3.PublicKey(m), T22));
const relUsdc = ataOf(relayer.publicKey, USDC, TOKEN);
const legs = async () => Promise.all(legAtas.map(bal));

// Real SOL price for the page (the gas service fetches its own); made-up prices for the replicas.
const sol = Number((await (await fetch('https://lite-api.jup.ag/price/v3?ids=So11111111111111111111111111111111111111112')).json())['So11111111111111111111111111111111111111112'].usdPrice);
const PRICES = { So11111111111111111111111111111111111111112: sol };
addresses.legMints.forEach((m, i) => { PRICES[m] = [150, 275, 495, 625][i]; });

const MOCK = `
window.phantom = { solana: {
  isPhantom: true,
  async connect() { return { publicKey: { toString: () => '${user.publicKey.toBase58()}' } }; },
  async signTransaction(t) { window.__kp ||= solanaWeb3.Keypair.fromSecretKey(Uint8Array.from(${JSON.stringify([...user.secretKey])})); t.sign([window.__kp]); window.__signed = (window.__signed || 0) + 1; return t; },
  async signAllTransactions(txs) { for (const t of txs) await this.signTransaction(t); return txs; },
}};`;

const browser = await chromium.launch();
const page = await browser.newPage();
await page.route('**/fonts.g*/**', (r) => r.abort());
await page.route('**/lite-api.jup.ag/price/**', (r) => {
  const ids = new URL(r.request().url()).searchParams.get('ids').split(',');
  r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(Object.fromEntries(ids.filter((i) => PRICES[i]).map((i) => [i, { usdPrice: PRICES[i] }]))) });
});
await page.route('**/lite-api.jup.ag/swap/**', (r) => r.fulfill({ status: 400, contentType: 'application/json', body: '{"error":"no route (local chain)"}' }));
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));
await page.addInitScript(MOCK);
await page.goto(`http://127.0.0.1:${site.address().port}/app/`);
const stat = async () => ((await page.locator('#left .stat').textContent().catch(() => '')) || '').trim();
const waitStat = (re, ms = 90000) => page.waitForFunction((src) => new RegExp(src, 'i').test(document.querySelector('#left .stat')?.textContent || ''), re, { timeout: ms }).catch(() => {});
await page.waitForFunction(() => /units issued/.test(document.getElementById('right')?.textContent || ''), null, { timeout: 30000 });
await page.click('[data-go="invest"]');
await page.click('[data-connect]');

// 1. The wallet box offers to add the stocks; adding them deposits through the gas service.
await page.waitForSelector('[data-loadd]', { timeout: 60000 });
ok(/stock tokens in your wallet/.test(await page.locator('#left').textContent()), 'wallet box shows the stock tokens, with "add them to the fund"');
const u0 = await bal(unitAta), l0 = await legs(), r0 = await bal(relUsdc);
await page.click('[data-loadd]');
await waitStat('done|declined|fail|error|below');
const u1 = await bal(unitAta), l1 = await legs(), r1 = await bal(relUsdc);
ok(/done/.test(await stat()), 'add them to the fund: page says done', await stat());
ok(u1 > u0, 'PAIF arrived', `${Number(u1 - u0) / 1e9} PAIF`);
ok(l1.every((x, i) => x < l0[i]), 'the stocks left the wallet', l1.map(String).join(' / '));
ok(r1 > r0, 'the gas service was repaid in USDC', `${Number(r1 - r0) / 1e6} USDC`);
ok(!(await page.locator('[data-loadd]').count()), 'wallet box goes away once only crumbs remain');

// 2. Withdraw half: step 1 (redeem) free; step 2 (sell) can't route on a local chain.
await page.click('[data-go="home"]');
await page.click('[data-go="withdraw"]');
const half = Number(u1) / 2e9;
await page.fill('#wamt', String(half));
await page.dispatchEvent('#wamt', 'change');
await page.click('[data-review="withdraw"]');
await page.waitForSelector('[data-sign]', { timeout: 30000 });
ok(/phantom asks twice/.test(await page.locator('#left').textContent()), 'review explains the two approvals');
const relSol0 = await conn.getBalance(relayer.publicKey), rU0 = await bal(relUsdc);
await page.click('[data-sign]');
await waitStat('step 1 went through|done|declined');
const msg = await stat();
const u2 = await bal(unitAta), l2 = await legs();
ok(u1 - u2 >= BigInt(Math.floor(half * 1e9)) - 1n, 'step 1: the units were handed back', `${Number(u1 - u2) / 1e9}`);
ok(l2.every((x, i) => x > l1[i]), 'step 1: the four stocks arrived', l2.map(String).join(' / '));
const spent = relSol0 - (await conn.getBalance(relayer.publicKey));
ok(spent > 0 && spent < 60_000 && (await bal(relUsdc)) === rU0, 'step 1 rode free (gas service paid only fee + tip)', `${spent} lamports`);
ok(/step 1 went through/.test(msg) && /stock tokens in your wallet/.test(msg), 'step 2 failure says step 1 went through and where to finish', msg.slice(0, 170));
await page.waitForSelector('[data-loadd]', { timeout: 30000 }).catch(() => {});
ok(await page.locator('[data-loadd]').count() === 1 && await page.locator('[data-losell]').count() === 1, 'wallet box is back, offering add or sell');

// 3. Finish by adding them back.
await page.click('[data-loadd]');
await waitStat('done\\.|declined');
const u3 = await bal(unitAta);
ok(u3 > u2, 'adding them back restores PAIF', `${Number(u3) / 1e9} PAIF · ${(await stat()).slice(0, 160)}`);
ok(errors.length === 0, 'no page errors', errors.join(' | '));

await browser.close();
site.close(); jito.close();
console.log(failures ? `\n${failures} FAILED` : '\nall checks passed');
process.exit(failures ? 1 : 0);
