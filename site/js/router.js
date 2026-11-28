/* Permafrost router — stables in, stables out (decided 24–25 Nov 2026).
 *
 *   invest:  USDC/USDT → four Jupiter swaps → deposit     (one Jito bundle)
 *   redeem:  redeem → four Jupiter swaps → USDC/USDT      (one Jito bundle)
 *
 * The user only signs. The gas service (site/api/relay.js) is the fee payer and
 * pays any new token accounts, is reimbursed inside the same transactions in the
 * user's stablecoin, checks every instruction, co-signs, and submits the bundle,
 * so either all five transactions land or none do.
 *
 * Runs in the browser (window.solanaWeb3 from the vendored web3.js) and in Node
 * (tests pass `web3` from @solana/web3.js). Nothing here holds a key.
 */

export const STABLES = {
  USDC: { mint: 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', decimals: 6 },
  USDT: { mint: 'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB', decimals: 6 },
};
export const PROGRAMS = {
  token: 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
  token2022: 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb',
  ata: 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL',
  system: '11111111111111111111111111111111',
  computeBudget: 'ComputeBudget111111111111111111111111111111',
  jupiter: 'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4',
};
const SOL_MINT = 'So11111111111111111111111111111111111111112';
export const DISC = {
  deposit: [242, 35, 198, 137, 82, 225, 242, 182],
  redeem: [184, 12, 86, 149, 70, 196, 97, 225],
  deposit_locked: [88, 91, 135, 52, 79, 190, 164, 141],
  claim_locked: [34, 206, 181, 23, 11, 207, 147, 90],
  exit_locked: [207, 93, 52, 157, 105, 252, 90, 187],
  // v4 orders (28 Nov)
  order_invest: [185, 129, 130, 118, 24, 64, 154, 221],
  order_redeem: [223, 244, 191, 69, 48, 105, 196, 143],
  cancel_order: [95, 129, 237, 240, 8, 49, 223, 132],
};
/* An order must live 1–60 minutes on chain; the site gives fills 10 minutes. */
export const ORDER_LIFE_SECONDS = 600;
/* Account sizes the router creates. Their rent is read from the cluster at build
 * time (getMinimumBalanceForRentExemption), never hard-coded: on 26 Nov the live
 * rate was ~27% below the long-standing figure (179 B = 1,559,560 lamports, not
 * 2,136,720). The position account (81 B) is the one the program makes the
 * depositor pay for, so the gas service hands the user exactly that much SOL in
 * the same transaction; it comes back to the user at claim or exit. */
export const SIZES = { xstockAccount: 179, paifAccount: 170, stableAccount: 165, position: 81 };

/* Pricing for the gas reimbursement — shared with the gas service, which
 * recomputes it and refuses a bundle that under-pays. */
export const GAS = {
  lamportsPerSignature: 5000,
  microLamportsPerCu: 10000,       // priority fee; inclusion comes from the Jito tip
  tipLamports: 20000,               // Jito tip, once per bundle (≈ $0.0014 at $69)
  rentTokenAccount2022: 1560000,    // fallback only; live rent is fetched (179 B = 1,559,560 on 26 Nov)
  rentTokenAccount: 1490000,        // fallback only (165 B = 1,488,440 on 26 Nov)
  margin: 1.2,                      // on top of cost, covers SOL price drift
  minimumUsd: 0.01,
};
export const SLIPPAGE_BPS = 100;    // per swap; Jupiter enforces it on chain
export const MAX_ACCOUNTS = 38;     // keep each swap transaction under 1,232 bytes

const enc = new TextEncoder();
const u64le = (v) => { const b = new Uint8Array(8); new DataView(b.buffer).setBigUint64(0, BigInt(v), true); return b; };
const cat = (...a) => { const o = new Uint8Array(a.reduce((n, x) => n + x.length, 0)); let i = 0; for (const x of a) { o.set(x, i); i += x.length; } return o; };
const b64 = (u8) => (typeof Buffer !== 'undefined' ? Buffer.from(u8).toString('base64') : btoa(String.fromCharCode(...u8)));
const fromB64 = (s) => (typeof Buffer !== 'undefined' ? new Uint8Array(Buffer.from(s, 'base64')) : Uint8Array.from(atob(s), (c) => c.charCodeAt(0)));

export function makeRouter({ web3, rpc, addresses, relayer, jupiter = 'https://lite-api.jup.ag', fetchImpl = fetch, fundOverride = null }) {
  const W = web3;
  const PK = (s) => new W.PublicKey(s);
  const P = Object.fromEntries(Object.entries(PROGRAMS).map(([k, v]) => [k, PK(v)]));
  const A = {
    program: PK(addresses.programId),
    vault: PK(addresses.vault),
    unitMint: PK(addresses.unitMint),
    feeAccount: PK(addresses.feeAccount),
    vaultLegs: addresses.vaultLegs.map(PK),
    legMints: (addresses.legMints || [
      'Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh', 'XsCPL9dNWBMvFtTmwcCA5v3xWPSMEBCszbQdiLLq6aN',
      'XspzcW1PRtgf6Wj92HCiZdjzKCyFekVD8P5Ueh3dRMX', 'Xsa62P5mvPszXL1krVUnU5ar38bBSVcWAB6fmPCo5Zu',
    ]).map(PK),
  };
  const RELAYER = PK(relayer);
  const ata = (owner, mint, prog) => W.PublicKey.findProgramAddressSync([owner.toBuffer(), prog.toBuffer(), mint.toBuffer()], P.ata)[0];
  const meta = (pubkey, isSigner, isWritable) => ({ pubkey, isSigner, isWritable });
  const ix = (programId, keys, data) => new W.TransactionInstruction({ programId, keys, data: typeof Buffer !== 'undefined' ? Buffer.from(data) : data });

  async function call(method, params) {
    // Retries rate limits with backoff: a throttled read must never become a wrong number.
    let last;
    for (let attempt = 0; attempt < 5; attempt++) {
      if (attempt) await new Promise((ok) => setTimeout(ok, 500 * 2 ** (attempt - 1)));
      const r = await fetchImpl(rpc, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }) });
      if (r.status === 429 || r.status >= 500) { last = new Error(`${method}: http ${r.status}`); continue; }
      const j = await r.json();
      if (j.error && /rate limit/i.test(j.error.message || '')) { last = new Error(`${method}: ${j.error.message}`); continue; }
      if (j.error) throw new Error(`${method}: ${j.error.message}`);
      return j.result;
    }
    throw last;
  }
  async function exists(keys) {
    // 'confirmed', not the default 'finalized' (~13 s behind): step 2 must see what step 1 just did.
    const r = await call('getMultipleAccounts', [keys.map((k) => k.toBase58()), { encoding: 'base64', dataSlice: { offset: 0, length: 0 }, commitment: 'confirmed' }]);
    return r.value.map((v) => v !== null);
  }
  async function readRaw(keys) {
    const r = await call('getMultipleAccounts', [keys.map((k) => k.toBase58()), { encoding: 'base64', commitment: 'confirmed' }]);
    return r.value.map((v) => (v ? fromB64(v.data[0]) : null));
  }
  const tokenAmount = (d) => (d ? new DataView(d.buffer, d.byteOffset).getBigUint64(64, true) : 0n);

  /* ---------------------------------------------------------- the fund */
  async function fund() {
    if (fundOverride) return fundOverride; // tests only: a vault that isn't on this cluster
    const [vault, mint, ...legs] = await readRaw([A.vault, A.unitMint, ...A.vaultLegs]);
    if (!vault || !mint) throw new Error('vault not initialised');
    const dv = new DataView(vault.buffer, vault.byteOffset);
    return {
      holdings: legs.map(tokenAmount),
      supply: new DataView(mint.buffer, mint.byteOffset).getBigUint64(36, true),
      exitBp: BigInt(dv.getUint16(109, true)),
      bonus: {
        on: vault[19] === 1 && vault[20] === 1,
        bp: BigInt(dv.getUint16(21, true)),
        lockDays: dv.getUint16(23, true),
        max: dv.getBigUint64(25, true),
        nextPosition: dv.getBigUint64(33, true),
      },
    };
  }

  async function prices(mints) {
    const r = await fetchImpl(`${jupiter}/price/v3?ids=${mints.join(',')}`);
    const j = await r.json();
    return mints.map((m) => {
      const p = Number(j?.[m]?.usdPrice);
      if (!(p > 0)) throw new Error(`no price for ${m}`);
      return p;
    });
  }

  async function quote(inputMint, outputMint, amount) {
    const u = `${jupiter}/swap/v1/quote?inputMint=${inputMint}&outputMint=${outputMint}&amount=${amount}` +
      `&slippageBps=${SLIPPAGE_BPS}&maxAccounts=${MAX_ACCOUNTS}`;
    const q = await (await fetchImpl(u)).json();
    if (!q.outAmount) throw new Error(`no route ${inputMint.slice(0, 4)}→${outputMint.slice(0, 4)}: ${q.error || q.errorCode || ''}`);
    return q;
  }

  async function swapInstructions(q, user) {
    const r = await fetchImpl(`${jupiter}/swap/v1/swap-instructions`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ quoteResponse: q, userPublicKey: user.toBase58(), wrapAndUnwrapSol: false }),
    });
    const s = await r.json();
    if (!s.swapInstruction) throw new Error(`swap-instructions: ${s.error || 'no swap instruction'}`);
    const toIx = (i) => ix(PK(i.programId), i.accounts.map((a) => meta(PK(a.pubkey), a.isSigner, a.isWritable)), fromB64(i.data));
    return { swap: toIx(s.swapInstruction), alts: s.addressLookupTableAddresses || [], cu: s.computeUnitLimit || 400000 };
  }

  async function lookupTables(addrs) {
    if (!addrs.length) return [];
    const raw = await readRaw(addrs.map(PK));
    return raw.map((d, i) => new W.AddressLookupTableAccount({ key: PK(addrs[i]), state: decodeLookupTable(d) }));
  }

    /* Address lookup table account: u32 type, u64 deactivation slot, u64 last
   * extended slot, u8 start index, Option<Pubkey> authority (1 + 32), 2 bytes
   * padding — 56 bytes — then the addresses, 32 bytes each. Decoded by hand:
   * web3.js's AddressLookupTableAccount.deserialize calls Node's Buffer and throws
   * in the browser (found 26 Nov by tools/test_router_browser.mjs). */
  function decodeLookupTable(d) {
    if (d.length < 56 || (d.length - 56) % 32) throw new Error('not an address lookup table');
    const dv = new DataView(d.buffer, d.byteOffset, d.byteLength);
    if (dv.getUint32(0, true) !== 1) throw new Error('lookup table not initialized');
    const addresses = [];
    for (let o = 56; o < d.length; o += 32) addresses.push(new W.PublicKey(d.slice(o, o + 32)));
    return {
      deactivationSlot: dv.getBigUint64(4, true),
      lastExtendedSlot: Number(dv.getBigUint64(12, true)),
      lastExtendedSlotStartIndex: d[20],
      authority: d[21] === 1 ? new W.PublicKey(d.slice(22, 54)) : undefined,
      addresses,
    };
  }

  /* An ATA the gas service pays for — replaces Jupiter's user-paid setup step. */
  const createAta = (owner, mint, prog) => ix(P.ata, [
    meta(RELAYER, true, true), meta(ata(owner, mint, prog), false, true), meta(owner, false, false),
    meta(mint, false, false), meta(P.system, false, false), meta(prog, false, false),
  ], Uint8Array.from([1]));

  /* The reimbursement: user's stablecoin → the gas service's account, same mint. */
  const reimburse = (user, stable, amount) => {
    const m = PK(stable.mint);
    return ix(P.token, [
      meta(ata(user, m, P.token), false, true), meta(m, false, false),
      meta(ata(RELAYER, m, P.token), false, true), meta(user, true, false),
    ], cat(Uint8Array.from([12]), u64le(amount), Uint8Array.from([stable.decimals])));
  };

  /* Compute-budget and SOL-transfer instructions are encoded here, not with
   * web3.js's ComputeBudgetProgram / SystemProgram helpers: in the browser build
   * (web3-1.98.0.iife) those call Node's Buffer for their u64 fields and throw
   * "Buffer is not defined" (found 26 Nov by tools/test_app_lock.mjs). */
  const u32le = (v) => { const b = new Uint8Array(4); new DataView(b.buffer).setUint32(0, v, true); return b; };
  const budget = (cu) => [
    ix(P.computeBudget, [], cat(Uint8Array.from([2]), u32le(Math.min(1_400_000, Math.ceil(cu * 1.2) + 30_000)))),
    ix(P.computeBudget, [], cat(Uint8Array.from([3]), u64le(GAS.microLamportsPerCu))),
  ];
  const solTransfer = (from, to, lamports) =>
    ix(P.system, [meta(from, true, true), meta(to, false, true)], cat(u32le(2), u64le(lamports)));
  const tip = (account) => solTransfer(RELAYER, PK(account), GAS.tipLamports);

  function userMetas(user, userUnit) {
    return [
      meta(user, true, false), meta(A.vault, false, true), meta(A.unitMint, false, true), meta(userUnit, false, true),
      ...A.legMints.map((m) => meta(m, false, false)), ...A.vaultLegs.map((v) => meta(v, false, true)),
      ...A.legMints.map((m) => meta(ata(user, m, P.token2022), false, true)),
      meta(P.token2022, false, false), meta(A.feeAccount, false, true),
    ];
  }

  let rent = null;
  async function loadRent() {
    if (rent) return rent;
    const [x, p, st, pos] = await Promise.all([SIZES.xstockAccount, SIZES.paifAccount, SIZES.stableAccount, SIZES.position]
      .map((n) => call('getMinimumBalanceForRentExemption', [n])));
    rent = { x2022: Math.max(x, p), classic: st, position: pos };
    GAS.rentTokenAccount2022 = rent.x2022;
    GAS.rentTokenAccount = rent.classic;
    return rent;
  }

  /* Gas cost of one transaction for the gas service, in lamports. */
  const txCost = (cuLimit, newAtas2022, newAtasClassic, withTip) =>
    2 * GAS.lamportsPerSignature + Math.ceil((cuLimit * GAS.microLamportsPerCu) / 1e6) +
    newAtas2022 * GAS.rentTokenAccount2022 + newAtasClassic * GAS.rentTokenAccount + (withTip ? GAS.tipLamports : 0);

  const toStable = (lamports, solUsd, stable) => {
    const usd = Math.max(GAS.minimumUsd, (lamports / 1e9) * solUsd * GAS.margin);
    return BigInt(Math.ceil(usd * 10 ** stable.decimals));
  };

  async function compile(user, ixs, alts, blockhash) {
    const msg = new W.TransactionMessage({ payerKey: RELAYER, recentBlockhash: blockhash, instructions: ixs }).compileToV0Message(alts);
    return new W.VersionedTransaction(msg);
  }

  const pool = W.PublicKey.findProgramAddressSync([enc.encode('pool')], PK(addresses.programId))[0];
  const escrow = W.PublicKey.findProgramAddressSync([enc.encode('escrow')], PK(addresses.programId))[0];
  const positionPda = (id) => W.PublicKey.findProgramAddressSync([enc.encode('position'), u64le(id)], PK(addresses.programId))[0];

  /* ------------------------------------------------------------ invest */
  /* Two signing steps (28 Nov). Phantom simulates each transaction on its own and
   * warns "this dApp could be malicious" when one fails. The deposit can't simulate
   * before the swaps have delivered the stocks, so it is signed separately:
   *   1. buildInvest  → the four swaps (one Jito bundle, each self-contained);
   *   2. buildDeposit → the deposit of exactly what the swaps delivered.
   * Returns { txs, summary }. `amount` is in the stablecoin's base units. The
   * summary's gas includes the deposit's, which stays in the user's wallet until
   * step 2. lock: true → step 2 is a locked deposit (setup-cost rebate). */
  async function buildInvest({ user: userStr, stable: stableName, amount, tipAccount, lock = false }) {
    const user = PK(userStr);
    const R0 = await loadRent();
    const stable = STABLES[stableName];
    if (!stable) throw new Error('stable must be USDC or USDT');
    const f = await fund();
    const [solUsd, ...legUsd] = await prices([SOL_MINT, ...A.legMints.map((m) => m.toBase58())]);

    // Which accounts are new (the gas service pays their rent).
    const userLegs = A.legMints.map((m) => ata(user, m, P.token2022));
    const userUnit = ata(user, A.unitMint, P.token2022);
    const [stableOk, ...have] = await exists([ata(RELAYER, PK(stable.mint), P.token), ...userLegs, userUnit]);
    if (!stableOk) throw new Error(`gas service has no ${stableName} account yet`);
    const legNew = have.slice(0, 4).map((x) => !x);
    const unitNew = !have[4];

    // Reserve the gas reimbursement first, then split the rest by vault value.
    const est = [0, 1, 2, 3].map((i) => txCost(400000, (legNew[i] ? 1 : 0) + (lock && i === 0 && unitNew ? 1 : 0), 0, i === 3))
      .concat([txCost(250000, !lock && unitNew ? 1 : 0, 0, true) + (lock ? R0.position : 0)]);
    const gas = est.map((l) => toStable(l, solUsd, stable));
    const gasTotal = gas.reduce((a, b) => a + b, 0n);
    const spend = BigInt(amount) - gasTotal;
    if (spend <= 0n) throw new Error('amount too small to cover gas');
    const values = f.holdings.map((h, i) => (Number(h) / 1e8) * legUsd[i]);
    const total = values.reduce((a, b) => a + b, 0);
    const split = values.map((v) => BigInt(Math.floor(Number(spend) * (v / total))));

    const quotes = await Promise.all(split.map((s, i) => quote(stable.mint, A.legMints[i].toBase58(), s.toString())));
    const parts = await Promise.all(quotes.map((q) => swapInstructions(q, user)));
    const blockhash = (await call('getLatestBlockhash', [{ commitment: 'confirmed' }])).value.blockhash;

    const txs = [];
    for (let i = 0; i < 4; i++) {
      const alts = await lookupTables(parts[i].alts);
      const ixs = [...budget(parts[i].cu), createAta(user, A.legMints[i], P.token2022)];
      // A locked deposit doesn't touch the PAIF account, but claiming will:
      // create it here, where there's room, rather than at claim time.
      if (lock && i === 0) ixs.push(createAta(user, A.unitMint, P.token2022));
      ixs.push(parts[i].swap, reimburse(user, stable, gas[i]));
      if (i === 3) ixs.push(tip(tipAccount));
      txs.push(await compile(user, ixs, alts, blockhash));
    }
    // An estimate for the review screen: what the swaps are guaranteed to deliver
    // (Jupiter's minimum out), deposited. Step 2 deposits what actually arrived.
    const minOut = quotes.map((q) => BigInt(q.otherAmountThreshold));
    const supplyEff = f.supply; // fees are 0 at launch; the program charges any owed fee itself
    let k = null;
    for (let i = 0; i < 4; i++) { const r = (minOut[i] * 10n ** 18n) / f.holdings[i]; if (k === null || r < k) k = r; }
    const unitsMin = (k * supplyEff) / 10n ** 18n;
    const minUnitsOut = (unitsMin * 9900n) / 10000n;
    let bonus = 0n;
    if (lock) {
      const b = f.bonus;
      if (!b || !b.on) throw new Error('locked investing is switched off');
      const [poolRaw] = await readRaw([pool]);
      bonus = [(unitsMin * b.bp) / 10000n, b.max, tokenAmount(poolRaw)].reduce((x, y) => (y < x ? y : x));
    }

    return {
      txs,
      summary: {
        stable: stableName, amount: BigInt(amount), gas: gasTotal, spend, split,
        expectedLegs: quotes.map((q) => BigInt(q.outAmount)), minLegs: minOut,
        minUnitsOut, solUsd, legUsd, lock, bonus,
        lockDays: lock ? f.bonus.lockDays : 0,
      },
    };
  }

  /* Raw balances of the four stock tokens in the user's wallet (0 if no account). */
  async function walletLegs(userStr) {
    const user = PK(userStr);
    return (await readRaw(A.legMints.map((m) => ata(user, m, P.token2022)))).map(tokenAmount);
  }

  /* Step 2 of investing, and the "add my stocks to the fund" button: deposit up to
   * `amounts` (raw, per leg) of the user's stocks. The program takes the largest
   * in-proportion slice; the rest stays in the wallet. Gas is repaid in `stable`. */
  async function buildDeposit({ user: userStr, stable: stableName, amounts, tipAccount, lock = false }) {
    const user = PK(userStr);
    const R0 = await loadRent();
    const stable = STABLES[stableName];
    if (!stable) throw new Error('stable must be USDC or USDT');
    const maxIn = amounts.map((a) => BigInt(a));
    if (maxIn.some((a) => a <= 0n)) throw new Error('every stock must be above zero to deposit');
    const f = await fund();
    const [solUsd] = await prices([SOL_MINT]);
    const userUnit = ata(user, A.unitMint, P.token2022);
    const [stableOk, unitHave] = await exists([ata(RELAYER, PK(stable.mint), P.token), userUnit]);
    if (!stableOk) throw new Error(`gas service has no ${stableName} account yet`);
    let k = null;
    for (let i = 0; i < 4; i++) { const r = (maxIn[i] * 10n ** 18n) / f.holdings[i]; if (k === null || r < k) k = r; }
    const units = (k * f.supply) / 10n ** 18n;
    if (units === 0n) throw new Error('too little to deposit');
    const minUnitsOut = (units * 9900n) / 10000n;
    const blockhash = (await call('getLatestBlockhash', [{ commitment: 'confirmed' }])).value.blockhash;
    let ixs;
    let bonus = 0n;
    let positionId = null;
    if (!lock) {
      const gas = toStable(txCost(250000, unitHave ? 0 : 1, 0, true), solUsd, stable);
      const depositIx = ix(A.program, userMetas(user, userUnit), cat(Uint8Array.from(DISC.deposit), ...maxIn.map(u64le), u64le(minUnitsOut)));
      ixs = [...budget(250000), createAta(user, A.unitMint, P.token2022), depositIx, reimburse(user, stable, gas)];
      ixs.push(tip(tipAccount));
      return { txs: [await compile(user, ixs, [], blockhash)], summary: { units, minUnitsOut, gas, lock } };
    }
    const b = f.bonus;
    if (!b || !b.on) throw new Error('locked investing is switched off');
    const [poolRaw] = await readRaw([pool]);
    bonus = [(units * b.bp) / 10000n, b.max, tokenAmount(poolRaw)].reduce((x, y) => (y < x ? y : x));
    positionId = b.nextPosition;
    const gas = toStable(txCost(250000, 0, 0, true) + R0.position, solUsd, stable);
    const keys = [
      meta(user, true, true), meta(A.vault, false, true), meta(A.unitMint, false, true),
      meta(pool, false, true), meta(escrow, false, true), meta(positionPda(positionId), false, true),
      ...A.legMints.map((m) => meta(m, false, false)), ...A.vaultLegs.map((v) => meta(v, false, true)),
      ...A.legMints.map((m) => meta(ata(user, m, P.token2022), false, true)),
      meta(P.token2022, false, false), meta(P.system, false, false), meta(A.feeAccount, false, true),
    ];
    const data = cat(Uint8Array.from(DISC.deposit_locked), ...maxIn.map(u64le), u64le(minUnitsOut),
      Uint8Array.from([b.lockDays & 0xff, b.lockDays >> 8]), u64le((bonus * 9900n) / 10000n));
    // The position account's rent: the gas service gives it to the user here. No
    // compute-budget instructions: this is the largest transaction the router
    // builds (1,197 bytes), and the default allowance covers deposit_locked (~84k
    // CU). No room to create the PAIF account here too: buildInvest's first swap
    // creates it for a locked investment.
    if (!unitHave) throw new Error('create the PAIF account first (a locked investment does this in its first step)');
    ixs = [solTransfer(RELAYER, user, R0.position), ix(A.program, keys, data), reimburse(user, stable, gas), tip(tipAccount)];
    return { txs: [await compile(user, ixs, [], blockhash)], summary: { units, minUnitsOut, gas, lock, bonus, positionId, lockDays: b.lockDays } };
  }

  /* ------------------------------------------------------------ redeem */
  /* Two signing steps (28 Nov), for the same reason as investing: the sales can't
   * simulate until the redemption has delivered the stocks.
   *   1. buildRedeem → hand back PAIF, receive the four stocks;
   *   2. buildSell   → sell exactly what arrived for USDC/USDT (one Jito bundle).
   * Step 1 is free when it creates no accounts (the gas service accepts a lone
   * redeem, as it does a lone claim or exit); if the user has no stock accounts
   * yet, step 1 creates them and repays their rent in `stable`. */
  async function buildRedeem({ user: userStr, stable: stableName, units, tipAccount }) {
    const user = PK(userStr);
    await loadRent();
    const stable = STABLES[stableName];
    if (!stable) throw new Error('stable must be USDC or USDT');
    const f = await fund();
    const [solUsd] = await prices([SOL_MINT]);
    const userLegs = A.legMints.map((m) => ata(user, m, P.token2022));
    const userUnit = ata(user, A.unitMint, P.token2022);
    const [stableOk, ...legsHave] = await exists([ata(RELAYER, PK(stable.mint), P.token), ...userLegs]);
    if (!stableOk) throw new Error(`gas service has no ${stableName} account yet`);

    // Exactly what redeem pays (the program's arithmetic): fee slice, then pro rata.
    const u = BigInt(units);
    const burned = u - (u * f.exitBp) / 10000n;
    const payout = f.holdings.map((h) => (h * burned) / f.supply);
    const minOut = payout.map((p) => (p * 9950n) / 10000n);
    const legNew = legsHave.map((x) => !x).filter(Boolean).length;
    const ixs = [...budget(120000)];
    if (legNew) A.legMints.forEach((m, i) => { if (!legsHave[i]) ixs.push(createAta(user, m, P.token2022)); });
    ixs.push(ix(A.program, userMetas(user, userUnit), cat(Uint8Array.from(DISC.redeem), u64le(u), ...minOut.map(u64le))));
    const gas = legNew ? toStable(txCost(120000, legNew, 0, true), solUsd, stable) : 0n;
    if (legNew) ixs.push(reimburse(user, stable, gas));
    ixs.push(tip(tipAccount));
    const blockhash = (await call('getLatestBlockhash', [{ commitment: 'confirmed' }])).value.blockhash;
    // An estimate of the sale for the review screen (step 2 re-quotes what arrived).
    let expected = null;
    try {
      const qs = await Promise.all(payout.map((p, i) => (p > 0n ? quote(A.legMints[i].toBase58(), stable.mint, p.toString()) : null)));
      expected = qs.reduce((a, q) => a + (q ? BigInt(q.outAmount) : 0n), 0n);
    } catch { expected = null; }
    const sellGas = [0, 1, 2, 3].map((i) => toStable(txCost(400000, 0, 0, i === 3), solUsd, stable)).reduce((a, b) => a + b, 0n);
    const totalGas = gas + sellGas;
    return { txs: [await compile(user, ixs, [], blockhash)],
      summary: { stable: stableName, units: u, payout, minOut, gas: totalGas, expected, net: expected === null ? null : expected - totalGas, solUsd, free: !legNew } };
  }

  /* Step 2 of withdrawing, and the "sell the stocks in my wallet" button: sell
   * `amounts` (raw, per leg; zeros skipped) for `stable`, each sale repaying its gas. */
  async function buildSell({ user: userStr, stable: stableName, amounts, tipAccount }) {
    const user = PK(userStr);
    await loadRent();
    const stable = STABLES[stableName];
    if (!stable) throw new Error('stable must be USDC or USDT');
    const sellAll = amounts.map((a) => BigInt(a));
    const legs = [0, 1, 2, 3].filter((i) => sellAll[i] > 0n);
    if (!legs.length) throw new Error('nothing to sell');
    const [solUsd] = await prices([SOL_MINT]);
    const userStable = ata(user, PK(stable.mint), P.token);
    const [stableOk, stableHave] = await exists([ata(RELAYER, PK(stable.mint), P.token), userStable]);
    if (!stableOk) throw new Error(`gas service has no ${stableName} account yet`);
    const quotes = await Promise.all(legs.map((i) => quote(A.legMints[i].toBase58(), stable.mint, sellAll[i].toString())));
    const parts = await Promise.all(quotes.map((q) => swapInstructions(q, user)));
    const blockhash = (await call('getLatestBlockhash', [{ commitment: 'confirmed' }])).value.blockhash;
    const txs = [];
    let gasTotal = 0n;
    for (const [n, i] of legs.entries()) {
      const last = n === legs.length - 1;
      const gas = toStable(txCost(Math.min(1_400_000, Math.ceil(parts[n].cu * 1.2) + 30_000), 0, n === 0 && !stableHave ? 1 : 0, last), solUsd, stable);
      // A sale can't repay more than it brings in; the first sale also pays for a
      // new stablecoin account, so it must clear that.
      if (BigInt(quotes[n].otherAmountThreshold) <= gas) throw new Error('too little of a stock to sell after network fees');
      gasTotal += gas;
      const alts = await lookupTables(parts[n].alts);
      const ixs = [...budget(parts[n].cu)];
      if (n === 0) ixs.push(createAta(user, PK(stable.mint), P.token));
      ixs.push(parts[n].swap, reimburse(user, stable, gas));
      if (last) ixs.push(tip(tipAccount));
      txs.push(await compile(user, ixs, alts, blockhash));
    }
    const expected = quotes.reduce((a, q) => a + BigInt(q.outAmount), 0n);
    return { txs, summary: { stable: stableName, sell: sellAll, expected, gas: gasTotal, net: expected - gasTotal, solUsd } };
  }

  /* ------------------------------------------------------- claim / exit */
  /* One transaction; the gas service pays it without reimbursement (one per
   * position, so bounded). claim: at or after maturity, principal + bonus to the
   * user's PAIF. exit: before maturity, principal back, bonus to the pool. */
  async function buildSettle({ user: userStr, positionId, kind, tipAccount }) {
    if (kind !== 'claim' && kind !== 'exit') throw new Error('claim or exit');
    const user = PK(userStr);
    const blockhash = (await call('getLatestBlockhash', [{ commitment: 'confirmed' }])).value.blockhash;
    const keys = [
      meta(user, true, true), meta(A.vault, false, false), meta(A.unitMint, false, false),
      meta(pool, false, true), meta(escrow, false, true), meta(positionPda(positionId), false, true),
      meta(ata(user, A.unitMint, P.token2022), false, true), meta(P.token2022, false, false),
    ];
    const ixs = [...budget(60000), ix(A.program, keys, Uint8Array.from(kind === 'claim' ? DISC.claim_locked : DISC.exit_locked)), tip(tipAccount)];
    return { txs: [await compile(user, ixs, [], blockhash)] };
  }

  /* ============================================================ v4 orders
   * One user signature. The gas service (fee payer AND rent payer, account 1)
   * co-signs, sends, fills through Jupiter and settles. The user signs the whole
   * plan: per leg the most stablecoin to spend and the least stock to accept
   * (from live quotes, 1% slippage), the fewest units overall, the fee cap. */
  const orderPda = (user, id) => W.PublicKey.findProgramAddressSync([enc.encode('order'), user.toBuffer(), u64le(id)], PK(addresses.programId))[0];
  const randomId = () => { const b = new Uint8Array(8); crypto.getRandomValues(b); b[7] &= 0x7f; return new DataView(b.buffer).getBigUint64(0, true); };
  const i64le = (v) => { const b = new Uint8Array(8); new DataView(b.buffer).setBigInt64(0, BigInt(v), true); return b; };

  /* feeStable: the gas service's fee cap in the stablecoin's base units (0 while subsidised). */
  async function buildOrderInvest({ user: userStr, stable: stableName, amount, feeStable = 0n, slippageBps = 100 }) {
    const user = PK(userStr);
    const stable = STABLES[stableName];
    if (!stable) throw new Error('stable must be USDC or USDT');
    const f = await fund();
    const [, ...legUsd] = await prices([SOL_MINT, ...A.legMints.map((m) => m.toBase58())]);
    const total = BigInt(amount);
    const fee = BigInt(feeStable);
    const spend = total - fee;
    if (spend <= 0n) throw new Error('amount too small');
    // Split by the vault's current weights, so the four legs land in proportion
    // (the leftover above the binding leg stays in the vault).
    const values = f.holdings.map((h, i) => (Number(h) / 1e8) * legUsd[i]);
    const sum = values.reduce((a, b) => a + b, 0);
    const budget = values.map((v) => BigInt(Math.floor(Number(spend) * (v / sum))));
    const quotes = await Promise.all(budget.map((b, i) => quote(stable.mint, A.legMints[i].toBase58(), b.toString())));
    const minOut = quotes.map((q) => (BigInt(q.otherAmountThreshold) * BigInt(10000 - slippageBps)) / 10000n);
    // Units the vault's arithmetic would mint for exactly minOut of each leg (the binding leg decides), less 1%.
    let k = null;
    for (let i = 0; i < 4; i++) { const r = (minOut[i] * 10n ** 18n) / f.holdings[i]; if (k === null || r < k) k = r; }
    const unitsMin = (k * f.supply) / 10n ** 18n;
    const minUnits = (unitsMin * 9900n) / 10000n;
    const expected = quotes.map((q) => BigInt(q.outAmount));
    let ke = null;
    for (let i = 0; i < 4; i++) { const r = (expected[i] * 10n ** 18n) / f.holdings[i]; if (ke === null || r < ke) ke = r; }
    const unitsExpected = (ke * f.supply) / 10n ** 18n;
    const id = randomId();
    const expiresAt = Math.floor(Date.now() / 1000) + ORDER_LIFE_SECONDS;
    const order = orderPda(user, id);
    const keys = [
      meta(user, true, false), meta(RELAYER, true, true), meta(order, false, true),
      meta(PK(stable.mint), false, false), meta(ata(user, PK(stable.mint), P.token), false, true), meta(ata(order, PK(stable.mint), P.token), false, true),
      ...A.legMints.map((m) => meta(m, false, false)), ...A.legMints.map((m) => meta(ata(order, m, P.token2022), false, true)),
      meta(P.token, false, false), meta(P.token2022, false, false), meta(P.ata, false, false), meta(P.system, false, false),
    ];
    const data = cat(Uint8Array.from(DISC.order_invest), u64le(id), u64le(total), ...budget.map(u64le), ...minOut.map(u64le), u64le(minUnits), u64le(fee), i64le(expiresAt));
    const blockhash = (await call('getLatestBlockhash', [{ commitment: 'confirmed' }])).value.blockhash;
    const tx = await compile(user, [...budget300(), ix(A.program, keys, data)], [], blockhash);
    return { txs: [tx], order: order.toBase58(), summary: { stable: stableName, amount: total, fee, spend, budget, minOut, minUnits, unitsExpected, expiresAt, id } };
  }

  async function buildOrderRedeem({ user: userStr, stable: stableName, units, feeStable = 0n, slippageBps = 100 }) {
    const user = PK(userStr);
    const stable = STABLES[stableName];
    if (!stable) throw new Error('stable must be USDC or USDT');
    const f = await fund();
    const u = BigInt(units);
    const burned = u - (u * f.exitBp) / 10000n;
    const payout = f.holdings.map((h) => (h * burned) / f.supply);
    const quotes = await Promise.all(payout.map((p, i) => (p > 0n ? quote(A.legMints[i].toBase58(), stable.mint, p.toString()) : null)));
    const minOut = quotes.map((q) => (q ? (BigInt(q.otherAmountThreshold) * BigInt(10000 - slippageBps)) / 10000n : 0n));
    const expected = quotes.reduce((a, q) => a + (q ? BigInt(q.outAmount) : 0n), 0n);
    const fee = BigInt(feeStable);
    const id = randomId();
    const expiresAt = Math.floor(Date.now() / 1000) + ORDER_LIFE_SECONDS;
    const order = orderPda(user, id);
    const keys = [
      meta(user, true, false), meta(RELAYER, true, true), meta(order, false, true),
      meta(A.vault, false, true), meta(A.unitMint, false, true), meta(ata(user, A.unitMint, P.token2022), false, true), meta(A.feeAccount, false, true),
      meta(PK(stable.mint), false, false), meta(ata(order, PK(stable.mint), P.token), false, true),
      ...A.legMints.map((m) => meta(m, false, false)), ...A.vaultLegs.map((v) => meta(v, false, true)),
      ...A.legMints.map((m) => meta(ata(order, m, P.token2022), false, true)),
      meta(P.token, false, false), meta(P.token2022, false, false), meta(P.ata, false, false), meta(P.system, false, false),
    ];
    const data = cat(Uint8Array.from(DISC.order_redeem), u64le(id), u64le(u), ...minOut.map(u64le), u64le(fee), i64le(expiresAt));
    const blockhash = (await call('getLatestBlockhash', [{ commitment: 'confirmed' }])).value.blockhash;
    const tx = await compile(user, [...budget300(), ix(A.program, keys, data)], [], blockhash);
    return { txs: [tx], order: order.toBase58(), summary: { stable: stableName, units: u, payout, minOut, expected, fee, net: expected - fee, expiresAt, id } };
  }

  /* The owner's own cancel (any time): refund of whatever the escrow holds. */
  async function buildCancelOrder({ user: userStr, order: orderStr, stable: stableName }) {
    const user = PK(userStr);
    const order = PK(orderStr);
    const stable = STABLES[stableName];
    const keys = [
      meta(user, true, true), meta(order, false, true), meta(user, false, false), meta(RELAYER, false, true),
      meta(PK(stable.mint), false, false), meta(ata(order, PK(stable.mint), P.token), false, true), meta(ata(user, PK(stable.mint), P.token), false, true),
      ...A.legMints.map((m) => meta(m, false, false)), ...A.legMints.map((m) => meta(ata(order, m, P.token2022), false, true)),
      ...A.legMints.map((m) => meta(ata(user, m, P.token2022), false, true)),
      meta(P.token, false, false), meta(P.token2022, false, false), meta(P.ata, false, false), meta(P.system, false, false),
    ];
    const blockhash = (await call('getLatestBlockhash', [{ commitment: 'confirmed' }])).value.blockhash;
    return { txs: [await compile(user, [...budget300(), ix(A.program, keys, Uint8Array.from(DISC.cancel_order))], [], blockhash)] };
  }
  const budget300 = () => [
    ix(P.computeBudget, [], cat(Uint8Array.from([2]), u32le(300_000))),
    ix(P.computeBudget, [], cat(Uint8Array.from([3]), u64le(GAS.microLamportsPerCu))),
  ];

  return { buildInvest, buildDeposit, buildRedeem, buildSell, buildSettle, buildOrderInvest, buildOrderRedeem, buildCancelOrder, orderPda, walletLegs, fund, prices, ata, A, P, RELAYER, txCost, toStable, b64, positionPda };
}
