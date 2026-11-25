/* End-to-end plumbing test of the gas-service function (site/api/relay.js):
 * a real invest bundle built against mainnet Jupiter, signed by a throwaway
 * user, then POSTed to the handler, which must check it, co-sign it and hand a
 * correctly double-signed bundle to "Jito" (a local stand-in, since the build
 * sandbox can't reach the block engine). Mainnet RPC is used through a small
 * local proxy that pretends the throwaway gas wallet's USDC account exists.
 *   node tools/test_relay_handler.mjs                                          */
import http from 'node:http';
import web3 from '@solana/web3.js';
import { ed25519 } from '@noble/curves/ed25519';
import { makeRouter } from '../site/js/router.js';
import { derive } from './derive_addresses.mjs';

const MAINNET = 'https://api.mainnet-beta.solana.com';
const R = web3.Keypair.generate();
const U = web3.Keypair.generate();
const TIP = '96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5';
const addresses = derive('48N2qchckkoWhmT9anrDU7bRt83QPLNBqj9JLZA7kEF2');
const TOKEN = new web3.PublicKey('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA');
const ATAP = new web3.PublicKey('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL');
const rUSDC = web3.PublicKey.findProgramAddressSync([R.publicKey.toBuffer(), TOKEN.toBuffer(), new web3.PublicKey('EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v').toBuffer()], ATAP)[0].toBase58();

const serve = (handler) => new Promise((ok) => { const s = http.createServer(handler).listen(0, () => ok(s)); });
const body = (req) => new Promise((ok) => { let d = ''; req.on('data', (c) => { d += c; }); req.on('end', () => ok(d)); });

// RPC proxy: mainnet, except the gas wallet's USDC account "exists".
const rpcSrv = await serve(async (req, res) => {
  const b = await body(req);
  const j = JSON.parse(b);
  const up = await (await fetch(MAINNET, { method: 'POST', headers: { 'content-type': 'application/json' }, body: b })).json();
  if (j.method === 'getMultipleAccounts') {
    j.params[0].forEach((k, i) => { if (k === rUSDC) up.result.value[i] = { data: ['', 'base64'], owner: TOKEN.toBase58(), lamports: 2039280, executable: false }; });
  }
  res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(up));
});
// Jito stand-in: tip accounts, and capture the bundle.
let captured = null;
const jitoSrv = await serve(async (req, res) => {
  const j = JSON.parse(await body(req));
  const result = j.method === 'getTipAccounts' ? [TIP] : j.method === 'sendBundle' ? ((captured = j.params), 'bundle-id-test') : null;
  res.setHeader('content-type', 'application/json'); res.end(JSON.stringify({ jsonrpc: '2.0', id: 1, result }));
});
const RPC = `http://127.0.0.1:${rpcSrv.address().port}`;
process.env.RPC_URL = RPC;
process.env.JITO_URL = `http://127.0.0.1:${jitoSrv.address().port}/api/v1`;
process.env.PROGRAM_ID = addresses.programId;
process.env.RELAYER_SECRET_KEY = JSON.stringify([...R.secretKey]);
const { default: handler } = await import('../site/api/relay.js');

const call = (method, payload, query = {}) => new Promise((ok) => {
  const res = { code: 0, status(c) { this.code = c; return this; }, json(o) { ok({ code: this.code, body: o }); return this; }, setHeader() { return this; } };
  handler({ method, body: payload, query }, res);
});

let failures = 0;
const ok = (c, label, detail = '') => { console.log(`${c ? '  pass' : '  FAIL'}  ${label}${detail ? '  ' + detail : ''}`); if (!c) failures++; };

const info = await call('GET');
ok(info.code === 200 && info.body.relayer === R.publicKey.toBase58() && info.body.tipAccount === TIP, 'GET returns the gas wallet and a tip account');

const router = makeRouter({ web3, rpc: RPC, addresses, relayer: R.publicKey.toBase58(),
  fundOverride: { holdings: [2915247n, 1840129n, 1179223n, 880579n], supply: 25_000_000_000n, exitBp: 0n } });
const { txs } = await router.buildInvest({ user: U.publicKey.toBase58(), stable: 'USDC', amount: 20_000_000, tipAccount: info.body.tipAccount });

const unsigned = await call('POST', { txs: txs.map((t) => Buffer.from(t.serialize()).toString('base64')) });
ok(unsigned.code === 400 && /user signature/.test(unsigned.body.error), 'refuses a bundle the user has not signed', unsigned.body.error);

txs.forEach((t) => t.sign([U]));
const r = await call('POST', { txs: txs.map((t) => Buffer.from(t.serialize()).toString('base64')) });
ok(r.code === 200 && r.body.bundleId === 'bundle-id-test', 'accepts, co-signs and submits the signed bundle', r.body.error || `cost $${(r.body.costLamports / 1e9 * 69).toFixed(3)} ≈, paid $${r.body.paidUsd?.toFixed(3)}`);
if (captured) {
  const sent = captured[0].map((b) => web3.VersionedTransaction.deserialize(Buffer.from(b, 'base64')));
  const allValid = sent.every((t) => {
    const msg = t.message.serialize();
    return t.message.staticAccountKeys.slice(0, 2).every((k, i) => ed25519.verify(t.signatures[i], msg, k.toBytes()));
  });
  ok(sent.length === 5 && allValid, 'Jito receives 5 transactions, each validly signed by gas wallet and user');
}
const tampered = txs.map((t) => Buffer.from(t.serialize()).toString('base64'));
const bad = web3.VersionedTransaction.deserialize(Buffer.from(tampered[4], 'base64'));
bad.message.compiledInstructions.at(-1).data[4] = 0xff; // inflate the tip after the user signed
tampered[4] = Buffer.from(bad.serialize()).toString('base64');
const t2 = await call('POST', { txs: tampered });
ok(t2.code === 400, 'refuses a bundle altered after the user signed (tip inflated)', t2.body.error);

rpcSrv.close(); jitoSrv.close();
console.log(failures ? `\n${failures} FAILED` : '\nall checks passed');
process.exit(failures ? 1 : 0);
