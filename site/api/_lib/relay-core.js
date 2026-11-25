/* Permafrost gas service — the checks. Pure: no network, no key.
 *
 * The gas service's key pays every transaction's fee and the rent of any new
 * token account, and tips Jito. It must never be usable for anything else. The
 * rule is positional: the gas key may appear ONLY as
 *   1. the fee payer (account 0 of every transaction),
 *   2. the payer of an associated-token-account create (account 0 of that instruction),
 *   3. the source of one System transfer to a Jito tip account, at most MAX_TIP lamports.
 * Anywhere else — any other instruction, any other position, including inside an
 * address lookup table — and the whole bundle is refused. That one rule is what
 * stops a crafted transaction from moving the gas service's SOL or tokens: every
 * such move needs the gas key as a signer or authority in some other position.
 *
 * Then it prices what the bundle costs the gas service and refuses unless the
 * stablecoin paid to the gas service's own USDC/USDT accounts covers it.
 */

export const LIMITS = {
  MAX_TXS: 5,                 // a Jito bundle
  MAX_TIP: 100_000,           // lamports, per bundle
  MAX_CU_PRICE: 50_000,       // micro-lamports per CU
  MAX_CU_LIMIT: 1_400_000,
  LAMPORTS_PER_SIGNATURE: 5000,
  RENT_ATA: 2_140_000,        // the largest token account we create (xStock, 179 B)
  MARGIN: 1.1,                // the client adds 1.2; accept down to 1.1
};

export const ALLOWED_PROGRAMS = {
  ComputeBudget111111111111111111111111111111: 'compute-budget',
  ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL: 'ata',
  TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA: 'token',
  TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb: 'token-2022',
  JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4: 'jupiter',
  '11111111111111111111111111111111': 'system',
};

const STABLE_DECIMALS = 6;

export class Refused extends Error {}
const refuse = (m) => { throw new Refused(m); };

/* txs: VersionedTransaction[]; resolveAlts(addr) → PublicKey[] (the table's
 * addresses); existing(set of base58) tells which token accounts already exist;
 * ctx: { relayer, programId, tipAccounts:Set, stableAccounts:{base58: mint},
 *        solUsd }. Returns { user, costLamports, paidUsd, needUsd }. */
export function check(txs, ctx, resolveAlts, existing, web3) {
  if (!Array.isArray(txs) || txs.length < 1 || txs.length > LIMITS.MAX_TXS) refuse('bundle must hold 1–5 transactions');
  const R = ctx.relayer;
  const allowed = { ...ALLOWED_PROGRAMS, [ctx.programId]: 'permafrost' };
  let user = null;
  let cost = 0;
  let tips = 0;
  let paidBase = 0n;
  const created = new Set();
  let blockhash = null;

  for (const [t, tx] of txs.entries()) {
    const m = tx.message;
    if (m.version !== 0) refuse(`tx ${t}: must be a v0 transaction`);
    if (blockhash === null) blockhash = m.recentBlockhash;
    else if (m.recentBlockhash !== blockhash) refuse('all transactions must share one blockhash');

    const statics = m.staticAccountKeys.map((k) => k.toBase58());
    if (statics[0] !== R) refuse(`tx ${t}: the gas service must be the fee payer`);
    const nSig = m.header.numRequiredSignatures;
    if (nSig !== 2) refuse(`tx ${t}: exactly two signers (gas service, user)`);
    if (user === null) user = statics[1];
    else if (statics[1] !== user) refuse('every transaction must be signed by the same user');
    if (statics.indexOf(R, 1) !== -1) refuse(`tx ${t}: gas key appears twice`);

    // Full account list: statics, then each table's writable picks, then readonly.
    const keys = [...statics];
    const writ = [];
    const ro = [];
    for (const l of m.addressTableLookups) {
      const table = resolveAlts(l.accountKey.toBase58());
      if (!table) refuse(`tx ${t}: unknown lookup table`);
      for (const i of l.writableIndexes) writ.push(table[i]?.toBase58() ?? refuse('bad lookup index'));
      for (const i of l.readonlyIndexes) ro.push(table[i]?.toBase58() ?? refuse('bad lookup index'));
    }
    keys.push(...writ, ...ro);
    if (keys.indexOf(R, 1) !== -1) refuse(`tx ${t}: gas key hidden in an account list`);

    let cuLimit = 200_000;
    let cuPrice = 0;
    for (const [n, ci] of m.compiledInstructions.entries()) {
      const prog = keys[ci.programIdIndex];
      const kind = allowed[prog];
      if (!kind) refuse(`tx ${t} ix ${n}: program ${prog} not allowed`);
      const acc = ci.accountKeyIndexes.map((i) => keys[i]);
      const d = ci.data;
      const usesR = acc.map((k, i) => (k === R ? i : -1)).filter((i) => i >= 0);

      if (kind === 'compute-budget') {
        if (d[0] === 2) cuLimit = new DataView(d.buffer, d.byteOffset).getUint32(1, true);
        else if (d[0] === 3) cuPrice = Number(new DataView(d.buffer, d.byteOffset).getBigUint64(1, true));
        else refuse(`tx ${t} ix ${n}: compute-budget instruction ${d[0]} not allowed`);
        continue;
      }
      if (kind === 'ata') {
        // CreateIdempotent only: [payer, ata, owner, mint, system, token program].
        if (d.length !== 1 || d[0] !== 1) refuse(`tx ${t} ix ${n}: only create-idempotent`);
        if (acc[0] !== R) refuse(`tx ${t} ix ${n}: the gas service pays account creation`);
        if (usesR.length !== 1) refuse(`tx ${t} ix ${n}: gas key in a second position`);
        if (acc[2] === R) refuse(`tx ${t} ix ${n}: no accounts owned by the gas key`);
        if (!existing.has(acc[1]) && !created.has(acc[1])) { created.add(acc[1]); cost += LIMITS.RENT_ATA; }
        continue;
      }
      if (kind === 'system') {
        // Only the tip: Transfer (2) from the gas key to a Jito tip account.
        const dv = new DataView(d.buffer, d.byteOffset);
        if (d.length !== 12 || dv.getUint32(0, true) !== 2) refuse(`tx ${t} ix ${n}: only a tip transfer`);
        if (acc[0] !== R || !ctx.tipAccounts.has(acc[1])) refuse(`tx ${t} ix ${n}: system transfer must be gas key → Jito tip`);
        const lamports = Number(dv.getBigUint64(4, true));
        tips += lamports;
        cost += lamports;
        continue;
      }
      if (usesR.length) refuse(`tx ${t} ix ${n}: gas key used by ${kind}`);
      if (kind === 'token') {
        // Count TransferChecked (12) into the gas service's own stable accounts.
        if (d[0] === 12 && ctx.stableAccounts[acc[2]] && ctx.stableAccounts[acc[2]] === acc[1]) {
          paidBase += new DataView(d.buffer, d.byteOffset).getBigUint64(1, true);
        }
      }
      // token-2022, jupiter and permafrost instructions never involve the gas key
      // (checked above); what they do with the user's own funds is the user's
      // signature's business.
    }
    if (cuPrice > LIMITS.MAX_CU_PRICE) refuse(`tx ${t}: priority fee above the cap`);
    if (cuLimit > LIMITS.MAX_CU_LIMIT) refuse(`tx ${t}: compute limit above the cap`);
    cost += nSig * LIMITS.LAMPORTS_PER_SIGNATURE + Math.ceil((cuLimit * cuPrice) / 1e6);
  }
  if (tips > LIMITS.MAX_TIP) refuse('tip above the cap');
  if (tips === 0) refuse('bundle must tip Jito');

  const needUsd = (cost / 1e9) * ctx.solUsd * LIMITS.MARGIN;
  const paidUsd = Number(paidBase) / 10 ** STABLE_DECIMALS;
  if (paidUsd + 1e-9 < needUsd) refuse(`reimbursement $${paidUsd.toFixed(4)} below cost $${needUsd.toFixed(4)}`);
  return { user, costLamports: cost, paidUsd, needUsd, blockhash };
}
