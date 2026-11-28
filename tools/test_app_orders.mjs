/* v4 one-signature invest / withdraw, END TO END on a local validator (28 Nov).
 *
 * Real: the app page, router.js (buildOrderInvest/buildOrderRedeem), /api/order
 * (the gas service as filler, with its own transaction compiler), the v4 program
 * (mockswap build: SWAP_PROGRAM = the mock), the mock swap program at today's real
 * prices. Stand-ins: Phantom (signs with a local key), Jupiter's quotes (the router
 * still asks the real Jupiter API for min-outs on the real mint addresses; the
 * mock then fills at the real current price so those minimums are met).
 *
 * Checks: $100 USDC in with ONE signature → units arrive, no stock tokens in the
 * wallet, escrow closed, gas wallet repaid (or not, under the subsidy); withdraw
 * all with ONE signature → USDC back; a cancelled (stalled) order refunds.
 *
 *   node tools/test_app_orders.mjs USER_KEY.json GAS_KEY.json
 * Needs: solana-test-validator on PATH, program/target/deploy-mock/{permafrost,mockswap}.so,
 * ~/pf-scratch/orders-accounts (tests/local_orders_setup.py accounts …). */
import http from 'node:http';
import { spawn, execSync } from 'node:child_process';
import { readFileSync, existsSync, statSync, readdirSync } from 'node:fs';
import { join, extname, resolve } from 'node:path';
import { createRequire } from 'node:module';
import web3 from '@solana/web3.js';
import { derive } from './derive_addresses.mjs';

const { chromium } = createRequire(import.meta.url)('playwright');
const ROOT = resolve(new URL('..', import.meta.url).pathname);
const SITE = join(ROOT, 'site');
const userKey = process.argv[2], gasKey = process.argv[3];
const user = web3.Keypair.fromSecretKey(Uint8Array.from(JSON.parse(readFileSync(userKey, 'utf8'))));
const gas = web3.Keypair.fromSecretKey(Uint8Array.from(JSON.parse(readFileSync(gasKey, 'utf8'))));
const ACCTS = resolve(process.env.HOME, 'pf-scratch/orders-accounts');
const keys = JSON.parse(execSync('PF_MOCK=1 python3 -I tests/local_orders_setup.py keys', { cwd: join(ROOT, 'program') }).toString());
const RPC = 'http://127.0.0.1:8899';

let failures = 0;
const ok = (c, label, detail = '') => { console.log(`${c ? '  pass' : '  FAIL'}  ${label}${detail ? '  ' + detail : ''}`); if (!c) failures++; };

// ---- 1. the chain
try { execSync('pkill -x solana-test-val'); } catch {}
await new Promise((r) => setTimeout(r, 1500));
const accountArgs = readdirSync(ACCTS).filter((f) => f.endsWith('.json')).flatMap((f) => ['--account', '-', join(ACCTS, f)]);
const validator = spawn('solana-test-validator', ['--ledger', '/tmp/claude-0/ledger-orders', '--reset', '--quiet',
  '--bpf-program', 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb', join(ROOT, 'program/fixtures/token_2022.so'),
  '--bpf-program', 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL', join(ROOT, 'program/fixtures/associated_token.so'),
  '--upgradeable-program', keys.program, join(ROOT, 'program/target/deploy-mock/permafrost.so'), keys.admin,
  '--bpf-program', keys.mock, join(ROOT, 'program/target/deploy-mock/mockswap.so'),
  ...accountArgs], { stdio: 'ignore' });
const conn = new web3.Connection(RPC, 'confirmed');
for (let i = 0; i < 90; i++) { try { await conn.getVersion(); break; } catch { await new Promise((r) => setTimeout(r, 1000)); } }
for (const k of [user.publicKey, gas.publicKey]) await conn.confirmTransaction(await conn.requestAirdrop(k, 5e9), 'confirmed');
console.log(execSync('PF_MOCK=1 PF_RPC=http://localhost:8899 python3 -I tests/local_orders_setup.py launch', { cwd: join(ROOT, 'program') }).toString().trim().split('\n').slice(-2).join('\n'));

// ---- 2. prices: today's, so the router's real-Jupiter minimums are met by the mock
const PRICE_URL = 'https://lite-api.jup.ag/price/v3?ids=' + keys.legs.join(',');
const pj = await (await fetch(PRICE_URL)).json();
const prices = keys.legs.map((m) => Number(pj[m].usdPrice));
console.log('  prices', prices.map((p) => '$' + p.toFixed(2)).join(' '));

// ---- 3. the site, with the real /api/order and /api/relay handlers
process.env.RPC_URL = RPC;
process.env.PROGRAM_ID = keys.program;
process.env.RELAYER_SECRET_KEY = JSON.stringify([...gas.secretKey]);
process.env.SWAP_MODE = 'mock';
process.env.MOCK_PRICES = JSON.stringify(prices);
process.env.JITO_URL = 'http://127.0.0.1:1/api/v1'; // the old relay must not be used
const order = (await import('../site/api/order.js')).default;
const relay = (await import('../site/api/relay.js')).default;
const rpcProxy = (await import('../site/api/rpc.js')).default;
const addresses = { ...derive(keys.program), orders: true };
const body = (req) => new Promise((done) => { let d = ''; req.on('data', (c) => { d += c; }); req.on('end', () => done(d)); });
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.json': 'application/json', '.css': 'text/css' };
const site = await new Promise((done) => {
  const s = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://x');
    if (url.pathname.startsWith('/api/')) {
      const raw = req.method === 'POST' ? await body(req) : '';
      const vres = { statusCode: 200, status(c) { this.statusCode = c; return this; }, setHeader(k, v) { res.setHeader(k, v); return this; },
        json(o) { res.writeHead(this.statusCode, { 'content-type': 'application/json' }); res.end(JSON.stringify(o)); return this; },
        send(t) { res.writeHead(this.statusCode); res.end(t); return this; } };
      const vreq = { method: req.method, body: raw ? JSON.parse(raw) : undefined, query: Object.fromEntries(url.searchParams) };
      const h = url.pathname === '/api/order' ? order : url.pathname === '/api/relay' ? relay : rpcProxy;
      try { return await h(vreq, vres); } catch (e) { return vres.status(500).json({ error: String(e.message || e) }); }
    }
    if (url.pathname === '/js/addresses.json') { res.setHeader('content-type', 'application/json'); return res.end(JSON.stringify(addresses)); }
    let p = join(SITE, url.pathname);
    if (existsSync(p) && statSync(p).isDirectory()) p = join(p, 'index.html');
    if (!p.startsWith(SITE) || !existsSync(p)) { res.writeHead(404); return res.end(); }
    res.setHeader('content-type', TYPES[extname(p)] || 'application/octet-stream');
    res.end(readFileSync(p));
  }).listen(0, '127.0.0.1', () => done(s));
});
const base = `http://127.0.0.1:${site.address().port}`;

const T22 = new web3.PublicKey(addresses.tokenProgram), TOKEN = new web3.PublicKey('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA');
const ATAP = new web3.PublicKey('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL');
const USDC = new web3.PublicKey('EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v');
const ataOf = (o, m, p) => web3.PublicKey.findProgramAddressSync([o.toBuffer(), p.toBuffer(), m.toBuffer()], ATAP)[0];
const bal = async (k) => { try { return BigInt((await conn.getTokenAccountBalance(k)).value.amount); } catch { return 0n; } };
const unitAta = ataOf(user.publicKey, new web3.PublicKey(addresses.unitMint), T22);
const userUsdc = ataOf(user.publicKey, USDC, TOKEN), gasUsdc = ataOf(gas.publicKey, USDC, TOKEN);
const legs = async () => Promise.all(keys.legs.map((m) => bal(ataOf(user.publicKey, new web3.PublicKey(m), T22))));
const openOrders = async () => (await conn.getProgramAccounts(new web3.PublicKey(keys.program), { filters: [{ dataSize: 219 }] })).length;

// ---- 4. the page, with a Phantom stand-in
const MOCK = `window.phantom = { solana: { isPhantom: true,
  async connect() { return { publicKey: { toString: () => '${user.publicKey.toBase58()}' } }; },
  async signTransaction(t) { window.__kp ||= solanaWeb3.Keypair.fromSecretKey(Uint8Array.from(${JSON.stringify([...user.secretKey])})); t.sign([window.__kp]); window.__signed = (window.__signed || 0) + 1; return t; },
  async signAllTransactions(txs) { for (const t of txs) await this.signTransaction(t); return txs; } } };`;
const browser = await chromium.launch();
const page = await browser.newPage();
await page.route('**/fonts.g*/**', (r) => r.abort());
const errors = [];
page.on('pageerror', (e) => errors.push(String(e))); page.on('console', (m) => { if (m.type() === 'error') console.log('CONSOLE', m.text().slice(0, 200)); });
await page.addInitScript(MOCK);
await page.goto(`${base}/app/`);
const stat = async () => ((await page.locator('#left .stat').textContent().catch(() => '')) || '').trim();
const waitStat = (re, ms = 120000) => page.waitForFunction((src) => new RegExp(src, 'i').test(document.querySelector('#left .stat')?.textContent || ''), re, { timeout: ms }).catch(() => {});
await page.waitForFunction(() => /units issued/.test(document.getElementById('right')?.textContent || ''), null, { timeout: 30000 });
const pol = await (await fetch(`${base}/api/order`)).json();
ok(pol.policy && pol.policy.subsidized === true && pol.policy.feeUsd === 0, 'gas service: subsidy on (holders < 50)', JSON.stringify(pol.policy));

// ---- 5. invest $100 with one signature
await page.click('[data-go="invest"]');
await page.click('[data-connect]');
await page.waitForSelector('#amt', { timeout: 30000 });
await page.fill('#amt', '100');
await page.dispatchEvent('#amt', 'change');
await page.click('[data-review="invest"]');
await page.waitForSelector('[data-sign]', { timeout: 60000 }).catch(async () => { console.log('REVIEW FAILED:', (await page.locator('#left').textContent()).replace(/\s+/g, ' ').slice(0, 400)); });
const review = await page.locator('#left').textContent();
ok(/one approval in phantom/.test(review) && /free \(first 50 wallets\)/.test(review), 'review: one approval, fee free under the subsidy');
const u0 = await bal(unitAta), usdc0 = await bal(userUsdc), gasUsdc0 = await bal(gasUsdc), gasSol0 = await conn.getBalance(gas.publicKey);
await page.click('[data-sign]');
await waitStat('done\\.|refunded|declined|error|fail');
const msg = await stat();
const signed = await page.evaluate(() => window.__signed || 0);
ok(/done\./.test(msg), 'invest completed', msg.slice(0, 120));
ok(signed === 1, 'exactly one Phantom signature', String(signed));
const u1 = await bal(unitAta);
ok(u1 > u0, 'units arrived', `${Number(u1 - u0) / 1e9} units for $100`);
ok((await legs()).every((x) => x === 0n), 'no stock tokens in the user wallet');
ok(usdc0 - (await bal(userUsdc)) <= 100_000_000n, 'user paid at most $100 (change returned)', `${Number(usdc0 - (await bal(userUsdc))) / 1e6}`);
ok((await bal(gasUsdc)) === gasUsdc0, 'subsidy: gas wallet took no fee');
ok(await openOrders() === 0, 'escrow closed');
const gasSpent = gasSol0 - (await conn.getBalance(gas.publicKey));
ok(gasSpent > 0 && gasSpent < 5_000_000, 'gas wallet paid only tx fees + the PAIF account (rent came back)', `${gasSpent} lamports`);

// ---- 6. withdraw everything with one signature
await page.click('[data-go="home"]');
await page.click('[data-go="withdraw"]');
await page.waitForSelector('[data-wmax]', { timeout: 30000 });
await page.click('[data-wmax]');
await page.dispatchEvent('#wamt', 'change');
await page.click('[data-review="withdraw"]');
await page.waitForSelector('[data-sign]', { timeout: 60000 });
ok(/one approval in phantom/.test(await page.locator('#left').textContent()), 'withdraw review: one approval');
const usdc1 = await bal(userUsdc);
await page.click('[data-sign]');
await waitStat('done\\.|refunded|declined|error|fail');
const msg2 = await stat();
ok(/done\./.test(msg2), 'withdraw completed', msg2.slice(0, 120));
ok(await page.evaluate(() => window.__signed) === 2, 'one more Phantom signature');
const back = (await bal(userUsdc)) - usdc1;
ok(back > 95_000_000n && back <= 100_000_000n, 'USDC back (rounding and the 0.1% sale buffer only)', `$${Number(back) / 1e6}`);
ok((await bal(unitAta)) === 0n, 'all units handed back');
ok((await legs()).every((x) => x === 0n), 'still no stock tokens in the user wallet');
ok(await openOrders() === 0, 'escrow closed');

// ---- 7. a stalled order refunds: fills refused (mock paid nothing) → expiry → sweep cancels
process.env.MOCK_PRICES = JSON.stringify(prices.map((p) => p * 100)); // the mock now delivers 1% of the stock: below every minimum
await page.click('[data-go="home"]'); await page.click('[data-go="invest"]');
await page.waitForSelector('#amt', { timeout: 30000 });
await page.fill('#amt', '20'); await page.dispatchEvent('#amt', 'change');
await page.click('[data-review="invest"]'); await page.waitForSelector('[data-sign]', { timeout: 60000 });
const usdc2 = await bal(userUsdc);
await page.click('[data-sign]');
await waitStat('buying the four stocks', 60000);
await new Promise((r) => setTimeout(r, 6000));
ok(await openOrders() === 1, 'order open, fills refused by the program (below minimum)', await stat());
ok(usdc2 - (await bal(userUsdc)) === 20_000_000n, '$20 escrowed meanwhile');
// Jump the clock past expiry: the validator's clock can't be warped, so cancel as the owner instead (same code path as the sweep for an expired order).
const open = await conn.getProgramAccounts(new web3.PublicKey(keys.program), { filters: [{ dataSize: 219 }] });
const orderKey = open[0].pubkey.toBase58();
// Build the owner's cancel through the router and submit it through /api/order.
const cancelRes = await page.evaluate(async ({ orderKey }) => {
  const { makeRouter } = await import('/js/router.js');
  const addresses = await (await fetch('/js/addresses.json')).json();
  const relay = await (await fetch('/api/order')).json();
  const router = makeRouter({ web3: window.solanaWeb3, rpc: '/api/rpc', addresses, relayer: relay.relayer });
  const { txs } = await router.buildCancelOrder({ user: window.__kp.publicKey.toBase58(), order: orderKey, stable: 'USDC' });
  txs[0].sign([window.__kp]);
  const enc = btoa(String.fromCharCode(...txs[0].serialize()));
  return await (await fetch('/api/order', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ tx: enc }) })).json();
}, { orderKey });
ok(cancelRes.status === 'cancelled', 'owner cancel through /api/order', JSON.stringify(cancelRes).slice(0, 120));
ok((await bal(userUsdc)) === usdc2, 'the $20 came back in full');
ok(await openOrders() === 0, 'escrow closed');
ok(errors.length === 0, 'no page errors', errors.join(' | '));

await browser.close();
site.close();
validator.kill();
console.log(failures ? `\n${failures} FAILED` : '\nall checks passed');
process.exit(failures ? 1 : 0);
