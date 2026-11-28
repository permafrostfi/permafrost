/* The gas service's own v0 compiler (site/api/_lib/tx.js) against web3.js as the
 * oracle: same instructions, same tables → identical message bytes, and the
 * signed transaction parses back and verifies.
 *   node tools/test_tx_compile.mjs */
import web3 from '@solana/web3.js';
import { Key, compileV0, serializeSigned, parseTransaction, verify, keypairFromSecret, meta } from '../site/api/_lib/tx.js';

let failures = 0;
const ok = (c, label, detail = '') => { console.log(`${c ? '  pass' : '  FAIL'}  ${label}${detail ? '  ' + detail : ''}`); if (!c) failures++; };
const K = (pk) => new Key(pk.toBytes());

const payer = web3.Keypair.generate(), user = web3.Keypair.generate();
const progA = web3.Keypair.generate().publicKey, progB = web3.Keypair.generate().publicKey;
const tableKey = web3.Keypair.generate().publicKey;
const tableAddrs = Array.from({ length: 12 }, () => web3.Keypair.generate().publicKey);
const extra = Array.from({ length: 6 }, () => web3.Keypair.generate().publicKey);
const blockhash = web3.Keypair.generate().publicKey.toBase58();

// A mix: payer, a second signer, a program also passed as a readonly account,
// table addresses used writable and read-only, static non-table keys.
const ixs = [
  new web3.TransactionInstruction({ programId: progA, keys: [
    { pubkey: user.publicKey, isSigner: true, isWritable: false },
    { pubkey: tableAddrs[3], isSigner: false, isWritable: true },
    { pubkey: tableAddrs[7], isSigner: false, isWritable: false },
    { pubkey: extra[0], isSigner: false, isWritable: true },
    { pubkey: extra[1], isSigner: false, isWritable: false },
    { pubkey: progB, isSigner: false, isWritable: false },
  ], data: Buffer.from([1, 2, 3, 4, 5]) }),
  new web3.TransactionInstruction({ programId: progB, keys: [
    { pubkey: tableAddrs[7], isSigner: false, isWritable: true },   // upgraded to writable by this ix
    { pubkey: tableAddrs[0], isSigner: false, isWritable: false },
    { pubkey: payer.publicKey, isSigner: true, isWritable: true },
    { pubkey: extra[2], isSigner: false, isWritable: false },
  ], data: Buffer.alloc(200, 7) }),
];
const table = new web3.AddressLookupTableAccount({ key: tableKey, state: { deactivationSlot: 0n, lastExtendedSlot: 0, lastExtendedSlotStartIndex: 0, authority: undefined, addresses: tableAddrs } });
const want = new web3.TransactionMessage({ payerKey: payer.publicKey, recentBlockhash: blockhash, instructions: ixs }).compileToV0Message([table]).serialize();

const mine = compileV0({
  payer: K(payer.publicKey), recentBlockhash: blockhash,
  instructions: ixs.map((ix) => ({ programId: K(ix.programId), keys: ix.keys.map((k) => meta(K(k.pubkey), k.isSigner, k.isWritable)), data: Uint8Array.from(ix.data) })),
  tables: [{ key: K(tableKey), addresses: tableAddrs.map(K) }],
});
ok(Buffer.compare(Buffer.from(mine.message), Buffer.from(want)) === 0, 'message bytes identical to web3.js (with a lookup table)', `${mine.message.length} bytes`);

const noTable = compileV0({ payer: K(payer.publicKey), recentBlockhash: blockhash,
  instructions: ixs.map((ix) => ({ programId: K(ix.programId), keys: ix.keys.map((k) => meta(K(k.pubkey), k.isSigner, k.isWritable)), data: Uint8Array.from(ix.data) })) });
const wantNo = new web3.TransactionMessage({ payerKey: payer.publicKey, recentBlockhash: blockhash, instructions: ixs }).compileToV0Message([]).serialize();
ok(Buffer.compare(Buffer.from(noTable.message), Buffer.from(wantNo)) === 0, 'message bytes identical to web3.js (no table)');

const kpP = keypairFromSecret(JSON.stringify([...payer.secretKey])), kpU = keypairFromSecret(JSON.stringify([...user.secretKey]));
const bytes = serializeSigned(mine, [kpP, kpU]);
const parsed = parseTransaction(bytes);
ok(parsed.signatures.length === 2 && parsed.message.staticAccountKeys[0].toBase58() === payer.publicKey.toBase58(), 'signed transaction parses back');
ok(parsed.signatures.every((s, i) => verify(s, parsed.message.serialize(), parsed.message.staticAccountKeys[i].toBytes())), 'both signatures verify');
const w3 = web3.VersionedTransaction.deserialize(bytes);
ok(w3.signatures.length === 2, 'web3.js deserializes it too');
console.log(failures ? `\n${failures} FAILED` : '\nall checks passed');
process.exit(failures ? 1 : 0);
