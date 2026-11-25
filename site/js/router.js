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
const DISC = {
  deposit: [242, 35, 198, 137, 82, 225, 242, 182],
  redeem: [184, 12, 86, 149, 70, 196, 97, 225],
};

/* Pricing for the gas reimbursement — shared with the gas service, which
 * recomputes it and refuses a bundle that under-pays. */
export const GAS = {
  lamportsPerSignature: 5000,
  microLamportsPerCu: 10000,       // priority fee; inclusion comes from the Jito tip
  tipLamports: 20000,               // Jito tip, once per bundle (≈ $0.0014 at $69)
  rentTokenAccount2022: 2140000,    // measured: xStock account 179 B = 2,136,720; PAIF 170 B = 2,074,080
  rentTokenAccount: 2040000,        // classic SPL token account (USDC / USDT), 165 B
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
    const r = await fetchImpl(rpc, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }) });
    const j = await r.json();
    if (j.error) throw new Error(`${method}: ${j.error.message}`);
    return j.result;
  }
  async function exists(keys) {
    const r = await call('getMultipleAccounts', [keys.map((k) => k.toBase58()), { encoding: 'base64', dataSlice: { offset: 0, length: 0 } }]);
    return r.value.map((v) => v !== null);
  }
  async function readRaw(keys) {
    const r = await call('getMultipleAccounts', [keys.map((k) => k.toBase58()), { encoding: 'base64' }]);
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
    return raw.map((d, i) => new W.AddressLookupTableAccount({ key: PK(addrs[i]), state: W.AddressLookupTableAccount.deserialize(d) }));
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

  const budget = (cu) => [
    W.ComputeBudgetProgram.setComputeUnitLimit({ units: Math.min(1_400_000, Math.ceil(cu * 1.2) + 30_000) }),
    W.ComputeBudgetProgram.setComputeUnitPrice({ microLamports: GAS.microLamportsPerCu }),
  ];
  const tip = (account) => W.SystemProgram.transfer({ fromPubkey: RELAYER, toPubkey: PK(account), lamports: GAS.tipLamports });

  function userMetas(user, userUnit) {
    return [
      meta(user, true, false), meta(A.vault, false, true), meta(A.unitMint, false, true), meta(userUnit, false, true),
      ...A.legMints.map((m) => meta(m, false, false)), ...A.vaultLegs.map((v) => meta(v, false, true)),
      ...A.legMints.map((m) => meta(ata(user, m, P.token2022), false, true)),
      meta(P.token2022, false, false), meta(A.feeAccount, false, true),
    ];
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

  /* ------------------------------------------------------------ invest */
  /* Returns { txs, summary }. `amount` is in the stablecoin's base units. */
  async function buildInvest({ user: userStr, stable: stableName, amount, tipAccount }) {
    const user = PK(userStr);
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
    const est = [0, 1, 2, 3].map((i) => txCost(400000, legNew[i] ? 1 : 0, 0, false))
      .concat([txCost(250000, unitNew ? 1 : 0, 0, true)]);
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
      const ixs = [...budget(parts[i].cu), createAta(user, A.legMints[i], P.token2022), parts[i].swap, reimburse(user, stable, gas[i])];
      txs.push(await compile(user, ixs, alts, blockhash));
    }
    // Deposit what the swaps are guaranteed to deliver (Jupiter's minimum out).
    const minOut = quotes.map((q) => BigInt(q.otherAmountThreshold));
    const supplyEff = f.supply; // fees are 0 at launch; the program charges any owed fee itself
    let k = null;
    for (let i = 0; i < 4; i++) { const r = (minOut[i] * 10n ** 18n) / f.holdings[i]; if (k === null || r < k) k = r; }
    const unitsMin = (k * supplyEff) / 10n ** 18n;
    const minUnitsOut = (unitsMin * 9900n) / 10000n;
    const depositIx = ix(A.program, userMetas(user, userUnit), cat(Uint8Array.from(DISC.deposit), ...minOut.map(u64le), u64le(minUnitsOut)));
    const last = [...budget(250000), createAta(user, A.unitMint, P.token2022), depositIx, reimburse(user, stable, gas[4]), tip(tipAccount)];
    txs.push(await compile(user, last, [], blockhash));

    return {
      txs,
      summary: {
        stable: stableName, amount: BigInt(amount), gas: gasTotal, spend, split,
        expectedLegs: quotes.map((q) => BigInt(q.outAmount)), minLegs: minOut,
        minUnitsOut, solUsd, legUsd,
      },
    };
  }

  /* ------------------------------------------------------------ redeem */
  async function buildRedeem({ user: userStr, stable: stableName, units, tipAccount }) {
    const user = PK(userStr);
    const stable = STABLES[stableName];
    if (!stable) throw new Error('stable must be USDC or USDT');
    const f = await fund();
    const [solUsd] = await prices([SOL_MINT]);
    const userLegs = A.legMints.map((m) => ata(user, m, P.token2022));
    const userUnit = ata(user, A.unitMint, P.token2022);
    const userStable = ata(user, PK(stable.mint), P.token);
    const [stableOk, stableHave, ...legsHave] = await exists([ata(RELAYER, PK(stable.mint), P.token), userStable, ...userLegs]);
    if (!stableOk) throw new Error(`gas service has no ${stableName} account yet`);

    // Exactly what redeem pays (the program's arithmetic): fee slice, then pro rata.
    const u = BigInt(units);
    const burned = u - (u * f.exitBp) / 10000n;
    const payout = f.holdings.map((h) => (h * burned) / f.supply);
    // Sell slightly less than the payout so a small change in the vault between
    // building and landing can't make a swap fail; the dust stays with the user.
    const sell = payout.map((p) => (p * 9990n) / 10000n);
    const minOut = payout.map((p) => (p * 9950n) / 10000n);

    const blockhash = (await call('getLatestBlockhash', [{ commitment: 'confirmed' }])).value.blockhash;
    const quotes = await Promise.all(sell.map((s, i) => quote(A.legMints[i].toBase58(), stable.mint, s.toString())));
    const parts = await Promise.all(quotes.map((q) => swapInstructions(q, user)));

    const legNew = legsHave.map((x) => !x).filter(Boolean).length;
    const gas0 = toStable(txCost(250000, legNew, 0, false), solUsd, stable);
    const gasSwaps = [0, 1, 2, 3].map((i) => toStable(txCost(400000, 0, i === 0 && !stableHave ? 1 : 0, i === 3), solUsd, stable));
    // The redeem transaction has no stablecoin yet; its gas is added to the first swap's.
    gasSwaps[0] += gas0;

    const redeemIx = ix(A.program, userMetas(user, userUnit), cat(Uint8Array.from(DISC.redeem), u64le(u), ...minOut.map(u64le)));
    const first = [...budget(250000), ...A.legMints.map((m) => createAta(user, m, P.token2022)), redeemIx];
    const txs = [await compile(user, first, [], blockhash)];
    for (let i = 0; i < 4; i++) {
      const alts = await lookupTables(parts[i].alts);
      const ixs = [...budget(parts[i].cu)];
      if (i === 0) ixs.push(createAta(user, PK(stable.mint), P.token));
      ixs.push(parts[i].swap, reimburse(user, stable, gasSwaps[i]));
      if (i === 3) ixs.push(tip(tipAccount));
      txs.push(await compile(user, ixs, alts, blockhash));
    }
    const expected = quotes.reduce((a, q) => a + BigInt(q.outAmount), 0n);
    const gas = gasSwaps.reduce((a, b) => a + b, 0n);
    return { txs, summary: { stable: stableName, units: u, payout, sell, expected, gas, net: expected - gas, solUsd } };
  }

  return { buildInvest, buildRedeem, fund, prices, ata, A, P, RELAYER, txCost, toStable, b64 };
}
