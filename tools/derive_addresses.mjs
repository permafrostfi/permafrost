#!/usr/bin/env node
/* Derive every fixed Permafrost address from the program id and write
 * site/js/addresses.json, which the site loads.
 *
 * Run once per deploy target. The mainnet program keypair is generated fresh
 * at deploy time, so the mainnet id is not known until then.
 *
 *   node tools/derive_addresses.mjs <PROGRAM_ID> [--devnet] [--out site/js/addresses.json]
 *
 * Nothing here is guessed: the seeds are the program's own (lib.rs, v3).
 * --devnet uses the four devnet replica mints (the `devnet` build's LEG_MINTS).
 */
import { writeFileSync } from 'node:fs';
import { PublicKey } from '@solana/web3.js';

const TOKEN_2022 = new PublicKey('TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb');
const ATA_PROGRAM = new PublicKey('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL');

/* FEE_OWNER in lib.rs: fees go to this wallet's PAIF account, fixed in code. */
const FEE_OWNER = new PublicKey('E3FTvAEzUU8v91cFCbykxQzcqRo7Zc2Jp3HxtwTZmYyt');

const DEVNET_MINTS = [
  'A15QnvzoNKst2X2aVaQANvr48kzKVRyYHzP6DXHjppFz',
  '5dy1EDc71EC9tsJzbbY78ZTh1vdhGMeXQNyPiUKtpGzn',
  'JCs5xPjRmSBHtYKAWB6PviCAE5TUMPV4UibbKkPpRMGP',
  'DRcvvyhjF7chPrLbUyoX8DG67j1HYix2WKkHEaHUHHB9',
];

const LEG_MINTS = [
  ['nvdax', 'Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh'],
  ['googlx', 'XsCPL9dNWBMvFtTmwcCA5v3xWPSMEBCszbQdiLLq6aN'],
  ['msftx', 'XspzcW1PRtgf6Wj92HCiZdjzKCyFekVD8P5Ueh3dRMX'],
  ['metax', 'Xsa62P5mvPszXL1krVUnU5ar38bBSVcWAB6fmPCo5Zu'],
];

const pda = (seeds, programId) =>
  PublicKey.findProgramAddressSync(seeds.map((s) => Buffer.from(s)), programId)[0];

const ata = (owner, mint) =>
  PublicKey.findProgramAddressSync(
    [owner.toBuffer(), TOKEN_2022.toBuffer(), mint.toBuffer()],
    ATA_PROGRAM,
  )[0];

export function derive(programIdStr, { devnet = false } = {}) {
  const programId = new PublicKey(programIdStr);
  const vault = pda(['vault'], programId);
  const unitMint = pda(['unit_mint'], programId);
  const deadOwner = pda(['dead'], programId);
  const mints = devnet ? DEVNET_MINTS : LEG_MINTS.map(([, m]) => m);

  return {
    network: devnet ? 'devnet' : 'mainnet',
    programId: programId.toBase58(),
    vault: vault.toBase58(),
    unitMint: unitMint.toBase58(),
    deadOwner: deadOwner.toBase58(),
    deadUnits: ata(deadOwner, unitMint).toBase58(),
    vaultLegs: mints.map((mint) => ata(vault, new PublicKey(mint)).toBase58()),
    legSymbols: LEG_MINTS.map(([symbol]) => symbol),
    ...(devnet ? { legMints: mints } : {}),
    // v3
    pool: pda(['pool'], programId).toBase58(),
    escrow: pda(['escrow'], programId).toBase58(),
    feeOwner: FEE_OWNER.toBase58(),
    feeAccount: ata(FEE_OWNER, unitMint).toBase58(),
    tokenProgram: TOKEN_2022.toBase58(),
  };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const programId = process.argv[2];
  if (!programId) {
    console.error('usage: node tools/derive_addresses.mjs <PROGRAM_ID> [--devnet] [--out path]');
    process.exit(1);
  }
  const outIdx = process.argv.indexOf('--out');
  const out = outIdx > 0 ? process.argv[outIdx + 1] : 'site/js/addresses.json';
  const addresses = derive(programId, { devnet: process.argv.includes('--devnet') });
  writeFileSync(out, JSON.stringify(addresses, null, 2) + '\n');
  console.log(JSON.stringify(addresses, null, 2));
  console.error(`\nwritten to ${out}`);
}
