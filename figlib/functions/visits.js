// Cloudflare Pages Function: /api/visits
// Counts page views and first-time visitors in a D1 table (binding DB, see deploy/cf-counter.py).
// A first visit sets a one-year cookie, so "visitors" counts browsers, not requests.
// Without the binding the endpoint answers {} and the page shows nothing.
const HEADERS = { "content-type": "application/json", "cache-control": "no-store" };

async function read(env) {
  const rows = await env.DB.prepare("SELECT k, n FROM counters").all();
  return Object.fromEntries(rows.results.map(r => [r.k, r.n]));
}

export async function onRequestGet({ env }) {
  if (!env.DB) return new Response("{}", { headers: HEADERS });
  return new Response(JSON.stringify(await read(env)), { headers: HEADERS });
}

export async function onRequestPost({ request, env }) {
  if (!env.DB) return new Response("{}", { headers: HEADERS });
  const seen = /(^|;\s*)pf_v=1(;|$)/.test(request.headers.get("cookie") || "");
  const ops = [env.DB.prepare("UPDATE counters SET n = n + 1 WHERE k = 'views'")];
  if (!seen) ops.push(env.DB.prepare("UPDATE counters SET n = n + 1 WHERE k = 'visitors'"));
  await env.DB.batch(ops);
  const h = { ...HEADERS };
  if (!seen) h["set-cookie"] = "pf_v=1; Max-Age=31536000; Path=/; SameSite=Lax; Secure";
  return new Response(JSON.stringify(await read(env)), { headers: h });
}
