/* Vercel cron (vercel.json): settle complete / cancel expired orders the gas
 * service funded, so a user who closed the page still gets settled or refunded. */
import order from './order.js';

export const config = { maxDuration: 60 };

export default async function handler(req, res) {
  return order({ method: 'GET', query: { sweep: '1' } }, res);
}
