/* Token-2022 account decoders.
 *
 * Dependency-free. Verified against the four live xStock mints on
 * mainnet-beta, 23 Nov 2026 (see tools/verify_chain.mjs).
 *
 * Layout notes that cost time to get right:
 *   - A Token-2022 mint's base data is 82 bytes, but the account is padded to
 *     BASE_ACCOUNT_LENGTH (165) so a mint can't be confused with a token
 *     account. Byte 165 is the account type. The extension TLV therefore
 *     starts at 166, not 165, for BOTH mints and token accounts.
 *   - ScaledUiAmountConfig carries TWO multipliers. The one in force is
 *     new_multiplier once new_multiplier_effective_timestamp has passed.
 *     On all four xStock mints that timestamp is already in the past, so a
 *     reader that uses the `multiplier` field understates holdings by
 *     8-14 bps. Always use activeMultiplier().
 */

export const TOKEN_2022 = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb';
export const TLV_START = 166;

export const EXT = {
  DEFAULT_ACCOUNT_STATE: 6,
  PERMANENT_DELEGATE: 12,
  TRANSFER_HOOK: 14,
  METADATA_POINTER: 18,
  TOKEN_METADATA: 19,
  SCALED_UI_AMOUNT: 25,
  PAUSABLE: 26,
};

const B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';

export function toBase58(bytes) {
  const digits = [0];
  for (const byte of bytes) {
    let carry = byte;
    for (let i = 0; i < digits.length; i++) {
      carry += digits[i] << 8;
      digits[i] = carry % 58;
      carry = (carry / 58) | 0;
    }
    while (carry) { digits.push(carry % 58); carry = (carry / 58) | 0; }
  }
  // Drop the most significant zero digit, so an all-zero input encodes to
  // leading '1's alone and not one extra. Without this the all-zero pubkey
  // comes back 33 characters long and compares unequal to the real thing.
  while (digits.length > 1 && digits[digits.length - 1] === 0) digits.pop();
  if (digits.length === 1 && digits[0] === 0) digits.pop();
  let out = '';
  for (const byte of bytes) { if (byte === 0) out += '1'; else break; }
  return out + digits.reverse().map((d) => B58[d]).join('');
}

export function fromBase64(b64) {
  if (typeof Buffer !== 'undefined') return new Uint8Array(Buffer.from(b64, 'base64'));
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function view(bytes) {
  return new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
}

/* Walk the extension TLV. Returns {type: Uint8Array payload}. */
export function readExtensions(bytes) {
  const dv = view(bytes);
  const found = {};
  let off = TLV_START;
  while (off + 4 <= bytes.length) {
    const type = dv.getUint16(off, true);
    const len = dv.getUint16(off + 2, true);
    if (type === 0 && len === 0) break;
    if (off + 4 + len > bytes.length) break;
    found[type] = bytes.subarray(off + 4, off + 4 + len);
    off += 4 + len;
  }
  return found;
}

/* ScaledUiAmountConfig: authority(32) multiplier(f64) effectiveTs(i64) newMultiplier(f64) */
export function readScaledUi(payload) {
  if (!payload || payload.length < 56) return null;
  const dv = view(payload);
  return {
    authority: toBase58(payload.subarray(0, 32)),
    multiplier: dv.getFloat64(32, true),
    effectiveTimestamp: Number(dv.getBigInt64(40, true)),
    newMultiplier: dv.getFloat64(48, true),
  };
}

/* The multiplier actually in force right now. */
export function activeMultiplier(scaled, nowSeconds) {
  if (!scaled) return 1;
  const now = nowSeconds ?? Math.floor(Date.now() / 1000);
  return now >= scaled.effectiveTimestamp ? scaled.newMultiplier : scaled.multiplier;
}

/* Test the bytes, not the base58 — an encoder bug must not read as "a hook
 * is set" or, far worse, as "no hook is set". */
const allZero = (bytes) => bytes.every((b) => b === 0);

/* Full mint decode, including the issuer powers the risk disclosure turns on. */
export function decodeMint(bytes, nowSeconds) {
  const dv = view(bytes);
  const ext = readExtensions(bytes);
  const scaled = readScaledUi(ext[EXT.SCALED_UI_AMOUNT]);
  const pausable = ext[EXT.PAUSABLE];
  const hook = ext[EXT.TRANSFER_HOOK];
  const hookBytes = hook ? hook.subarray(32, 64) : null;
  const hookProgram = hookBytes && !allZero(hookBytes) ? toBase58(hookBytes) : null;

  return {
    mintAuthority: dv.getUint32(0, true) ? toBase58(bytes.subarray(4, 36)) : null,
    rawSupply: dv.getBigUint64(36, true),
    decimals: bytes[44],
    freezeAuthority: dv.getUint32(46, true) ? toBase58(bytes.subarray(50, 82)) : null,
    permanentDelegate: ext[EXT.PERMANENT_DELEGATE] ? toBase58(ext[EXT.PERMANENT_DELEGATE]) : null,
    // 1 = Initialized, 2 = Frozen. 2 means new holders start frozen.
    defaultAccountState: ext[EXT.DEFAULT_ACCOUNT_STATE] ? ext[EXT.DEFAULT_ACCOUNT_STATE][0] : null,
    pausableAuthority: pausable ? toBase58(pausable.subarray(0, 32)) : null,
    paused: pausable ? pausable[32] === 1 : false,
    transferHookAuthority: hook ? toBase58(hook.subarray(0, 32)) : null,
    transferHookProgram: hookProgram,
    scaledUi: scaled,
    multiplier: activeMultiplier(scaled, nowSeconds),
  };
}

/* SPL token account: mint(32) owner(32) amount(u64) ... state at 108 */
export function decodeTokenAccount(bytes) {
  const dv = view(bytes);
  return {
    mint: toBase58(bytes.subarray(0, 32)),
    owner: toBase58(bytes.subarray(32, 64)),
    rawAmount: dv.getBigUint64(64, true),
    // 0 uninitialized, 1 initialized, 2 FROZEN
    state: bytes[108],
    frozen: bytes[108] === 2,
  };
}

/* Raw base units -> a decimal string, exactly, no float rounding. */
export function rawToDecimalString(raw, decimals) {
  const s = raw.toString().padStart(decimals + 1, '0');
  const whole = s.slice(0, s.length - decimals);
  const frac = decimals ? '.' + s.slice(s.length - decimals) : '';
  return whole + frac;
}

/* UI amount = raw / 10^decimals * multiplier. Float is fine for display only. */
export function uiAmount(raw, decimals, multiplier = 1) {
  return Number(rawToDecimalString(raw, decimals)) * multiplier;
}
