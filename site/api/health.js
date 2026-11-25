/* Deployment check: which settings are present (never their values). */
export default function handler(req, res) {
  res.status(200).json({
    env: Object.fromEntries(['RPC_URL', 'RELAYER_SECRET_KEY', 'PROGRAM_ID'].map((k) => [k, Boolean(process.env[k])])),
  });
}
