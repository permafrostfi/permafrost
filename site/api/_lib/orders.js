/* v4 orders — the gas service as FILLER (28 Nov 2026).
 *
 * After a user's order lands (one signature: order_invest / order_redeem), the
 * gas service does the rest with its own key: fetches a Jupiter route for each
 * unfilled leg, sends a fill_leg transaction per leg, then settle. The program
 * enforces the user's budget / minimum per leg and min_units at settle, so a
 * mistake here can only waste gas, never the user's money. Stalled orders are
 * cancelled after expiry (full refund to the owner, rent back to this key).
 *
 * No web3.js (it does not load on Vercel): tx.js is the codec. */
import { Key, findProgramAddress, ataOf, compileV0, serializeSigned, lookupTableAddresses, u64le, u32le, cat, anchorDisc, meta } from './tx.js';

export const PROGRAMS = {
  token: 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
  token2022: 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb',
  ata: 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL',
  system: '11111111111111111111111111111111',
  computeBudget: 'ComputeBudget111111111111111111111111111111',
  jupiter: 'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4',
  mockswap: 'E9KwSa1oVgko2b3AqLsD8zU3ppKWhth12U8tnFWNpXzG', // local tests only
};
export const STABLES = {
  EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v: { name: 'USDC', decimals: 6 },
  Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB: { name: 'USDT', decimals: 6 },
};
export const DISC = {
  order_invest: anchorDisc('order_invest'), order_redeem: anchorDisc('order_redeem'), fill_leg: anchorDisc('fill_leg'),
  settle_invest: anchorDisc('settle_invest'), settle_redeem: anchorDisc('settle_redeem'), cancel_order: anchorDisc('cancel_order'),
};
export const ORDER_SIZE = 8 + 3 + 96 + 32 + 32 + 32 + 16;
export const KIND_INVEST = 0, KIND_REDEEM = 1, ALL_LEGS = 0b1111;
export const FEE_OWNER = 'E3FTvAEzUU8v91cFCbykxQzcqRo7Zc2Jp3HxtwTZmYyt';

const enc = (s) => new TextEncoder().encode(s);
const K = (b58) => Key.from(b58);

/* Everything derived from the program id and the leg mints (which the site's
 * addresses.json carries; the gas service gets them through env or the file). */
export function derive(programId, legMints) {
  const P = K(programId);
  const vault = findProgramAddress([enc('vault')], P);
  const unitMint = findProgramAddress([enc('unit_mint')], P);
  const t22 = K(PROGRAMS.token2022);
  return {
    programId: P, vault, unitMint, legMints: legMints.map(K),
    vaultLegs: legMints.map((m) => K(ataOf(vault, m, t22))),
    feeAccount: K(ataOf(K(FEE_OWNER), unitMint.toBase58(), t22)),
    pool: findProgramAddress([enc('pool')], P), escrow: findProgramAddress([enc('escrow')], P),
  };
}

export const orderPda = (A, owner, id) => findProgramAddress([enc('order'), owner.toBytes(), u64le(id)], A.programId);
export function orderAccounts(A, owner, id, stableMint) {
  const order = orderPda(A, owner, id);
  return {
    order,
    stable: K(ataOf(order, stableMint.toBase58(), K(PROGRAMS.token))),
    legs: A.legMints.map((m) => K(ataOf(order, m.toBase58(), K(PROGRAMS.token2022)))),
  };
}

/* Anchor layout: disc 8 | kind u8 | bump u8 | filled u8 | owner 32 | payer 32 | stable_mint 32 |
 * id u64 | amount_in u64 | max_fee u64 | min_units u64 | min_out 4×u64 | budget 4×u64 | created i64 | expires i64 */
export function decodeOrder(d) {
  if (d.length !== ORDER_SIZE) throw new Error('not an order account');
  const dv = new DataView(d.buffer, d.byteOffset, d.byteLength);
  let o = 8;
  const kind = d[o], bump = d[o + 1], filled = d[o + 2]; o += 3;
  const owner = new Key(d.slice(o, o + 32)); const payer = new Key(d.slice(o + 32, o + 64)); const stableMint = new Key(d.slice(o + 64, o + 96)); o += 96;
  const q = () => { const v = dv.getBigUint64(o, true); o += 8; return v; };
  const id = q(), amountIn = q(), maxFee = q(), minUnits = q();
  const minOut = [q(), q(), q(), q()];
  const budget = [q(), q(), q(), q()];
  const createdAt = Number(dv.getBigInt64(o, true)); o += 8;
  const expiresAt = Number(dv.getBigInt64(o, true));
  return { kind, bump, filled, owner, payer, stableMint, id, amountIn, maxFee, minUnits, minOut, budget, createdAt, expiresAt };
}

/* ------------------------------------------------------------ instructions */
const budgetIxs = (cu, price) => [
  { programId: K(PROGRAMS.computeBudget), keys: [], data: cat(Uint8Array.from([2]), u32le(cu)) },
  { programId: K(PROGRAMS.computeBudget), keys: [], data: cat(Uint8Array.from([3]), u64le(price)) },
];

export function fillLegIx(A, o, oa, leg, swapProgram, swapAccounts, swapData) {
  // swapAccounts: [{ pubkey: Key, isSigner, isWritable }] as the swap program's API gave
  // them. The order PDA cannot sign at transaction level; the program adds its
  // signature inside the CPI, so its flag is cleared here.
  const remaining = swapAccounts.map((k) => meta(k.pubkey, k.isSigner && k.pubkey.toBase58() !== oa.order.toBase58(), k.isWritable));
  const keys = [
    meta(A.gas, true, false), meta(oa.order, false, true),
    meta(o.stableMint, false, false), meta(oa.stable, false, true),
    ...A.legMints.map((m) => meta(m, false, false)),
    ...oa.legs.map((l) => meta(l, false, true)),
    meta(swapProgram, false, false), meta(K(PROGRAMS.token), false, false), meta(K(PROGRAMS.token2022), false, false),
    ...remaining,
  ];
  const data = cat(DISC.fill_leg, Uint8Array.from([leg]), u32le(swapData.length), swapData);
  return { programId: A.programId, keys, data };
}

export function settleInvestIx(A, o, oa, fee, feeTo) {
  const owner = o.owner;
  const keys = [
    meta(A.gas, true, true), meta(oa.order, false, true), meta(owner, false, false), meta(o.payer, false, true),
    meta(A.vault, false, true), meta(A.unitMint, false, true),
    meta(K(ataOf(owner, A.unitMint.toBase58(), K(PROGRAMS.token2022))), false, true), meta(A.feeAccount, false, true),
    meta(o.stableMint, false, false), meta(oa.stable, false, true),
    meta(K(ataOf(owner, o.stableMint.toBase58())), false, true), meta(feeTo, false, true),
    ...A.legMints.map((m) => meta(m, false, false)), ...A.vaultLegs.map((v) => meta(v, false, true)), ...oa.legs.map((l) => meta(l, false, true)),
    meta(K(PROGRAMS.token), false, false), meta(K(PROGRAMS.token2022), false, false), meta(K(PROGRAMS.ata), false, false), meta(K(PROGRAMS.system), false, false),
  ];
  return { programId: A.programId, keys, data: cat(DISC.settle_invest, u64le(fee)) };
}

export function settleRedeemIx(A, o, oa, fee, feeTo) {
  const owner = o.owner;
  const keys = [
    meta(A.gas, true, true), meta(oa.order, false, true), meta(owner, false, false), meta(o.payer, false, true),
    meta(A.vault, false, false),
    meta(o.stableMint, false, false), meta(oa.stable, false, true),
    meta(K(ataOf(owner, o.stableMint.toBase58())), false, true), meta(feeTo, false, true),
    ...A.legMints.map((m) => meta(m, false, false)), ...A.vaultLegs.map((v) => meta(v, false, true)), ...oa.legs.map((l) => meta(l, false, true)),
    meta(K(PROGRAMS.token), false, false), meta(K(PROGRAMS.token2022), false, false), meta(K(PROGRAMS.ata), false, false), meta(K(PROGRAMS.system), false, false),
  ];
  return { programId: A.programId, keys, data: cat(DISC.settle_redeem, u64le(fee)) };
}

export function cancelOrderIx(A, o, oa, canceller) {
  const owner = o.owner;
  const keys = [
    meta(canceller, true, true), meta(oa.order, false, true), meta(owner, false, false), meta(o.payer, false, true),
    meta(o.stableMint, false, false), meta(oa.stable, false, true), meta(K(ataOf(owner, o.stableMint.toBase58())), false, true),
    ...A.legMints.map((m) => meta(m, false, false)), ...oa.legs.map((l) => meta(l, false, true)),
    ...A.legMints.map((m) => meta(K(ataOf(owner, m.toBase58(), K(PROGRAMS.token2022))), false, true)),
    meta(K(PROGRAMS.token), false, false), meta(K(PROGRAMS.token2022), false, false), meta(K(PROGRAMS.ata), false, false), meta(K(PROGRAMS.system), false, false),
  ];
  return { programId: A.programId, keys, data: DISC.cancel_order };
}

/* ------------------------------------------------------------------ swaps */
/* A swap for the ESCROW: Jupiter's swap instruction with the order PDA as the
 * user and the escrow's own accounts as source and destination. Direct routes
 * only (CPI depth and transaction size), no SOL wrapping, no setup/cleanup —
 * the escrow's accounts already exist. */
export async function jupiterSwapForOrder({ jupiter, fetchImpl, inputMint, outputMint, amount, order, outAccount, slippageBps = 100 }) {
  const q = await (await fetchImpl(`${jupiter}/swap/v1/quote?inputMint=${inputMint}&outputMint=${outputMint}&amount=${amount}&slippageBps=${slippageBps}&onlyDirectRoutes=true&maxAccounts=24`)).json();
  if (!q.routePlan) throw new Error(`no route ${inputMint.slice(0, 4)}→${outputMint.slice(0, 4)}: ${q.error || 'unknown'}`);
  const r = await fetchImpl(`${jupiter}/swap/v1/swap-instructions`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ quoteResponse: q, userPublicKey: order.toBase58(), wrapAndUnwrapSol: false, useSharedAccounts: false,
      destinationTokenAccount: outAccount.toBase58(), skipUserAccountsRpcCalls: true, dynamicComputeUnitLimit: false }),
  });
  const s = await r.json();
  if (!s.swapInstruction) throw new Error(`swap-instructions: ${s.error || 'no swap instruction'}`);
  const ix = s.swapInstruction;
  return {
    program: K(ix.programId),
    accounts: ix.accounts.map((a) => meta(K(a.pubkey), a.isSigner, a.isWritable)),
    data: Uint8Array.from(Buffer.from(ix.data, 'base64')),
    alts: s.addressLookupTableAddresses || [],
    cu: s.computeUnitLimit || 300000,
    expectedOut: BigInt(q.outAmount), minOut: BigInt(q.otherAmountThreshold),
  };
}

/* LOCAL TESTS ONLY (SWAP_MODE=mock): the mock swap program at fixed prices. */
export function mockSwapForOrder({ prices, inputMint, outputMint, amount, order, inAccount, outAccount, legIndex, kind }) {
  const MOCK = K(PROGRAMS.mockswap);
  const pool = findProgramAddress([enc('pool')], MOCK);
  const isStable = (m) => !!STABLES[m];
  const prog = (m) => K(isStable(m) ? PROGRAMS.token : PROGRAMS.token2022);
  const poolAcc = (m) => K(ataOf(pool, m, prog(m)));
  const p = prices[legIndex];
  const out = kind === KIND_INVEST ? BigInt(Math.floor(Number(amount) / 1e6 / p * 1e8)) : BigInt(Math.floor(Number(amount) / 1e8 * p * 1e6));
  const accounts = [
    meta(order, true, false), meta(inAccount, false, true), meta(outAccount, false, true),
    meta(poolAcc(inputMint), false, true), meta(poolAcc(outputMint), false, true), meta(pool, false, false),
    meta(K(inputMint), false, false), meta(K(outputMint), false, false), meta(prog(inputMint), false, false), meta(prog(outputMint), false, false),
  ];
  return { program: MOCK, accounts, data: cat(anchorDisc('swap'), u64le(amount), u64le(out)), alts: [], cu: 120000, expectedOut: out, minOut: out };
}

/* -------------------------------------------------------------- the filler */
export function makeFiller({ A, gas, rpc, jupiter = 'https://lite-api.jup.ag', fetchImpl = fetch, swapMode = 'jupiter', mockPrices = null, cuPrice = 20000 }) {
  A = { ...A, gas: gas.publicKey };
  const tableCache = new Map();
  async function tables(addrs) {
    const need = addrs.filter((a) => !tableCache.has(a));
    if (need.length) {
      const got = await rpc('getMultipleAccounts', [need, { encoding: 'base64' }]);
      got.value.forEach((v, i) => { if (v) tableCache.set(need[i], lookupTableAddresses(Uint8Array.from(Buffer.from(v.data[0], 'base64')))); });
    }
    return addrs.filter((a) => tableCache.has(a)).map((a) => ({ key: K(a), addresses: tableCache.get(a) }));
  }
  async function send(instructions, alts = []) {
    const blockhash = (await rpc('getLatestBlockhash', [{ commitment: 'confirmed' }])).value.blockhash;
    const compiled = compileV0({ payer: A.gas, recentBlockhash: blockhash, instructions, tables: await tables(alts) });
    const bytes = serializeSigned(compiled, [gas]);
    if (bytes.length > 1232) throw new Error(`transaction too large: ${bytes.length} bytes`);
    const sig = await rpc('sendTransaction', [Buffer.from(bytes).toString('base64'), { encoding: 'base64', preflightCommitment: 'confirmed', maxRetries: 3 }]);
    for (let i = 0; i < 30; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      const st = (await rpc('getSignatureStatuses', [[sig]])).value[0];
      if (st && (st.confirmationStatus === 'confirmed' || st.confirmationStatus === 'finalized')) {
        if (st.err) throw new Error(`transaction failed on chain: ${JSON.stringify(st.err)}`);
        return sig;
      }
    }
    throw new Error('not confirmed within 30 s');
  }
  async function readOrder(orderB58) {
    const r = await rpc('getAccountInfo', [orderB58, { encoding: 'base64', commitment: 'confirmed' }]);
    if (!r.value) return null;
    return decodeOrder(Uint8Array.from(Buffer.from(r.value.data[0], 'base64')));
  }
  const now = () => Math.floor(Date.now() / 1000);

  /* One fill: the next unfilled leg. Returns the signature. */
  async function fillOne(orderB58, o, oa) {
    const leg = [0, 1, 2, 3].find((i) => !(o.filled & (1 << i)));
    const stable = o.stableMint.toBase58();
    const legMint = A.legMints[leg].toBase58();
    const invest = o.kind === KIND_INVEST;
    const [inputMint, outputMint, inAcc, outAcc] = invest ? [stable, legMint, oa.stable, oa.legs[leg]] : [legMint, stable, oa.legs[leg], oa.stable];
    const amount = o.budget[leg];
    const swap = swapMode === 'mock'
      ? mockSwapForOrder({ prices: mockPrices, inputMint, outputMint, amount, order: oa.order, inAccount: inAcc, outAccount: outAcc, legIndex: leg, kind: o.kind })
      : await jupiterSwapForOrder({ jupiter, fetchImpl, inputMint, outputMint, amount, order: oa.order, outAccount: outAcc });
    if (swap.minOut < o.minOut[leg]) throw new Error(`leg ${leg}: route delivers ${swap.minOut} < the order's minimum ${o.minOut[leg]} (prices moved)`);
    const ix = fillLegIx(A, o, oa, leg, swap.program, swap.accounts, swap.data);
    const cu = Math.min(1_400_000, swap.cu + 120_000);
    return { leg, signature: await send([...budgetIxs(cu, cuPrice), ix], swap.alts) };
  }

  /* Advance an order as far as it will go in one call. Returns its state. */
  async function step(orderB58, { feeTo, fee, maxFills = 4 } = {}) {
    let o = await readOrder(orderB58);
    if (!o) return { status: 'closed' };
    const oa = orderAccounts(A, o.owner, o.id, o.stableMint);
    const events = [];
    if (now() >= o.expiresAt) {
      events.push({ cancel: await send([...budgetIxs(400_000, cuPrice), cancelOrderIx(A, o, oa, A.gas)]) });
      return { status: 'cancelled', events };
    }
    let fills = 0;
    while (o.filled !== ALL_LEGS && fills < maxFills) {
      try {
        events.push(await fillOne(orderB58, o, oa));
      } catch (e) {
        events.push({ error: String(e.message || e) });
        return { status: 'filling', filled: o.filled, events, retry: true };
      }
      fills++;
      o = await readOrder(orderB58);
      if (!o) return { status: 'closed', events };
    }
    if (o.filled === ALL_LEGS) {
      const f = fee === undefined ? o.maxFee : (BigInt(fee) < o.maxFee ? BigInt(fee) : o.maxFee);
      const ix = o.kind === KIND_INVEST ? settleInvestIx(A, o, oa, f, feeTo) : settleRedeemIx(A, o, oa, f, feeTo);
      events.push({ settle: await send([...budgetIxs(400_000, cuPrice), ix]) });
      return { status: 'settled', events };
    }
    return { status: 'filling', filled: o.filled, events };
  }

  /* Orders this key funded: expired → cancel; complete → settle; else leave. */
  async function sweep({ feeTo }) {
    const accs = await rpc('getProgramAccounts', [A.programId.toBase58(), { encoding: 'base64', commitment: 'confirmed',
      filters: [{ dataSize: ORDER_SIZE }, { memcmp: { offset: 8 + 3 + 32, bytes: A.gas.toBase58() } }] }]);
    const out = [];
    for (const { pubkey, account } of accs) {
      const o = decodeOrder(Uint8Array.from(Buffer.from(account.data[0], 'base64')));
      if (now() >= o.expiresAt || o.filled === ALL_LEGS) {
        try { out.push({ order: pubkey, ...(await step(pubkey, { feeTo })) }); } catch (e) { out.push({ order: pubkey, error: String(e.message || e) }); }
      } else {
        out.push({ order: pubkey, status: 'filling', filled: o.filled, expiresAt: o.expiresAt });
      }
    }
    return out;
  }

  async function openCount() {
    const accs = await rpc('getProgramAccounts', [A.programId.toBase58(), { encoding: 'base64', dataSlice: { offset: 0, length: 0 },
      filters: [{ dataSize: ORDER_SIZE }, { memcmp: { offset: 8 + 3 + 32, bytes: A.gas.toBase58() } }] }]);
    return accs.length;
  }

  return { step, sweep, openCount, readOrder, send, A };
}

/* How many wallets hold PAIF (for the first-50 subsidy): token accounts of the
 * unit mint with a balance, less the program's own (dead, pool, escrow, fee). */
export async function holderCount(rpc, A) {
  const accs = await rpc('getProgramAccounts', [PROGRAMS.token2022, { encoding: 'base64', dataSlice: { offset: 32, length: 40 },
    filters: [{ memcmp: { offset: 0, bytes: A.unitMint.toBase58() } }] }]);
  const own = new Set([A.pool.toBase58(), A.escrow.toBase58(), A.feeAccount.toBase58()]);
  const dead = findProgramAddress([enc('dead')], A.programId);
  own.add(ataOf(dead, A.unitMint.toBase58(), K(PROGRAMS.token2022)));
  let n = 0;
  for (const { pubkey, account } of accs) {
    if (own.has(pubkey)) continue;
    const d = Uint8Array.from(Buffer.from(account.data[0], 'base64'));
    const amount = new DataView(d.buffer, d.byteOffset).getBigUint64(32, true); // slice starts at owner(32); amount follows
    if (amount > 0n) n++;
  }
  return n;
}
