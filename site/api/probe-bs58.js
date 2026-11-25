import * as m from 'bs58';
export default function handler(req, res) { res.status(200).json({ ok: true, keys: Object.keys(m).slice(0, 5) }); }
