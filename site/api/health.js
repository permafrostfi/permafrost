/* Deployment check: which settings are present (never their values) and
 * whether the gas service's modules load. */
export default async function handler(req, res) {
  const out = {
    env: Object.fromEntries(['RPC_URL', 'RELAYER_SECRET_KEY', 'PROGRAM_ID'].map((k) => [k, Boolean(process.env[k])])),
    modules: {},
  };
  for (const m of ['@solana/web3.js', 'bs58', '@noble/curves/ed25519', './_lib/relay-core.js', '../js/t22.js']) {
    try { await import(m); out.modules[m] = 'ok'; } catch (e) { out.modules[m] = String(e.message || e).slice(0, 200); }
  }
  res.status(200).json(out);
}
