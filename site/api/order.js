/* v4 orders endpoint (28 Nov 2026).
 *
 *   GET  /api/order                → { relayer, policy: { subsidized, holders, feeUsd, maxFeeUsd }, open }
 *   POST /api/order { tx }         → the user's ONE signed transaction (order_invest,
 *                                    order_redeem or cancel_order, gas service as fee
 *                                    payer and rent payer): checked, co-signed, sent,
 *                                    and the order advanced → { signature, order, ...state }
 *   POST /api/order { step }       → advance that order (fills, then settle) → state
 *   GET  /api/order?sweep=1        → settle complete / cancel expired orders this key funded
 *
 * What the gas service risks on an order: the escrow rent (~0.008 SOL, back at
 * close), the fill/settle fees, and — for the first SUBSIDY_WALLETS holders —
 * the fee it would otherwise charge. It never holds the user's money.
 *
 * Env: RELAYER_SECRET_KEY, RPC_URL, PROGRAM_ID, LEG_MINTS (comma list; default
 * mainnet xStocks), JUPITER_URL, SWAP_MODE=mock + MOCK_PRICES (local tests only). */
import bs58 from 'bs58';
import { parseTransaction, keypairFromSecret, verify, ataOf, Key } from './_lib/tx.js';
import { derive, makeFiller, holderCount, decodeOrder, DISC, STABLES, PROGRAMS, orderPda, ORDER_SIZE } from './_lib/orders.js';

export const config = { maxDuration: 60 };

const MAINNET_LEGS = ['Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh', 'XsCPL9dNWBMvFtTmwcCA5v3xWPSMEBCszbQdiLLq6aN',
  'XspzcW1PRtgf6Wj92HCiZdjzKCyFekVD8P5Ueh3dRMX', 'Xsa62P5mvPszXL1krVUnU5ar38bBSVcWAB6fmPCo5Zu'];
const SUBSIDY_WALLETS = 50;          // Vishal, 28 Nov: the first 50 wallets pay nothing
const FEE_USD = 0.15;                // after that: covers ~6 transactions + the PAIF account
const MAX_OPEN = 25;                 // escrow rent the gas service will float at once
const LIMITS = { MAX_CU_PRICE: 50_000, MAX_CU_LIMIT: 1_400_000 };
const SOL_MINT = 'So11111111111111111111111111111111111111112';

let keypair = null;
const kp = () => (keypair ||= keypairFromSecret(process.env.RELAYER_SECRET_KEY || ''));
const disc8 = (d) => Array.from(d.slice(0, 8)).join(',');
const DISC_INDEX = Object.fromEntries(Object.entries(DISC).map(([n, d]) => [disc8(d), n]));

async function rpc(method, params) {
  const r = await fetch(process.env.RPC_URL, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }) });
  const j = await r.json();
  if (j.error) throw new Error(`rpc ${method}: ${j.error.message}`);
  return j.result;
}
let solCache = { at: 0, usd: 0 };
async function solUsd() {
  if (Date.now() - solCache.at < 60_000 && solCache.usd) return solCache.usd;
  const j = await (await fetch(`${process.env.JUPITER_URL || 'https://lite-api.jup.ag'}/price/v3?ids=${SOL_MINT}`)).json();
  const p = Number(j?.[SOL_MINT]?.usdPrice);
  if (!(p > 0)) throw new Error('no SOL price');
  solCache = { at: Date.now(), usd: p };
  return p;
}
let holdersCache = { at: 0, n: 0 };
async function policy(A) {
  if (Date.now() - holdersCache.at > 60_000) {
    try { holdersCache = { at: Date.now(), n: await holderCount(rpc, A) }; } catch { /* keep the old count */ }
  }
  const subsidized = holdersCache.n < SUBSIDY_WALLETS;
  return { subsidized, holders: holdersCache.n, subsidyWallets: SUBSIDY_WALLETS, feeUsd: subsidized ? 0 : FEE_USD, maxFeeUsd: FEE_USD };
}

class Refused extends Error {}
const refuse = (m) => { throw new Refused(m); };

/* The user's order transaction: exactly one of ours, gas key only as fee payer
 * and (for order_*) as the rent payer at account 1; nothing else may touch it. */
function checkOrderTx(tx, gasB58, programId) {
  const m = tx.message;
  if (m.version !== 0) refuse('must be a v0 transaction');
  if (m.addressTableLookups.length) refuse('order transactions use no lookup tables');
  const keys = m.staticAccountKeys.map((k) => k.toBase58());
  if (keys[0] !== gasB58) refuse('the gas service must be the fee payer');
  if (m.header.numRequiredSignatures !== 2) refuse('exactly two signers (gas service, user)');
  const user = keys[1];
  if (user === gasB58) refuse('user must not be the gas key');
  let ours = null;
  for (const ci of m.compiledInstructions) {
    const prog = keys[ci.programIdIndex];
    const acc = ci.accountKeyIndexes.map((i) => keys[i]);
    if (prog === PROGRAMS.computeBudget) {
      const d = ci.data;
      if (d[0] === 2 && new DataView(d.buffer, d.byteOffset).getUint32(1, true) > LIMITS.MAX_CU_LIMIT) refuse('compute limit above the cap');
      if (d[0] === 3 && Number(new DataView(d.buffer, d.byteOffset).getBigUint64(1, true)) > LIMITS.MAX_CU_PRICE) refuse('priority fee above the cap');
      if (d[0] !== 2 && d[0] !== 3) refuse('only compute limit / price');
      continue;
    }
    if (prog !== programId) refuse(`program ${prog} not allowed`);
    if (ours) refuse('one order instruction per transaction');
    const name = DISC_INDEX[disc8(ci.data)];
    if (name === 'order_invest' || name === 'order_redeem') {
      if (acc[0] !== user) refuse('the user must be the order owner');
      if (acc[1] !== gasB58) refuse('the gas service must be the rent payer');
      if (acc.slice(2).includes(gasB58)) refuse('gas key in another position');
      const dv = new DataView(ci.data.buffer, ci.data.byteOffset);
      const expires = Number(dv.getBigInt64(ci.data.length - 8, true));
      if (expires > Math.floor(Date.now() / 1000) + 3600) refuse('expiry too far out');
      ours = { name, user, id: dv.getBigUint64(8, true), expires };
    } else if (name === 'cancel_order') {
      // [canceller, order, owner, payer, …]: the gas key is the rent payer (account
      // 3, where the program's has_one puts it) and receives the rent back; the
      // escrow's contents can only go to the owner. Nowhere else.
      if (acc[0] !== user) refuse('only the owner cancels through here');
      if (acc.some((k, i) => k === gasB58 && i !== 3)) refuse('gas key in another position');
      ours = { name, user };
    } else {
      refuse(`instruction ${name || 'unknown'} not allowed here`);
    }
  }
  if (!ours) refuse('no order instruction');
  return ours;
}

export default async function handler(req, res) {
  if (!process.env.RELAYER_SECRET_KEY || !process.env.RPC_URL || !process.env.PROGRAM_ID) {
    return res.status(503).json({ error: 'gas service not configured (RELAYER_SECRET_KEY, RPC_URL, PROGRAM_ID)' });
  }
  try {
    const gas = kp();
    const gasB58 = gas.publicKey.toBase58();
    const legs = (process.env.LEG_MINTS || MAINNET_LEGS.join(',')).split(',');
    const A = derive(process.env.PROGRAM_ID, legs);
    const filler = makeFiller({ A, gas, rpc, jupiter: process.env.JUPITER_URL || 'https://lite-api.jup.ag',
      swapMode: process.env.SWAP_MODE || 'jupiter', mockPrices: process.env.MOCK_PRICES ? JSON.parse(process.env.MOCK_PRICES) : null });
    const feeToFor = (stableMintB58) => Key.from(ataOf(gas.publicKey, stableMintB58));

    if (req.method === 'GET' && req.query?.sweep) {
      const out = [];
      for (const r of await filler.sweep({ feeTo: null })) out.push(r);
      return res.status(200).json({ swept: out });
    }
    if (req.method === 'GET') {
      return res.status(200).json({ relayer: gasB58, policy: await policy(A), open: await filler.openCount(), swapMode: process.env.SWAP_MODE || 'jupiter' });
    }
    if (req.method !== 'POST') return res.status(405).json({ error: 'GET or POST' });
    const body = typeof req.body === 'string' ? JSON.parse(req.body) : req.body;

    if (body?.step) {
      const o = await filler.readOrder(String(body.step));
      if (!o) return res.status(200).json({ status: 'closed' });
      const state = await filler.step(String(body.step), { feeTo: feeToFor(o.stableMint.toBase58()) });
      return res.status(200).json({ order: String(body.step), ...state });
    }

    if (!body?.tx) return res.status(400).json({ error: 'tx or step' });
    const tx = parseTransaction(Buffer.from(body.tx, 'base64'));
    const ours = checkOrderTx(tx, gasB58, process.env.PROGRAM_ID);
    const sig = tx.signatures[1];
    if (!sig || sig.every((b) => b === 0)) throw new Refused('missing the user signature');
    if (!verify(sig, tx.message.serialize(), tx.message.staticAccountKeys[1].toBytes())) throw new Refused('the user signature does not match the transaction');
    if (ours.name !== 'cancel_order' && (await filler.openCount()) >= MAX_OPEN) throw new Refused('too many orders in flight; try again in a minute');
    const fresh = await rpc('isBlockhashValid', [tx.message.recentBlockhash, { commitment: 'processed' }]);
    if (fresh && fresh.value === false) return res.status(409).json({ error: 'expired', message: 'The order expired before sending (over a minute passed). Nothing was sent.' });

    tx.signatures[0] = gas.sign(tx.message.serialize());
    const signature = await rpc('sendTransaction', [Buffer.from(tx.serialize()).toString('base64'), { encoding: 'base64', preflightCommitment: 'confirmed', maxRetries: 3 }]);
    let landed = false;
    for (let i = 0; i < 30 && !landed; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      const st = (await rpc('getSignatureStatuses', [[signature]])).value[0];
      if (st && (st.confirmationStatus === 'confirmed' || st.confirmationStatus === 'finalized')) {
        if (st.err) return res.status(400).json({ error: `order transaction failed on chain: ${JSON.stringify(st.err)}`, signature });
        landed = true;
      }
    }
    if (!landed) return res.status(504).json({ error: 'order not confirmed within 30 s', signature });
    if (ours.name === 'cancel_order') return res.status(200).json({ signature, status: 'cancelled' });
    const orderB58 = orderPda(A, Key.from(ours.user), ours.id).toBase58();
    // Start filling straight away; the client keeps calling { step } until settled.
    let state;
    try {
      const o = await filler.readOrder(orderB58);
      state = o ? await filler.step(orderB58, { feeTo: feeToFor(o.stableMint.toBase58()), maxFills: 2 }) : { status: 'closed' };
    } catch (e) { state = { status: 'filling', events: [{ error: String(e.message || e) }], retry: true }; }
    return res.status(200).json({ signature, order: orderB58, ...state });
  } catch (e) {
    const status = e instanceof Refused ? 400 : 500;
    return res.status(status).json({ error: String(e.message || e) });
  }
}
