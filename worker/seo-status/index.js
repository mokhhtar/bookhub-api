/**
 * litheca-seo-status — the indexing dashboard, and the only place its data
 * lives.
 *
 * WHY NOT A PAGE ON THE SITE. Three reasons, and privacy is the weakest of
 * them. litheca.com is served by GitHub Pages from a PUBLIC repo, so a
 * `_data/*.yml` snapshot would be world-readable whether or not a page
 * rendered it, and GitHub Pages supports no authentication at all — a
 * subdomain there would have been a costume, not a lock. The other two
 * reasons stand even if the data were fully public: every refresh would be a
 * commit that rebuilds and redeploys the entire site for an internal table,
 * and it would bury the site's history (already busy with the publishing bot)
 * under weekly operational noise.
 *
 * WHAT REFUSES TO HAPPEN HERE
 *
 * 1. The dashboard will not render without Cloudflare Access in front of it.
 *    GET / requires the Cf-Access-Jwt-Assertion header that Access injects; no
 *    header, no page. This is deliberate and is the same rule the games Worker
 *    applies to TURNSTILE_SECRET: a guard that quietly switches itself off
 *    when unconfigured is not a guard. If Access is removed or misconfigured
 *    later, this fails closed and shows nothing.
 *
 * 2. workers.dev is disabled in wrangler.toml. Access protects a hostname on
 *    the zone, so a live *.workers.dev URL would be an unprotected back door
 *    into the same script — including one where the Access header could simply
 *    be forged, since nothing would be stripping it.
 *
 * 3. /ingest will not accept a write without the shared secret, and answers
 *    503 rather than 200 when no secret is configured, so a half-finished
 *    deploy cannot silently accept anonymous writes.
 */

const KEY = "latest";

// Access sets this on every request it lets through, and strips any copy the
// client tried to send. Presence is a backstop, not the gate — the gate is
// Access itself, upstream. Verifying the JWT signature here would add key
// fetching and rotation for no gain while requests can only arrive through
// the protected hostname (see note 2 above).
const ACCESS_HEADER = "Cf-Access-Jwt-Assertion";

const esc = (s) =>
  String(s == null ? "" : s)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");

// The states, in the order a reader should think about them: what worked,
// what is queued, what was rejected, what is broken. Any state Google returns
// that is not listed keeps its own name and lands in "other" — a state we
// have not seen before must not be silently folded into one we have.
const BUCKETS = [
  { key: "indexed",   test: (s) => /submitted and indexed|^indexed/i.test(s),
    label: "Indexed", hint: "Live in Google.", tone: "good" },
  { key: "discovered", test: (s) => /discovered/i.test(s),
    label: "Discovered, not indexed", tone: "wait",
    hint: "Google knows the URL and has not crawled it yet. This is a queue, not a verdict — on a young domain it is the normal state and needs patience, not action." },
  { key: "crawled",   test: (s) => /crawled/i.test(s),
    label: "Crawled, not indexed", tone: "work",
    hint: "Google fetched the page and chose not to index it. Re-requesting does not change a judgement about value — this is the bucket that means improve the page." },
  { key: "unknown",   test: (s) => /unknown to google/i.test(s),
    label: "Unknown to Google", tone: "wait",
    hint: "Never seen. Expected for a URL added to the sitemap in the last few days." },
  { key: "excluded",  test: (s) => /excluded|noindex|redirect|duplicate|canonical/i.test(s),
    label: "Excluded", tone: "work",
    hint: "Blocked by a tag, a redirect, or a canonical pointing elsewhere. Usually ours to fix." },
];

const bucketOf = (state) =>
  BUCKETS.find((b) => b.test(state || ""))?.key || "other";

function page(snap) {
  const pages = snap?.pages || [];
  const counts = {};
  for (const p of pages) {
    const k = p.error ? "error" : bucketOf(p.coverage);
    counts[k] = (counts[k] || 0) + 1;
  }

  const cards = [...BUCKETS, { key: "other", label: "Other", tone: "wait", hint: "A state not seen before — read the table." },
                             { key: "error", label: "Errors", tone: "work", hint: "The inspection call itself failed." }]
    .filter((b) => counts[b.key])
    .map((b) => `<div class="card ${b.tone}">
        <div class="n">${counts[b.key]}</div>
        <div class="l">${esc(b.label)}</div>
        <p>${esc(b.hint)}</p>
      </div>`).join("");

  const rows = pages.map((p) => {
    const state = p.error ? `ERROR: ${p.error}` : (p.coverage || "—");
    const b = p.error ? "error" : bucketOf(p.coverage);
    const path = String(p.url || "").replace(/^https?:\/\/[^/]+/, "") || "/";
    const crawl = p.last_crawl ? String(p.last_crawl).slice(0, 10) : "never";
    // A Google canonical that disagrees with ours is the quiet cause of a page
    // never appearing, so it gets its own column rather than a detail view.
    const canon = p.google_canonical && p.user_canonical
      && p.google_canonical !== p.user_canonical
      ? `<span class="warn" title="${esc(p.google_canonical)}">differs</span>` : "";
    return `<tr class="b-${b}">
      <td><a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(path)}</a></td>
      <td>${esc(state)}</td><td>${esc(crawl)}</td><td>${canon}</td></tr>`;
  }).join("");

  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Indexing status — Litheca</title><style>
:root{--bg:#fbfaf8;--fg:#1d211e;--mut:#5b625c;--line:#e2e0da;--good:#2f6b4f;--work:#a3342a;--wait:#8a6d3b;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#141715;--fg:#e8e9e4;--mut:#a8aea3;--line:#2c302d;--card:#1b1f1c}}
*{box-sizing:border-box}body{margin:0;padding:28px 20px 60px;background:var(--bg);color:var(--fg);
font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1000px;margin:0 auto}h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--mut);font-size:13px;margin:0 0 24px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:28px}
.card{background:var(--card);border:1px solid var(--line);border-radius:3px;padding:14px 16px}
.card .n{font-size:30px;font-weight:650;line-height:1.1}
.card .l{font-weight:600;font-size:13px;margin-bottom:6px}
.card p{margin:0;font-size:12px;color:var(--mut)}
.card.good .n{color:var(--good)}.card.work .n{color:var(--work)}.card.wait .n{color:var(--wait)}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:3px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:13px;min-width:620px}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--mut)}
tr:last-child td{border-bottom:0}
td a{color:inherit;text-decoration:none;border-bottom:1px solid var(--line)}
td a:hover{border-bottom-color:currentColor}
.b-crawled td:nth-child(2),.b-excluded td:nth-child(2),.b-error td:nth-child(2){color:var(--work);font-weight:600}
.b-indexed td:nth-child(2){color:var(--good)}
.warn{color:var(--work);font-weight:600}
footer{margin-top:26px;font-size:12px;color:var(--mut)}
</style></head><body><div class="wrap">
<h1>Indexing status</h1>
<p class="sub">${esc(snap?.site || "—")} · ${pages.length} sitemap URL(s) ·
checked ${esc(snap?.checked_at ? String(snap.checked_at).replace("T", " ").slice(0, 16) + " UTC" : "—")}</p>
<div class="cards">${cards || '<div class="card wait"><div class="n">0</div><div class="l">No data yet</div><p>Run the “Indexing status” workflow.</p></div>'}</div>
<div class="scroll"><table><thead><tr><th>Page</th><th>State</th><th>Last crawl</th><th>Canonical</th></tr></thead>
<tbody>${rows || '<tr><td colspan="4">Nothing recorded yet.</td></tr>'}</tbody></table></div>
<footer>Self-reported by Google’s URL Inspection API. There is no supported way to
request indexing for these pages — the Indexing API covers only JobPosting and
BroadcastEvent — so “Crawled, not indexed” is answered by improving the page,
never by asking again.</footer>
</div></body></html>`;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/ingest" && request.method === "POST") {
      if (!env.INGEST_SECRET) {
        return new Response("ingest not configured", { status: 503 });
      }
      const auth = request.headers.get("Authorization") || "";
      if (auth !== `Bearer ${env.INGEST_SECRET}`) {
        return new Response("forbidden", { status: 403 });
      }
      let body;
      try {
        body = await request.json();
      } catch (e) {
        return new Response("bad json", { status: 400 });
      }
      if (!Array.isArray(body?.pages)) {
        return new Response("expected {pages:[...]}", { status: 400 });
      }
      body.checked_at = new Date().toISOString();
      await env.SEO.put(KEY, JSON.stringify(body));
      return Response.json({ ok: true, pages: body.pages.length });
    }

    if (url.pathname === "/" && request.method === "GET") {
      if (!request.headers.get(ACCESS_HEADER)) {
        return new Response(
          "This dashboard is served only behind Cloudflare Access.\n" +
          "No Access assertion was present on this request, so nothing is shown.\n",
          { status: 403, headers: { "Content-Type": "text/plain; charset=utf-8" } },
        );
      }
      const raw = await env.SEO.get(KEY);
      let snap = null;
      try {
        snap = raw ? JSON.parse(raw) : null;
      } catch (e) {
        snap = null;
      }
      return new Response(page(snap), {
        headers: {
          "Content-Type": "text/html; charset=utf-8",
          "Cache-Control": "no-store",
          "X-Robots-Tag": "noindex, nofollow",
        },
      });
    }

    return new Response("not found", { status: 404 });
  },
};
