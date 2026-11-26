/* The app page's locked-deposit screens, end to end, against a LOCAL validator.
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
const relayer = web3.Keypair.generate();
const tip = web3.Keypair.generate().publicKey;
const conn = new web3.Connection(LOCAL, 'confirmed');
const addresses = derive(PROGRAM, { devnet: true });

let failures = 0;
const ok = (c, label, detail = '') => { console.log(`${c ? '  pass' : '  FAIL'}  ${label}${detail ? '  ' + detail : ''}`); if (!c) failures++; };

for (const k of [relayer.publicKey, tip]) await conn.confirmTransaction(await conn.requestAirdrop(k, 2e9), 'confirmed');

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

// Read the user's position and balances straight from the validator.
const unitAta = web3.PublicKey.findProgramAddressSync(
  [user.publicKey.toBuffer(), new web3.PublicKey(addresses.tokenProgram).toBuffer(), new web3.PublicKey(addresses.unitMint).toBuffer()],
  new web3.PublicKey('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'))[0];
const units = async (k) => BigInt((await conn.getTokenAccountBalance(k)).value.amount);
const positionPda = (id) => { const b = Buffer.alloc(8); b.writeBigUInt64LE(BigInt(id)); return web3.PublicKey.findProgramAddressSync([Buffer.from('position'), b], new web3.PublicKey(PROGRAM))[0]; };
// The user's open position (the newest, if a previous run left others closed).
const mine = await conn.getProgramAccounts(new web3.PublicKey(PROGRAM), { commitment: 'confirmed',
  filters: [{ dataSize: 81 }, { memcmp: { offset: 16, bytes: user.publicKey.toBase58() } }] });
ok(mine.length === 1, 'the user has exactly one open position on the local chain', String(mine.length));
const posId = mine[0].account.data.readBigUInt64LE(8);
const pos0 = mine[0].account;
const principal = pos0.data.readBigUInt64LE(8 + 8 + 32);
const bonus = pos0.data.readBigUInt64LE(8 + 8 + 32 + 8);
const before = { user: await units(unitAta), pool: await units(new web3.PublicKey(addresses.pool)), relayer: await conn.getBalance(relayer.publicKey) };

// Phantom stand-in: connect + signAllTransactions, signing with the user's key.
const MOCK = `
window.phantom = { solana: {
  isPhantom: true,
  async connect() { return { publicKey: { toString: () => '${user.publicKey.toBase58()}' } }; },
  async signAllTransactions(txs) {
    const kp = solanaWeb3.Keypair.fromSecretKey(Uint8Array.from(${JSON.stringify([...user.secretKey])}));
    for (const t of txs) t.sign([kp]);
    window.__signed = (window.__signed || 0) + txs.length;
    return txs;
  },
}};`;

const browser = await chromium.launch();
const page = await browser.newPage();
await page.route('**/fonts.g*/**', (r) => r.abort());
await page.route('**/lite-api.jup.ag/**', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: '{}' }));
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));
await page.addInitScript(MOCK);
await page.goto(`http://127.0.0.1:${site.address().port}/app/`);

await page.waitForFunction(() => document.getElementById('figs')?.textContent.includes('PAIF in issue'), null, { timeout: 30000 });
ok(true, 'fund figures load through /api/rpc');
const lockRow = page.locator('#lock-row');
ok(await lockRow.isVisible(), 'lock option is shown (bonus on, pool funded)');
ok(await page.locator('#pane-withdraw').isHidden(), 'withdraw form hidden while the Invest tab is selected');
const lockText = await page.locator('#lock-text').textContent();
ok(/Lock for 30 days/.test(lockText) && /\+0\.9% in PAIF/.test(lockText) && /up to 0\.9 PAIF/.test(lockText), 'lock option states the vault\'s real terms', JSON.stringify(lockText));

await page.click('#connect');
await page.waitForSelector('#positions:not([hidden]) button[data-pos]', { timeout: 30000 });
const row = await page.locator('#positions tbody tr').first().textContent();
ok(await page.locator('#positions tbody tr').count() === 1, 'Locked list shows exactly the one position');
ok(/Leave early/.test(row), 'not yet matured, so the button is "Leave early"', row.replace(/\s+/g, ' ').trim());
ok(row.includes((Number(principal) / 1e9).toLocaleString('en-US', { maximumFractionDigits: 4 })), 'row shows the principal', String(Number(principal) / 1e9));
ok(row.includes((Number(bonus) / 1e9).toLocaleString('en-US', { maximumFractionDigits: 4 })), 'row shows the bonus', String(Number(bonus) / 1e9));

await page.click('#positions button[data-kind="exit"]');
await page.waitForSelector('#settle-no');
await page.click('#settle-no');
ok((await page.locator('#out-settle').textContent()).trim() === '', '"Keep it locked" cancels without signing', `signed ${await page.evaluate(() => window.__signed || 0)}`);

await page.click('#positions button[data-kind="exit"]');
await page.click('#settle-go');
await page.waitForFunction(() => /Done\.|failed|declined|Error|not|below/i.test(document.getElementById('st-settle')?.textContent || document.getElementById('out-settle')?.textContent || ''), null, { timeout: 90000 }).catch(() => {});
const status = (await page.locator('#out-settle').textContent().catch(() => '')) || '';
ok(/Done\./.test(status), 'Leave early completes through the gas service', status.trim().slice(0, 200));
ok(bundles === 1, 'exactly one bundle reached "Jito"', String(bundles));

const after = { user: await units(unitAta), pool: await units(new web3.PublicKey(addresses.pool)), relayer: await conn.getBalance(relayer.publicKey) };
ok(!(await conn.getAccountInfo(positionPda(posId))), 'position account closed on chain', `position ${posId}`);
ok(after.user - before.user === principal, 'user got the principal back in full', `${after.user - before.user} = ${principal}`);
ok(after.pool - before.pool === bonus, 'bonus returned to the pool', `${after.pool - before.pool} = ${bonus}`);
const spent = before.relayer - after.relayer;
ok(spent > 0 && spent <= 100_000 + 15_000, 'gas service paid only fee + tip (lone exit rides free)', `${spent} lamports`);
await page.waitForFunction(() => document.getElementById('positions').hidden, null, { timeout: 15000 }).catch(() => {});
ok(await page.locator('#positions').isHidden(), 'Locked list disappears after the refresh');
ok(errors.length === 0, 'no page errors', errors.join(' | '));

await browser.close();
site.close(); jito.close();
console.log(failures ? `\n${failures} FAILED` : '\nall checks passed');
process.exit(failures ? 1 : 0);
