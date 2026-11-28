/* Unit tests for the gas service's checks (site/api/_lib/relay-core.js).
 * Every way to make the gas key pay for or sign something it shouldn't must be
 * refused; a correctly-built bundle must pass.  node tools/test_relay.mjs */
import web3 from '@solana/web3.js';
import { check, Refused, LIMITS } from '../site/api/_lib/relay-core.js';

const { Keypair, PublicKey, TransactionMessage, VersionedTransaction, TransactionInstruction, SystemProgram,
  ComputeBudgetProgram, AddressLookupTableAccount } = web3;
const R = Keypair.generate(), U = Keypair.generate(), M = Keypair.generate();
const PROGRAM = Keypair.generate().publicKey;
const TIP = Keypair.generate().publicKey;
const JUP = new PublicKey('JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4');
const TOKEN = new PublicKey('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA');
const T22 = new PublicKey('TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb');
const ATAP = new PublicKey('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL');
const USDC = new PublicKey('EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v');
const ata = (o, m, p = TOKEN) => PublicKey.findProgramAddressSync([o.toBuffer(), p.toBuffer(), m.toBuffer()], ATAP)[0];
const RUSDC = ata(R.publicKey, USDC);
const ctx = { relayer: R.publicKey.toBase58(), programId: PROGRAM.toBase58(), tipAccounts: new Set([TIP.toBase58()]),
  stableAccounts: { [RUSDC.toBase58()]: USDC.toBase58() }, solUsd: 69 };
const bh = Keypair.generate().publicKey.toBase58();
const m = (k, s, w) => ({ pubkey: k, isSigner: s, isWritable: w });
const ix = (p, keys, data) => new TransactionInstruction({ programId: p, keys, data: Buffer.from(data) });

const budget = (price = 10000) => [ComputeBudgetProgram.setComputeUnitLimit({ units: 300000 }), ComputeBudgetProgram.setComputeUnitPrice({ microLamports: price })];
const createAta = (payer, owner, mint, prog = T22) => ix(ATAP, [m(payer, true, true), m(ata(owner, mint, prog), false, true), m(owner, false, false), m(mint, false, false), m(SystemProgram.programId, false, false), m(prog, false, false)], [1]);
const jupSwap = (extra = []) => ix(JUP, [m(U.publicKey, true, false), m(Keypair.generate().publicKey, false, true), ...extra], [1, 2, 3]);
const reimburse = (amount, from = U.publicKey, to = RUSDC, auth = U.publicKey) => {
  const d = Buffer.alloc(10); d[0] = 12; d.writeBigUInt64LE(BigInt(amount), 1); d[9] = 6;
  return ix(TOKEN, [m(ata(from, USDC), false, true), m(USDC, false, false), m(to, false, true), m(auth, true, false)], d);
};
const tip = (lamports = 20000, to = TIP, from = R.publicKey) => SystemProgram.transfer({ fromPubkey: from, toPubkey: to, lamports });
const ours = () => ix(PROGRAM, [m(U.publicKey, true, false), m(Keypair.generate().publicKey, false, true)], [9, 9]);

function tx(ixs, { payer = R.publicKey, alts = [], signers = [U] } = {}) {
  const msg = new TransactionMessage({ payerKey: payer, recentBlockhash: bh, instructions: ixs }).compileToV0Message(alts);
  const t = new VersionedTransaction(msg);
  t.sign(signers);
  return t;
}
const good = () => [
  tx([...budget(), createAta(R.publicKey, U.publicKey, M.publicKey), jupSwap(), reimburse(250000)]),
  tx([...budget(), ours(), reimburse(250000), tip()]),
];
const run = (txs, alts = {}) => check(txs, ctx, (k) => alts[k], new Set(), web3);

let pass = 0, fail = 0;
function expectOk(name, f) {
  try { const r = f(); pass++; console.log(`  pass  ${name}  (cost ${r.costLamports} lamports, need $${r.needUsd.toFixed(4)}, paid $${r.paidUsd})`); }
  catch (e) { fail++; console.log(`  FAIL  ${name}: ${e.message}`); }
}
function expectRefused(name, f, fragment) {
  try { f(); fail++; console.log(`  FAIL  ${name}: was accepted`); }
  catch (e) {
    if (!(e instanceof Refused)) { fail++; console.log(`  FAIL  ${name}: crashed ${e.message}`); return; }
    if (fragment && !e.message.includes(fragment)) { fail++; console.log(`  FAIL  ${name}: wrong reason "${e.message}"`); return; }
    pass++; console.log(`  pass  ${name}  → ${e.message}`);
  }
}

console.log('accepted');
expectOk('a correctly built two-transaction bundle', () => run(good()));

console.log('refused — the gas key used anywhere but its three places');
expectRefused('gas key inside a Jupiter instruction', () => run([tx([...budget(), jupSwap([m(R.publicKey, false, true)]), reimburse(250000), tip()])]), 'gas key');
expectRefused('gas key inside our own program instruction', () => run([tx([...budget(), ix(PROGRAM, [m(R.publicKey, false, true), m(U.publicKey, true, false)], [1]), reimburse(250000), tip()])]), 'gas key');
expectRefused('gas USDC moved out with the gas key as authority', () => run([tx([...budget(), reimburse(250000), ix(TOKEN, [m(RUSDC, false, true), m(USDC, false, false), m(ata(U.publicKey, USDC), false, true), m(R.publicKey, true, false)], Buffer.from([12, 0, 0, 0, 0, 1, 0, 0, 0, 6])), tip()], { signers: [U] })]), 'gas key');
const altKey = Keypair.generate().publicKey;
const alt = new AddressLookupTableAccount({ key: altKey, state: { deactivationSlot: BigInt('18446744073709551615'), lastExtendedSlot: 0, lastExtendedSlotStartIndex: 0, authority: undefined, addresses: [R.publicKey, Keypair.generate().publicKey] } });
expectRefused('gas key hidden in an address lookup table', () => {
  const t = tx([...budget(), ix(JUP, [m(U.publicKey, true, false), m(R.publicKey, false, false)], [1]), reimburse(250000), tip()], { alts: [] });
  // Recompile with the lookup table so the gas key travels inside it.
  const msg = new TransactionMessage({ payerKey: R.publicKey, recentBlockhash: bh, instructions: [...budget(), ix(JUP, [m(U.publicKey, true, false), m(alt.state.addresses[1], false, true)], [1]), reimburse(250000), tip()] }).compileToV0Message([alt]);
  void t;
  const v = new VersionedTransaction(msg); v.sign([U]);
  // Point a lookup at index 0 (the gas key) by hand.
  v.message.addressTableLookups[0].readonlyIndexes.push(0);
  return run([v], { [altKey.toBase58()]: alt.state.addresses });
}, 'hidden');
expectRefused('system transfer from the gas key to a non-tip account', () => run([tx([...budget(), reimburse(250000), tip(20000, Keypair.generate().publicKey)])]), 'Jito tip');
expectRefused('tip above the cap', () => run([tx([...budget(), reimburse(10_000_000), tip(LIMITS.MAX_TIP + 1)])]), 'tip above');
expectRefused('account creation paid by the user while gas key is fee payer is fine, but gas key as owner is not', () => run([tx([...budget(), createAta(R.publicKey, R.publicKey, M.publicKey), reimburse(250000), tip()])]), 'gas key');
expectRefused('the gas key not the fee payer', () => run([tx([...budget(), reimburse(250000), tip()], { payer: U.publicKey, signers: [U] })]), 'fee payer');

console.log('refused — shape and economics');
expectRefused('unknown program', () => run([tx([...budget(), ix(Keypair.generate().publicKey, [m(U.publicKey, true, false)], [1]), reimburse(250000), tip()])]), 'not allowed');
expectRefused('priority fee above the cap', () => run([tx([...budget(LIMITS.MAX_CU_PRICE + 1), reimburse(5_000_000), tip()])]), 'priority');
expectRefused('reimbursement too small', () => run([tx([...budget(), createAta(R.publicKey, U.publicKey, M.publicKey), jupSwap(), reimburse(100), tip()])]), 'below cost');
expectRefused('reimbursement sent somewhere else', () => run([tx([...budget(), createAta(R.publicKey, U.publicKey, M.publicKey), jupSwap(), reimburse(500000, U.publicKey, ata(Keypair.generate().publicKey, USDC)), tip()])]), 'below cost');
expectRefused('no Jito tip', () => run([tx([...budget(), reimburse(250000)])]), 'tip');
const other = Keypair.generate();
expectRefused('two different users in one bundle', () => run([good()[0], tx([...budget(), ix(PROGRAM, [m(other.publicKey, true, false)], [1]), reimburse(250000, other.publicKey, RUSDC, other.publicKey), tip()], { signers: [other] })]), 'same user');
expectRefused('six transactions', () => run([...good(), ...good(), ...good()]), '1–5');
expectRefused('a compute-budget heap request', () => run([tx([ix(new PublicKey('ComputeBudget111111111111111111111111111111'), [], [1, 0, 0, 1, 0]), reimburse(250000), tip()])]), 'compute-budget');

console.log('locked bonus: position rent and free claim / exit');
const disc = { deposit_locked: [88, 91, 135, 52, 79, 190, 164, 141], claim_locked: [34, 206, 181, 23, 11, 207, 147, 90], exit_locked: [207, 93, 52, 157, 105, 252, 90, 187], redeem: [184, 12, 86, 149, 70, 196, 97, 225], deposit: [242, 35, 198, 137, 82, 225, 242, 182] };
const ourIx = (name) => ix(PROGRAM, [m(U.publicKey, true, true), m(Keypair.generate().publicKey, false, true)], [...disc[name], 0, 0]);
const gift = (lamports = 1_061_720, to = U.publicKey) => SystemProgram.transfer({ fromPubkey: R.publicKey, toPubkey: to, lamports });
expectOk('locked deposit: gas key gives the user the position rent, reimbursed', () => run([tx([...budget(), gift(), ourIx('deposit_locked'), reimburse(400000), tip()])]));
expectRefused('position rent without a locked deposit in the same transaction', () => run([tx([...budget(), gift(), ours(), reimburse(5_000_000), tip()])]), 'position rent');
expectRefused('position rent above the cap', () => run([tx([...budget(), gift(LIMITS.POSITION_RENT + 1), ourIx('deposit_locked'), reimburse(5_000_000), tip()])]), 'position rent');
expectRefused('position rent to someone other than the user', () => run([tx([...budget(), gift(1_061_720, Keypair.generate().publicKey), ourIx('deposit_locked'), reimburse(5_000_000), tip()])]), 'position rent');
expectRefused('two position-rent gifts in one transaction', () => run([tx([...budget(), gift(), gift(), ourIx('deposit_locked'), reimburse(5_000_000), tip()])]), 'position rent');
expectOk('a lone claim rides free (no stablecoin needed)', () => { const r = run([tx([...budget(), ourIx('claim_locked'), tip()])]); if (!r.freeSettle) throw new Error('not free'); return r; });
expectOk('a lone exit rides free', () => run([tx([...budget(), ourIx('exit_locked'), tip()])]));
expectRefused('claim plus a swap, unpaid', () => run([tx([...budget(), ourIx('claim_locked'), jupSwap(), tip()])]), 'below cost');
expectRefused('claim plus a new token account, unpaid', () => run([tx([...budget(), createAta(R.publicKey, U.publicKey, M.publicKey), ourIx('claim_locked'), tip()])]), 'below cost');
expectOk('a lone redeem rides free (withdraw step 1, 28 Nov)', () => { const r = run([tx([...budget(), ourIx('redeem'), tip()])]); if (!r.freeSettle) throw new Error('not free'); return r; });
expectRefused('redeem that creates token accounts, unpaid', () => run([tx([...budget(), createAta(R.publicKey, U.publicKey, M.publicKey), ourIx('redeem'), tip()])]), 'below cost');
expectOk('redeem that creates token accounts, reimbursed', () => run([tx([...budget(), createAta(R.publicKey, U.publicKey, M.publicKey), ourIx('redeem'), reimburse(400000), tip()])]));
expectRefused('redeem plus a swap, unpaid (the old one-bundle withdraw)', () => run([tx([...budget(), ourIx('redeem'), tip()]), tx([...budget(), jupSwap()])]), 'below cost');
expectRefused('a lone deposit, unpaid', () => run([tx([...budget(), ourIx('deposit'), tip()])]), 'below cost');
expectRefused('two claims in a two-transaction bundle, unpaid', () => run([tx([...budget(), ourIx('claim_locked'), tip()]), tx([...budget(), ourIx('claim_locked')])]), 'below cost');

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
