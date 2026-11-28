/* Minimal Solana transaction codec for the gas service — no web3.js (it does
 * not load in Vercel's function runtime). Handles what the gas service needs:
 * v0 transactions (and legacy, which it then refuses), address lookup tables,
 * associated-token-account derivation, ed25519 signing and verification. */
import bs58 from 'bs58';
import { ed25519 } from '@noble/curves/ed25519';
import { sha256 } from '@noble/hashes/sha256';

export class Key {
  constructor(bytes) { this.bytes = bytes; this.b58 = bs58.encode(Buffer.from(bytes)); }
  static from(b58) { return new Key(Uint8Array.from(bs58.decode(b58))); }
  toBase58() { return this.b58; }
  toBytes() { return this.bytes; }
}

function readLen(b, o) { // compact-u16
  let len = 0, size = 0;
  for (;;) { const e = b[o + size]; len |= (e & 0x7f) << (size * 7); size++; if (!(e & 0x80)) break; }
  return [len, size];
}

export function parseTransaction(bytes) {
  const b = Uint8Array.from(bytes);
  let o = 0;
  const [nSig, s1] = readLen(b, o); o += s1;
  const signatures = [];
  for (let i = 0; i < nSig; i++) { signatures.push(b.slice(o, o + 64)); o += 64; }
  const msgStart = o;
  const versioned = (b[o] & 0x80) !== 0;
  const version = versioned ? b[o] & 0x7f : 'legacy';
  if (versioned) o++;
  const header = { numRequiredSignatures: b[o], numReadonlySignedAccounts: b[o + 1], numReadonlyUnsignedAccounts: b[o + 2] }; o += 3;
  const [nKeys, s2] = readLen(b, o); o += s2;
  const staticAccountKeys = [];
  for (let i = 0; i < nKeys; i++) { staticAccountKeys.push(new Key(b.slice(o, o + 32))); o += 32; }
  const recentBlockhash = bs58.encode(Buffer.from(b.slice(o, o + 32))); o += 32;
  const [nIx, s3] = readLen(b, o); o += s3;
  const compiledInstructions = [];
  for (let i = 0; i < nIx; i++) {
    const programIdIndex = b[o]; o++;
    const [na, sa] = readLen(b, o); o += sa;
    const accountKeyIndexes = Array.from(b.slice(o, o + na)); o += na;
    const [nd, sd] = readLen(b, o); o += sd;
    const data = b.slice(o, o + nd); o += nd;
    compiledInstructions.push({ programIdIndex, accountKeyIndexes, data });
  }
  const addressTableLookups = [];
  if (versioned) {
    const [nl, sl] = readLen(b, o); o += sl;
    for (let i = 0; i < nl; i++) {
      const accountKey = new Key(b.slice(o, o + 32)); o += 32;
      const [nw, sw] = readLen(b, o); o += sw; const writableIndexes = Array.from(b.slice(o, o + nw)); o += nw;
      const [nr, sr] = readLen(b, o); o += sr; const readonlyIndexes = Array.from(b.slice(o, o + nr)); o += nr;
      addressTableLookups.push({ accountKey, writableIndexes, readonlyIndexes });
    }
  }
  if (o !== b.length) throw new Error('trailing bytes in transaction');
  const messageBytes = b.slice(msgStart);
  return {
    signatures,
    message: { version, header, staticAccountKeys, recentBlockhash, compiledInstructions, addressTableLookups, serialize: () => messageBytes },
    serialize() {
      const out = new Uint8Array(b.length); out.set(b);
      signatures.forEach((s, i) => out.set(s, 1 + i * 64)); // signature count < 128 → one length byte
      return out;
    },
  };
}

/* Address lookup table account: 56-byte header, then 32-byte addresses. */
export function lookupTableAddresses(data) {
  const out = [];
  for (let o = 56; o + 32 <= data.length; o += 32) out.push(new Key(data.slice(o, o + 32)));
  return out;
}

export function keypairFromSecret(raw) {
  const s = raw.trim().startsWith('[') ? Uint8Array.from(JSON.parse(raw)) : Uint8Array.from(bs58.decode(raw.trim()));
  if (s.length !== 64) throw new Error('secret key must be 64 bytes');
  const seed = s.slice(0, 32);
  const pub = ed25519.getPublicKey(seed);
  if (Buffer.compare(Buffer.from(pub), Buffer.from(s.slice(32))) !== 0) throw new Error('secret key does not match its public key');
  return { publicKey: new Key(pub), sign: (msg) => ed25519.sign(msg, seed) };
}

export const verify = (sig, msg, pub) => ed25519.verify(sig, msg, pub);

function onCurve(bytes) { try { ed25519.ExtendedPoint.fromHex(bytes); return true; } catch { return false; } }
export function findProgramAddress(seeds, programId) {
  const pda = enc('ProgramDerivedAddress');
  for (let bump = 255; bump >= 0; bump--) {
    const h = sha256(concat(...seeds, Uint8Array.from([bump]), programId.toBytes(), pda));
    if (!onCurve(h)) return new Key(h);
  }
  throw new Error('no viable bump');
}
const enc = (s) => new TextEncoder().encode(s);
const concat = (...a) => { const o = new Uint8Array(a.reduce((n, x) => n + x.length, 0)); let i = 0; for (const x of a) { o.set(x, i); i += x.length; } return o; };
const TOKEN = Key.from('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA');
const ATA = Key.from('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL');
export const ataOf = (owner, mintB58, tokenProgram = TOKEN) =>
  findProgramAddress([owner.toBytes(), tokenProgram.toBytes(), Key.from(mintB58).toBytes()], ATA).toBase58();

/* ---------------------------------------------------------------- building
 * v4 (28 Nov): the gas service now BUILDS transactions of its own (fills,
 * settles, cancels), not only checks the user's. The same compile rules as
 * web3.js's compileToV0Message: payer first; signers before non-signers;
 * writable before read-only; invoked program ids stay static; any other
 * non-signer key found in a given lookup table is loaded from it. */
function writeLen(n) { // compact-u16
  const out = [];
  for (;;) { let e = n & 0x7f; n >>= 7; if (n) e |= 0x80; out.push(e); if (!n) break; }
  return Uint8Array.from(out);
}

/* instructions: [{ programId: Key, keys: [{ pubkey: Key, isSigner, isWritable }], data: Uint8Array }]
 * tables: [{ key: Key, addresses: Key[] }]  → Uint8Array message bytes. */
export function compileV0({ payer, recentBlockhash, instructions, tables = [] }) {
  const meta = new Map(); // b58 → { key, isSigner, isWritable, isInvoked }
  const touch = (key, f) => {
    const b = key.toBase58();
    const m = meta.get(b) || { key, isSigner: false, isWritable: false, isInvoked: false };
    Object.assign(m, { isSigner: m.isSigner || !!f.isSigner, isWritable: m.isWritable || !!f.isWritable, isInvoked: m.isInvoked || !!f.isInvoked });
    meta.set(b, m);
  };
  touch(payer, { isSigner: true, isWritable: true });
  for (const ix of instructions) {
    touch(ix.programId, { isInvoked: true });
    for (const k of ix.keys) touch(k.pubkey, k);
  }
  const all = [...meta.values()];
  const payerB = payer.toBase58();
  const ws = all.filter((m) => m.isSigner && m.isWritable && m.key.toBase58() !== payerB);
  const rs = all.filter((m) => m.isSigner && !m.isWritable);
  let wn = all.filter((m) => !m.isSigner && m.isWritable);
  let rn = all.filter((m) => !m.isSigner && !m.isWritable);
  // Lookups: non-signer, non-invoked keys present in a table.
  const lookups = [];
  for (const t of tables) {
    const inTable = new Map(t.addresses.map((a, i) => [a.toBase58(), i]));
    const pick = (list) => {
      const idx = [];
      const rest = [];
      for (const m of list) {
        const i = inTable.get(m.key.toBase58());
        if (i !== undefined && !m.isInvoked) idx.push([i, m]); else rest.push(m);
      }
      return [idx, rest];
    };
    const [wi, wrest] = pick(wn);
    const [ri, rrest] = pick(rn);
    wn = wrest; rn = rrest;
    if (wi.length || ri.length) lookups.push({ key: t.key, writable: wi, readonly: ri });
  }
  const statics = [meta.get(payerB), ...ws, ...rs, ...wn, ...rn];
  const header = Uint8Array.from([1 + ws.length + rs.length, rs.length, rn.length]);
  const index = new Map();
  statics.forEach((m, i) => index.set(m.key.toBase58(), i));
  let n = statics.length;
  for (const l of lookups) for (const [, m] of l.writable) index.set(m.key.toBase58(), n++);
  for (const l of lookups) for (const [, m] of l.readonly) index.set(m.key.toBase58(), n++);

  const parts = [Uint8Array.from([0x80]), header, writeLen(statics.length), ...statics.map((m) => m.key.toBytes()),
    Uint8Array.from(bs58.decode(recentBlockhash)), writeLen(instructions.length)];
  for (const ix of instructions) {
    const idx = ix.keys.map((k) => index.get(k.pubkey.toBase58()));
    parts.push(Uint8Array.from([index.get(ix.programId.toBase58())]), writeLen(idx.length), Uint8Array.from(idx), writeLen(ix.data.length), ix.data);
  }
  parts.push(writeLen(lookups.length));
  for (const l of lookups) {
    parts.push(l.key.toBytes(), writeLen(l.writable.length), Uint8Array.from(l.writable.map(([i]) => i)),
      writeLen(l.readonly.length), Uint8Array.from(l.readonly.map(([i]) => i)));
  }
  return { message: concat(...parts), numSigners: header[0], signerKeys: statics.slice(0, header[0]).map((m) => m.key) };
}

/* Sign with every keypair given (by position among the message's signers) and
 * serialize. Keypairs: { publicKey: Key, sign(msg) }. Unsigned slots stay zero. */
export function serializeSigned(compiled, keypairs) {
  const sigs = compiled.signerKeys.map((k) => {
    const kp = keypairs.find((p) => p.publicKey.toBase58() === k.toBase58());
    return kp ? kp.sign(compiled.message) : new Uint8Array(64);
  });
  return concat(writeLen(sigs.length), ...sigs, compiled.message);
}

/* Helpers for building instruction data. */
export const u64le = (v) => { const b = new Uint8Array(8); new DataView(b.buffer).setBigUint64(0, BigInt(v), true); return b; };
export const i64le = (v) => { const b = new Uint8Array(8); new DataView(b.buffer).setBigInt64(0, BigInt(v), true); return b; };
export const u32le = (v) => { const b = new Uint8Array(4); new DataView(b.buffer).setUint32(0, v, true); return b; };
export const cat = concat;
export const anchorDisc = (name) => sha256(enc(`global:${name}`)).slice(0, 8);
export const meta = (pubkey, isSigner, isWritable) => ({ pubkey, isSigner, isWritable });
