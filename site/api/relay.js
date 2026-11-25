/* Permafrost gas service — Vercel serverless function.
 *
 *   GET  /api/relay              → { relayer, tipAccount, stableAccounts }
 *   POST /api/relay {txs:[b64]}  → checks, co-signs, sends the Jito bundle
 *                                  → { bundleId, signatures }
 *   GET  /api/relay?bundle=<id>  → Jito's status for that bundle
 *
 * Environment (Vercel → Settings → Environment Variables):
 *   RELAYER_SECRET_KEY  the gas wallet's secret key: Phantom's exported private
 *                       key (base58), or a JSON array of 64 numbers
 *   RPC_URL             Helius mainnet URL (never sent to the browser)
 *   PROGRAM_ID          the Permafrost program id
 *   JITO_URL            optional; default mainnet block engine
 *
 * The user signs first; this function only ever adds the gas key's signature,
 * so it can't change anything the user agreed to — and it never hands the
 * co-signed transactions back, so they can only land together, as a bundle.
 */
import web3 from '@solana/web3.js';
import bs58 from 'bs58';
import { ed25519 } from '@noble/curves/ed25519';
import { check, Refused } from './_lib/relay-core.js';
import { toBase58 } from '../js/t22.js';

const { Keypair, PublicKey, VersionedTransaction, AddressLookupTableAccount } = web3;
const JITO = process.env.JITO_URL || 'https://mainnet.block-engine.jito.wtf/api/v1';
const STABLES = {
  EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v: 'USDC',
  Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB: 'USDT',
};
const TOKEN = new PublicKey('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA');
const ATA = new PublicKey('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL');
const SOL = 'So11111111111111111111111111111111111111112';

let keypair;
/* Accepts either a JSON array of 64 numbers or the base58 string Phantom's
 * "Export private key" gives. */
const kp = () => {
  if (keypair) return keypair;
  const raw = (process.env.RELAYER_SECRET_KEY || '').trim();
  keypair = Keypair.fromSecretKey(raw.startsWith('[') ? Uint8Array.from(JSON.parse(raw)) : bs58.decode(raw));
  return keypair;
};
const ataOf = (owner, mint) => PublicKey.findProgramAddressSync([owner.toBuffer(), TOKEN.toBuffer(), new PublicKey(mint).toBuffer()], ATA)[0].toBase58();

async function rpc(method, params) {
  const r = await fetch(process.env.RPC_URL, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }) });
  const j = await r.json();
  if (j.error) throw new Error(`${method}: ${j.error.message}`);
  return j.result;
}
async function jito(method, params) {
  const r = await fetch(`${JITO}/bundles`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }) });
  const j = await r.json();
  if (j.error) throw new Error(`jito ${method}: ${j.error.message}`);
  return j.result;
}
let tipCache;
async function tipAccounts() {
  if (!tipCache) tipCache = await jito('getTipAccounts', []);
  return tipCache;
}
async function solUsd() {
  const j = await (await fetch(`https://lite-api.jup.ag/price/v3?ids=${SOL}`)).json();
  const p = Number(j?.[SOL]?.usdPrice);
  if (!(p > 0)) throw new Error('no SOL price');
  return p;
}

export default async function handler(req, res) {
  try {
    const relayer = kp().publicKey;
    const stableAccounts = Object.fromEntries(Object.keys(STABLES).map((m) => [ataOf(relayer, m), m]));

    if (req.method === 'GET' && req.query?.bundle) {
      const s = await jito('getInflightBundleStatuses', [[String(req.query.bundle)]]);
      return res.status(200).json(s);
    }
    if (req.method === 'GET') {
      const tips = await tipAccounts();
      return res.status(200).json({
        relayer: relayer.toBase58(),
        tipAccount: tips[Math.floor(Math.random() * tips.length)],
        stableAccounts,
      });
    }
    if (req.method !== 'POST') return res.status(405).json({ error: 'GET or POST' });

    const body = typeof req.body === 'string' ? JSON.parse(req.body) : req.body;
    const txs = (body?.txs || []).map((b) => VersionedTransaction.deserialize(Buffer.from(b, 'base64')));

    // Address lookup tables named by the transactions, fetched from chain.
    const altKeys = [...new Set(txs.flatMap((t) => t.message.addressTableLookups.map((l) => l.accountKey.toBase58())))];
    const tables = {};
    if (altKeys.length) {
      const got = await rpc('getMultipleAccounts', [altKeys, { encoding: 'base64' }]);
      got.value.forEach((v, i) => {
        if (v) tables[altKeys[i]] = AddressLookupTableAccount.deserialize(Buffer.from(v.data[0], 'base64')).addresses;
      });
    }
    // Which accounts the bundle creates already exist (so it pays no rent for them).
    const ataTargets = new Set();
    for (const t of txs) {
      const keys = t.message.staticAccountKeys.map((k) => k.toBase58());
      for (const ci of t.message.compiledInstructions) {
        if (keys[ci.programIdIndex] === ATA.toBase58()) ataTargets.add(keys[ci.accountKeyIndexes[1]]);
      }
    }
    const existing = new Set();
    if (ataTargets.size) {
      const list = [...ataTargets];
      const got = await rpc('getMultipleAccounts', [list, { encoding: 'base64', dataSlice: { offset: 0, length: 0 } }]);
      got.value.forEach((v, i) => { if (v) existing.add(list[i]); });
    }

    const result = check(txs, {
      relayer: relayer.toBase58(),
      programId: process.env.PROGRAM_ID,
      tipAccounts: new Set(await tipAccounts()),
      stableAccounts,
      solUsd: await solUsd(),
    }, (k) => tables[k], existing, web3);

    // Every transaction must already carry the user's VALID signature over exactly
    // what was checked — so nothing can be altered after the user agreed to it.
    for (const t of txs) {
      const sig = t.signatures[1];
      if (!sig || sig.every((b) => b === 0)) throw new Refused('missing the user signature');
      if (!ed25519.verify(sig, t.message.serialize(), t.message.staticAccountKeys[1].toBytes())) {
        throw new Refused('the user signature does not match the transaction');
      }
    }
    for (const t of txs) t.sign([kp()]);
    const encoded = txs.map((t) => Buffer.from(t.serialize()).toString('base64'));
    const bundleId = await jito('sendBundle', [encoded, { encoding: 'base64' }]);
    const signatures = txs.map((t) => toBase58(t.signatures[0]));
    return res.status(200).json({ bundleId, signatures, ...result });
  } catch (e) {
    const status = e instanceof Refused ? 400 : 500;
    return res.status(status).json({ error: String(e.message || e) });
  }
}
