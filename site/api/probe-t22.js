import * as m from '../js/t22.js';
export default function handler(req, res) { res.status(200).json({ ok: true, keys: Object.keys(m).slice(0, 5) }); }
