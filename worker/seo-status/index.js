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

// A SECOND, INDEPENDENT FEED. The indexing snapshot answers "what did Google
// do with this URL"; it cannot answer "is the page any good", and every bug
// worth catching lately was the second question — a book page linking nine
// characters whose pages were never published, three pages offering
// "5, 28, 47, 68…" under a heading that says Chapters. Written by the site
// repo's page-integrity workflow from the files themselves.
//
// Kept as its own key rather than merged into `latest` because the two are
// produced on different schedules by different jobs, and a merge would mean
// whichever ran last silently deciding how fresh the other half looked.
// Written via POST /ingest?feed=pages — see the FEEDS note in fetch() for why
// the path is shared rather than /ingest/pages.
const KEY_PAGES = "pages";

const TABS = [
  { id: "indexing", label: "Indexing" },
  { id: "pages", label: "Pages" },
  { id: "errors", label: "Errors" },
];

// The integrity problems, worst first. `fix` says what actually resolves it,
// because a dashboard that only names a fault trains you to scroll past it.
const ISSUES = {
  dead_character_link: {
    label: "Dead character link", tone: "work",
    fix: "The book page links a character page that was never published. Viewing the book republishes both.",
  },
  junk_chapters: {
    label: "Chapter list is not chapters", tone: "work",
    fix: "An Open Library table of contents holding page numbers or a volume note. The resolver rejects these now; a page written before that has to be emptied by hand.",
  },
  stale_version: {
    label: "Old page format", tone: "wait",
    fix: "Rewritten automatically the next time the book is viewed.",
  },
  empty_page: {
    label: "No chapters, characters or quotes", tone: "wait",
    fix: "Often correct — plenty of books genuinely have none. Worth a look only if the book obviously should.",
  },
  no_cover: {
    label: "No cover", tone: "wait",
    fix: "No cover was found at any provider. Never substitute one from another edition.",
  },
};

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

function indexingTab(snap) {
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

  return `<p class="sub">${esc(snap?.site || "—")} · ${pages.length} sitemap URL(s) ·
checked ${esc(snap?.checked_at ? String(snap.checked_at).replace("T", " ").slice(0, 16) + " UTC" : "—")}</p>
<div class="cards">${cards || '<div class="card wait"><div class="n">0</div><div class="l">No data yet</div><p>Run the “Indexing status” workflow.</p></div>'}</div>
<div class="scroll"><table><thead><tr><th>Page</th><th>State</th><th>Last crawl</th><th>Canonical</th></tr></thead>
<tbody>${rows || '<tr><td colspan="4">Nothing recorded yet.</td></tr>'}</tbody></table></div>
<footer>Self-reported by Google’s URL Inspection API. There is no supported way to
request indexing for these pages — the Indexing API covers only JobPosting and
BroadcastEvent — so “Crawled, not indexed” is answered by improving the page,
never by asking again.</footer>`;
}

// The filters are LINKS, not script. The whole dashboard is server-rendered,
// and adding a first line of JavaScript for four toggles would be a new
// pattern to keep working for no gain at two hundred rows.
const FILTERS = [
  { id: "", label: "All", test: () => true },
  { id: "missing", label: "Missing chapters or characters",
    test: (b) => !b.chapters || !b.characters },
  { id: "nochars", label: "No characters", test: (b) => !b.characters },
  { id: "nochapters", label: "No chapters", test: (b) => !b.chapters },
];

function pagesTab(snap, filterId) {
  const books = snap?.books || [];
  if (!books.length) {
    return `<p class="sub">Nothing recorded yet. Run the “Page integrity” workflow in the site repo.</p>`;
  }
  const f = FILTERS.find((x) => x.id === filterId) || FILTERS[0];
  const shown = books.filter(f.test);
  const missing = books.filter((b) => !b.chapters || !b.characters).length;

  const chips = FILTERS.map((x) => {
    const n = books.filter(x.test).length;
    const on = x.id === f.id ? " on" : "";
    const q = x.id ? `?tab=pages&amp;f=${x.id}` : "?tab=pages";
    return `<a class="chip${on}" href="${q}">${esc(x.label)} <b>${n}</b></a>`;
  }).join("");

  const rows = shown.map((b) => {
    const cell = (n) => (n ? String(n) : `<span class="warn">0</span>`);
    return `<tr>
      <td><a href="https://litheca.com/summary/${esc(b.slug)}/" target="_blank" rel="noopener">${esc(b.title || b.slug)}</a></td>
      <td class="mut">${esc(b.author || "")}</td>
      <td>${cell(b.chapters)}</td><td>${cell(b.characters)}</td>
      <td>${b.quotes || "—"}</td><td>${b.free_ebook ? "yes" : "—"}</td>
      <td class="mut">v${esc(b.version)}</td></tr>`;
  }).join("");

  return `<p class="sub">${books.length} published book page(s) · ${esc(snap.character_pages)} character page(s) ·
scanned ${esc(String(snap.generated_at || "").replace("T", " ").slice(0, 16))} UTC</p>
<div class="cards">
  <div class="card ${missing ? "wait" : "good"}"><div class="n">${missing}</div>
    <div class="l">Missing chapters or characters</div>
    <p>Repaired as the refresh sweep reaches them — eight pages a day, so this should fall steadily.</p></div>
  <div class="card good"><div class="n">${books.length - missing}</div>
    <div class="l">Complete</div><p>Carrying both a chapter list and a cast.</p></div>
</div>
<div class="chips">${chips}</div>
<div class="scroll"><table><thead><tr><th>Book</th><th>Author</th><th>Chapters</th>
<th>Characters</th><th>Quotes</th><th>Free ebook</th><th>Format</th></tr></thead>
<tbody>${rows || '<tr><td colspan="7">Nothing matches this filter.</td></tr>'}</tbody></table></div>
<footer>Read from the committed <code>_books/*.md</code>, so this is what a reader
and Google actually get — not what the API would answer if asked today.</footer>`;
}

function errorsTab(snap) {
  const books = snap?.books || [];
  if (!books.length) {
    return `<p class="sub">Nothing recorded yet. Run the “Page integrity” workflow in the site repo.</p>`;
  }
  const groups = new Map();
  for (const b of books) {
    for (const i of b.issues || []) {
      if (!groups.has(i.kind)) groups.set(i.kind, []);
      groups.get(i.kind).push({ b, detail: i.detail });
    }
  }
  // Known kinds first, in the order ISSUES declares; anything the scanner
  // starts reporting that this Worker has never heard of still gets shown,
  // under its own name, rather than being dropped for being unrecognised.
  const order = Object.keys(ISSUES).filter((k) => groups.has(k))
    .concat([...groups.keys()].filter((k) => !ISSUES[k]));

  if (!order.length) {
    return `<p class="sub">No integrity problems across ${books.length} pages.</p>`;
  }

  const sections = order.map((kind) => {
    const meta = ISSUES[kind] || { label: kind, tone: "work", fix: "" };
    const hits = groups.get(kind);
    const rows = hits.map(({ b, detail }) => `<tr>
      <td><a href="https://litheca.com/summary/${esc(b.slug)}/" target="_blank" rel="noopener">${esc(b.title || b.slug)}</a></td>
      <td class="mut">${esc(detail || "")}</td></tr>`).join("");
    return `<section class="issue">
      <h2 class="${meta.tone}">${esc(meta.label)} <span class="count">${hits.length}</span></h2>
      <p class="fix">${esc(meta.fix)}</p>
      <div class="scroll"><table><tbody>${rows}</tbody></table></div>
    </section>`;
  }).join("");

  const total = [...groups.values()].reduce((n, v) => n + v.length, 0);
  return `<p class="sub">${total} issue(s) across ${books.length} pages ·
scanned ${esc(String(snap.generated_at || "").replace("T", " ").slice(0, 16))} UTC</p>
${sections}
<footer>Only shapes that are wrong however you look at them. An empty chapter
list is not one — plenty of books genuinely have none — so it is reported as a
fact on the Pages tab and reaches this tab only when the page has nothing at
all.</footer>`;
}

function page(snap, psnap, tab, filterId) {
  const active = TABS.find((t) => t.id === tab) ? tab : "indexing";
  const nErr = (psnap?.books || []).reduce((n, b) => n + (b.issues?.length || 0), 0);
  const nav = TABS.map((t) => {
    const badge = t.id === "errors" && nErr ? ` <b>${nErr}</b>` : "";
    return `<a class="tab${t.id === active ? " on" : ""}" href="?tab=${t.id}">${esc(t.label)}${badge}</a>`;
  }).join("");

  const body = active === "pages" ? pagesTab(psnap, filterId)
    : active === "errors" ? errorsTab(psnap)
    : indexingTab(snap);

  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Site status — Litheca</title><style>
:root{--bg:#fbfaf8;--fg:#1d211e;--mut:#5b625c;--line:#e2e0da;--good:#2f6b4f;--work:#a3342a;--wait:#8a6d3b;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#141715;--fg:#e8e9e4;--mut:#a8aea3;--line:#2c302d;--card:#1b1f1c}}
*{box-sizing:border-box}body{margin:0;padding:28px 20px 60px;background:var(--bg);color:var(--fg);
font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1000px;margin:0 auto}h1{font-size:22px;margin:0 0 12px}
.sub{color:var(--mut);font-size:13px;margin:0 0 24px}
.tabs{display:flex;gap:2px;border-bottom:1px solid var(--line);margin-bottom:20px;flex-wrap:wrap}
.tab{padding:8px 14px;font-size:13px;font-weight:600;color:var(--mut);text-decoration:none;
border:1px solid transparent;border-bottom:0;border-radius:3px 3px 0 0;margin-bottom:-1px}
.tab:hover{color:var(--fg)}
.tab.on{color:var(--fg);background:var(--card);border-color:var(--line);border-bottom:1px solid var(--card)}
.tab b{font-weight:700;color:var(--work)}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.chip{font-size:12px;padding:5px 11px;border:1px solid var(--line);border-radius:999px;
color:var(--mut);text-decoration:none;background:var(--card)}
.chip:hover{color:var(--fg)}.chip.on{color:var(--fg);border-color:currentColor;font-weight:600}
.chip b{font-weight:700}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:22px}
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
td.mut{color:var(--mut);white-space:normal}
.b-crawled td:nth-child(2),.b-excluded td:nth-child(2),.b-error td:nth-child(2){color:var(--work);font-weight:600}
.b-indexed td:nth-child(2){color:var(--good)}
.warn{color:var(--work);font-weight:600}
.issue{margin-bottom:26px}
.issue h2{font-size:14px;margin:0 0 4px;display:flex;align-items:center;gap:8px}
.issue h2.work{color:var(--work)}.issue h2.wait{color:var(--wait)}
.issue h2 .count{font-size:12px;color:var(--mut);font-weight:600}
.issue .fix{margin:0 0 10px;font-size:12px;color:var(--mut);max-width:70ch}
footer{margin-top:26px;font-size:12px;color:var(--mut);max-width:75ch}
code{font-size:12px}
</style></head><body><div class="wrap">
<h1>Site status</h1>
<nav class="tabs">${nav}</nav>
${body}
</div></body></html>`;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // ONE PATH, TWO FEEDS, selected by ?feed=. Not /ingest/pages, which would
    // have read better: /ingest is reached through a path-scoped Cloudflare
    // Access application carrying a Bypass policy, and whether that rule
    // covers a deeper path is console state no file here records. A query
    // parameter cannot fall outside it, so the second feed needs no Access
    // change and cannot be broken by one.
    //
    // Still an explicit selector rather than sniffing the body: a caller
    // naming a feed this Worker does not know is refused, instead of being
    // quietly treated as the other one and overwriting its key.
    const FEEDS = {
      indexing: { key: KEY, field: "pages" },
      pages: { key: KEY_PAGES, field: "books" },
    };
    const ingest = url.pathname === "/ingest"
      ? FEEDS[url.searchParams.get("feed") || "indexing"] || "unknown"
      : null;

    if (ingest === "unknown") {
      return new Response(`unknown feed; expected one of ${Object.keys(FEEDS).join(", ")}`,
                          { status: 400 });
    }

    if (ingest && request.method === "POST") {
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
      if (!Array.isArray(body?.[ingest.field])) {
        return new Response(`expected {${ingest.field}:[...]}`, { status: 400 });
      }
      body.checked_at = new Date().toISOString();
      await env.SEO.put(ingest.key, JSON.stringify(body));
      return Response.json({ ok: true, [ingest.field]: body[ingest.field].length });
    }

    if (url.pathname === "/" && request.method === "GET") {
      if (!request.headers.get(ACCESS_HEADER)) {
        return new Response(
          "This dashboard is served only behind Cloudflare Access.\n" +
          "No Access assertion was present on this request, so nothing is shown.\n",
          { status: 403, headers: { "Content-Type": "text/plain; charset=utf-8" } },
        );
      }
      // One unreadable feed must not blank the other: each is parsed on its
      // own and a failure leaves that tab empty, not the dashboard.
      const read = async (k) => {
        try {
          const raw = await env.SEO.get(k);
          return raw ? JSON.parse(raw) : null;
        } catch (e) {
          return null;
        }
      };
      const [snap, psnap] = await Promise.all([read(KEY), read(KEY_PAGES)]);
      return new Response(page(snap, psnap, url.searchParams.get("tab"),
                               url.searchParams.get("f") || ""), {
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
