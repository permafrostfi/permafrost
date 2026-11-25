/* Read-only RPC proxy, so the Helius URL (with its key) never reaches the
 * browser. Only the methods the site uses are forwarded. Transactions are sent
 * by the gas service (as Jito bundles) or by Phantom, never through here. */
const ALLOWED = new Set([
  'getMultipleAccounts', 'getAccountInfo', 'getTokenAccountsByOwner', 'getProgramAccounts',
  'getLatestBlockhash', 'getSignatureStatuses', 'getBalance', 'getSlot', 'simulateTransaction',
  'getMinimumBalanceForRentExemption', 'getTokenSupply',
]);

export default async function handler(req, res) {
  if (req.method !== 'POST') return res.status(405).json({ error: 'POST' });
  if (!process.env.RPC_URL) return res.status(503).json({ error: 'RPC_URL is not set on the server' });
  const body = typeof req.body === 'string' ? JSON.parse(req.body) : req.body;
  const calls = Array.isArray(body) ? body : [body];
  if (calls.length > 10 || calls.some((c) => !ALLOWED.has(c?.method))) {
    return res.status(400).json({ error: 'method not allowed' });
  }
  let r;
  try {
    r = await fetch(process.env.RPC_URL, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  } catch (e) {
    return res.status(502).json({ error: `upstream RPC: ${e.message}` });
  }
  res.status(r.status).setHeader('content-type', 'application/json');
  return res.send(await r.text());
}
