/**
 * litheca-akinator-admin — the Book Mind Reader admin page.
 *
 * SAME SHAPE AS worker/seo-status/, deliberately. Cloudflare Access sits in
 * front of the custom domain (owner-configured in the dashboard, not here);
 * this Worker checks the Access-injected header as a backstop only, never
 * validates the JWT itself (see worker/seo-status/index.js's own note on
 * why that verification would add key rotation for no real gain), and
 * workers.dev is disabled so there is no unprotected back door.
 *
 * WHAT THIS WORKER DOES NOT DO: write to GitHub. It holds no GITHUB_PAT.
 * Every /api/* route is a thin, Access-gated relay to bookhub-api's
 * /akinator/admin/* endpoints, authenticated on that hop by its OWN secret
 * (ADMIN_SECRET here, AKINATOR_ADMIN_SECRET on Render) — Access
 * authenticates the browser, not this server-to-server call, so that call
 * needs its own credential regardless of who is signed in. Python stays the
 * sole committer to the bookhub repo, exactly as it is today for
 * /akinator/sync and /akinator/drain.
 */

const ACCESS_HEADER = "Cf-Access-Jwt-Assertion";

// The Render service this project is deployed to (see CLAUDE.md). Hardcoded
// like the games Worker hardcodes litheca.com for CORS — this almost never
// changes, and an env var here would be one more thing to keep in sync
// with no real flexibility gained.
const API_BASE = "https://bookhub-api-hnv7.onrender.com";

// Escaping happens in the BROWSER here, not in the Worker — unlike
// worker/seo-status/, whose tables are rendered server-side from a KV
// snapshot. This page's rows come from books.json/questions.json fetched
// by the client, so the helper has to live in the client script (below);
// a Worker-scope copy would be invisible to it. Kept as source text so
// there is exactly one definition and no chance of the two drifting.
const ESC_FN = `const esc = (s) => String(s == null ? "" : s)
  .replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;")
  .replaceAll('"',"&quot;").replaceAll("'","&#39;");`;

function relay(path) {
  return async (request, env) => {
    if (!env.ADMIN_SECRET) {
      return new Response("admin write not configured", { status: 503 });
    }
    let body;
    try {
      body = await request.text();
    } catch (e) {
      return new Response("bad body", { status: 400 });
    }
    const upstream = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Admin-Secret": env.ADMIN_SECRET,
      },
      body,
    });
    const text = await upstream.text();
    return new Response(text, {
      status: upstream.status,
      headers: { "Content-Type": "application/json" },
    });
  };
}

// GET, not POST, and it needs no ADMIN_SECRET — book_data.search_books_list()
// is already public (GET /search in tools/summary.py, the site's own book
// search). This exists ONLY so the browser calls the SAME origin as every
// other relay: fetching bookhub-api-hnv7.onrender.com directly from
// mindreader-admin.litheca.com would need that host added to the API's own
// CORS allow-list, and adding a page whose whole purpose is not being
// public to a CORS allow-list is exactly backwards. The Worker fetches
// server-to-server instead, where CORS does not apply at all.
//
// Still a POST from the CLIENT's side (body: {q, offset}), so it fits the
// same relay() shape as everything else and the deploy workflow's guard
// loop (POST … -d '{}') exercises it exactly like every other route — a
// GET-shaped route here would need its own carve-out in that loop, and
// that carve-out is exactly the kind of "this one relay is different" gap
// that let /display slip through unguarded once already.
function bookSearchRelay(request) {
  return (async () => {
    let q = "", offset = 0;
    try {
      const body = JSON.parse(await request.text() || "{}");
      q = String(body.q || "").slice(0, 200);
      offset = Number.isFinite(body.offset) ? body.offset : 0;
    } catch (e) { /* empty q below */ }
    if (!q.trim()) return new Response("[]", { headers: { "Content-Type": "application/json" } });
    const upstream = await fetch(
      `${API_BASE}/search?q=${encodeURIComponent(q)}&offset=${offset}`);
    const text = await upstream.text();
    return new Response(text, {
      status: upstream.status,
      headers: { "Content-Type": "application/json" },
    });
  })();
}

const ROUTES = {
  "/api/book/search": (request) => bookSearchRelay(request),
  "/api/book/preview": relay("/akinator/admin/book/preview"),
  "/api/exclude": relay("/akinator/admin/exclude"),
  "/api/correction": relay("/akinator/admin/correction"),
  "/api/question": relay("/akinator/admin/question"),
  "/api/book": relay("/akinator/admin/book"),
  // Was missing, and the Rename button had been posting into a 404 since the
  // display override shipped: the endpoint existed on the Python side and the
  // client called it, but this table — the only thing that maps one to the
  // other — never gained the row. Nothing logs a route that does not exist,
  // so it failed silently and only in the browser.
  "/api/display": relay("/akinator/admin/display"),
  // "this book has no known author", set by hand on Add a book and Edit a
  // book. A fact for the next rebuild, not a live matrix clamp — see the
  // note above the endpoint in akinator_admin.py for why absence of an
  // author can never stand in for it.
  "/api/noauthor": relay("/akinator/admin/noauthor"),
  "/api/suggestions": relay("/akinator/admin/suggestions"),
  "/api/suggestions/resolve": relay("/akinator/admin/suggestions/resolve"),
  "/api/suggestions/theme": relay("/akinator/admin/suggestions/theme"),
  "/api/taught": relay("/akinator/admin/taught"),
  "/api/taught/apply": relay("/akinator/admin/taught/apply"),
  // One commit for every reviewed question on the Edit-a-book tab, instead
  // of one per Yes/No/Clear click — see akinator_drain.py's apply_taught_batch.
  "/api/taught/apply_batch": relay("/akinator/admin/taught/apply_batch"),
  // Read-only: every hand-set clamp against what the CURRENT matrix says
  // about the same cell. A clamp outlives the correction that made it
  // unnecessary, and nothing else in this system would ever mention it.
  "/api/taught/audit": relay("/akinator/admin/taught/audit"),
  // Verdicts on scripts/akinator/propose_questions.py's mined candidates —
  // the candidates themselves are read straight off question_candidates.json
  // by the client, same as books.json/questions.json; only the decision
  // needs a write, hence one relay rather than two.
  "/api/candidates/decide": relay("/akinator/admin/candidates/decide"),
  // The review gate between the game's Fandom-wiki discovery and the
  // summarizer's live map — see tools/fandom_admin.py's module docstring.
  // list is a relay rather than a plain litheca.com read because it also
  // needs live_count from wikis.json in the same response, and the two
  // files are hosted from bookhub, not this Worker's own domain.
  // tools/fandom_admin.py's router prefix is /fandom/admin, NOT
  // /akinator/admin/... — this pointed at the wrong path on first write,
  // caught by directly probing the live backend (403 vs 404) rather than
  // trusting the harness test, which stubs the CLIENT's fetch and so never
  // exercises what a relay actually points at. check.mjs cannot catch this
  // class of bug either: it only proves a called path exists somewhere in
  // ROUTES, not that ROUTES' own target matches a route the Python side
  // serves — the same "verify the wiring, not just that wiring exists"
  // lesson as the missing /api/display relay from 2026-08-20.
  "/api/fandom/candidates": relay("/fandom/admin"),
  "/api/fandom/candidates/approve": relay("/fandom/admin/approve"),
  "/api/fandom/candidates/reject": relay("/fandom/admin/reject"),
  // Author identity and hand-set author facts. `resolve` writes nothing —
  // it is the Open Library author search and the single-author Wikidata
  // join — and `save` is the only thing that touches author_overrides.json.
  // The overlay itself is read straight off litheca.com like every other
  // artifact; only the write needs a relay.
  "/api/authors/resolve": relay("/akinator/admin/authors/resolve"),
  "/api/authors/save": relay("/akinator/admin/authors/save"),
  // Says who a book is BY: the display name and the author's known answers
  // instantly, and author_name/author_key into admin_corrections.json so
  // the next rebuild reaches the same conclusion on its own.
  "/api/authors/link": relay("/akinator/admin/authors/link"),
};

function page() {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Book Mind Reader — admin</title><style>
:root{--bg:#fbfaf8;--fg:#1d211e;--mut:#5b625c;--line:#e2e0da;--good:#2f6b4f;--work:#a3342a;--wait:#8a6d3b;--card:#fff;--accent:#2e5c8e}
@media (prefers-color-scheme:dark){:root{--bg:#141715;--fg:#e8e9e4;--mut:#a8aea3;--line:#2c302d;--card:#1b1f1c;--accent:#7fa8d9}}
*{box-sizing:border-box}
body{margin:0;padding:24px 20px 70px;background:var(--bg);color:var(--fg);
  font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:21px;margin:0 0 3px}
.sub{color:var(--mut);font-size:13px;margin:0 0 22px}
nav{display:flex;gap:6px;margin-bottom:18px;border-bottom:1px solid var(--line)}
nav button{background:none;border:none;padding:9px 14px;font:inherit;font-weight:600;
  color:var(--mut);cursor:pointer;border-bottom:2px solid transparent}
nav button.on{color:var(--fg);border-bottom-color:var(--accent)}
section{display:none}section.on{display:block}
input,textarea{font:inherit;background:var(--card);color:var(--fg);border:1px solid var(--line);
  border-radius:3px;padding:6px 9px}
input[type=text],input[type=search],textarea{width:100%}
textarea{resize:vertical;min-height:70px}
button.act{font:inherit;font-weight:600;background:var(--accent);color:#fff;border:none;
  border-radius:3px;padding:6px 12px;cursor:pointer;white-space:nowrap}
button.act.danger{background:var(--work)}
button.act.ghost{background:none;color:var(--accent);border:1px solid var(--accent)}
button.act:disabled{opacity:.5;cursor:default}
.row{display:flex;gap:8px;align-items:center}
.field{margin-bottom:12px}
.field label{display:block;font-size:12px;font-weight:600;color:var(--mut);margin-bottom:4px}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:3px;background:var(--card);margin-top:12px}
table{border-collapse:collapse;width:100%;font-size:13px;min-width:640px}
th,td{text-align:left;padding:8px 11px;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--mut);position:sticky;top:0;background:var(--card)}
tr:last-child td{border-bottom:0}
tr.excluded{opacity:.5}
td.title{white-space:normal;min-width:220px}
/* Question text wraps instead of stretching the row — without this, a
   long question ("Is the author from Asia, Africa, or Latin America?")
   forces the whole row wide under the generic nowrap rule above, and the
   Yes/No/Unknown buttons end up a horizontal scroll away from the question
   they answer. Capped at 320px so a short question does not sprawl either. */
/* THREE SEPARATE FIXES, found empirically one at a time in a real browser —
   each looked sufficient until measured, and was not:
   1. td.q{white-space:normal} makes the question text itself wrap. On its
      own this only fixed that one cell; the ROW stayed 1967px wide because
      the other three columns still claimed unlimited space.
   2. table.qtable{table-layout:fixed} + explicit column percentages caps
      every column — but percentages are meaningless without #3.
   3. The Authors tab embeds this table's whole card inside a bare
      <td colspan="5"> (see auEdit below), which matches the page's generic
      th,td{white-space:nowrap} rule. white-space INHERITS, so every
      descendant — the book-titles summary line, the aliases label, the
      "Attaching writes…" note — was nowrap too. Most of them happened to
      be short enough to fit on one unwrapped line and looked fine, which is
      exactly how this hid until a long paragraph exposed it (see td.auEditCell). */
/* min-width:0 overrides the page's OWN generic table{min-width:640px} rule
   (meant for wide book-list tables). Caught in a real browser on the
   Add-a-book review table specifically: its .card is the default 560px,
   narrower than that 640px floor, so table-layout:fixed;width:100% still
   overflowed — width:100% resolves to ~520px but min-width:640px is a
   hard floor that wins regardless. The Authors/Edit-a-book tables never
   hit this because their containers happen to be wider than 640px; a
   floor that only bites in ONE context is exactly the kind of bug that
   stays hidden until something narrower than every case tested exposes it. */
table.qtable{table-layout:fixed;width:100%;min-width:0}
td.q{white-space:normal}
/* The buttons cell (class="row qa") can genuinely need two lines — Yes /
   No / Unknown / Clear in a ~28%-wide fixed column. .row's flex is nowrap
   everywhere else it is used on this page (search toolbars, the Books
   tab's per-row actions) and stays that way; qa adds wrapping only where
   it is also present, rather than changing what .row means everywhere. */
td.qa{flex-wrap:wrap}
/* Fix #3 above, and the one that actually mattered most: without this, the
   ENTIRE inline author editor — not just its question table — silently
   inherits nowrap from the generic td rule. */
td.auEditCell{white-space:normal}
/* The books/authors LIST table (id=auRows's own <table>) is table-layout:
   auto by default, which lets a colspan=5 cell's content dictate the whole
   table's width — auto layout treats an explicit width as a MINIMUM, not a
   cap, when a cell's content cannot be compressed further. Scoped to this
   one table only: the books table on the other tab must keep sizing to its
   (much simpler) content. */
table.qhost{table-layout:fixed}
/* The identity column holds the one string on this page with no spaces in
   it — name:lord george gordon byron george gordon byron is 48 characters —
   and it inherits nowrap from the generic th,td rule while its column is
   capped at 20% by table-layout:fixed. Measured in a browser: 502px of
   content in a 216px cell, with the "name only" badge printed ON TOP of the
   id rather than after it. Only visible when rendered; the emitted HTML is
   perfectly correct. Fourth instance of this same class of bug on this page
   (see the two notes above) and the same shape every time — an inherited
   nowrap meeting content that finally got long enough to show it.
   The break is scoped to the <code>, not the cell: applied to the cell it
   also broke "id guessed" across two lines mid-label, which is the fix
   making a second, smaller mess of the same kind. Badges never wrap. */
/* A checkbox and its wording on one line, inside a .field whose <label> is
   a block. Without it the box sits on its own row above the text it labels
   and reads as belonging to the field above. */
label.inline{display:flex;align-items:center;gap:7px;margin-top:8px;font-weight:400}
label.inline input{width:auto;margin:0}
td.ident{white-space:normal}
td.ident code{overflow-wrap:anywhere}
.badge{font-size:11px;font-weight:600;padding:1px 7px;border-radius:3px;background:var(--line);white-space:nowrap}
.badge.off{color:var(--work)}
.badge.dup{color:var(--wait);cursor:pointer;border:1px solid transparent}
.badge.dup:hover{border-color:var(--wait)}
td.rich{font-variant-numeric:tabular-nums;text-align:right}
td.rich.thin{color:var(--work);font-weight:600}
.toolbar{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-top:8px;font-size:12px;color:var(--mut)}
.toolbar label{display:flex;gap:5px;align-items:center;cursor:pointer}
.status{font-size:12px;color:var(--mut);margin-top:6px;min-height:1.4em}
.status.ok{color:var(--good)}.status.err{color:var(--work)}
.card{background:var(--card);border:1px solid var(--line);border-radius:3px;padding:16px 18px;max-width:560px}
.effect{font-size:11px;color:var(--mut);margin-top:2px}
footer{margin-top:30px;font-size:12px;color:var(--mut)}
.pill{display:inline-block;min-width:17px;padding:0 5px;border-radius:9px;background:var(--work);
  color:#fff;font-size:11px;line-height:17px;text-align:center;vertical-align:1px}
.pill:empty{display:none}
.sg{background:var(--card);border:1px solid var(--line);border-radius:3px;padding:14px 16px;margin-bottom:10px}
.sg-head{display:flex;gap:9px;align-items:baseline;flex-wrap:wrap;margin-bottom:9px}
.sg-reason{font-weight:600}
.sg-when{color:var(--mut);font-size:12px;margin-left:auto}
.sg-cmp{display:grid;grid-template-columns:74px 1fr;gap:3px 10px;font-size:13px;margin-bottom:11px}
.sg-cmp dt{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.05em;padding-top:2px}
.sg-cmp dd{margin:0}
.sg-new{font-weight:600;color:var(--good)}
.sg-none{color:var(--mut);font-style:italic}
.sg-dupe{font-size:12px;color:var(--wait);margin:0 0 6px;padding:7px 10px;
  border-left:2px solid var(--wait);background:var(--bg)}
.sg-dupe--sure{color:var(--work);border-left-color:var(--work);font-weight:600}
.sg-dupe--clear{color:var(--mut);border-left-color:var(--line)}
.sg-near{margin:0 0 11px;padding:0 0 0 22px;font-size:13px}
.sg-near li{margin-bottom:2px}
.sg-themes{margin-top:6px;font-size:12px;color:var(--mut)}
.sg-stats{border:1px solid var(--line);border-radius:3px;background:var(--card);
  padding:12px 15px;margin-bottom:16px}
.sg-stats summary{cursor:pointer;font-weight:600;font-size:13px}
.sg-stats table{min-width:420px}
.sg-stats .scroll{margin-top:10px}
.sg-over{color:var(--work)}
.cq-draft{background:var(--bg);border:1px solid var(--line);border-radius:3px;padding:9px 11px;
  font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap;
  word-break:break-word;margin:0 0 10px;max-height:220px;overflow:auto}
</style></head><body><div class="wrap">
<h1>Book Mind Reader — admin</h1>
<p class="sub">Every action here commits directly to the live game. Nothing is automatic — nothing applies without you clicking it.</p>

<nav>
  <button data-tab="books" class="on">Books</button>
  <button data-tab="questions">Questions</button>
  <button data-tab="add">Add a book</button>
  <button data-tab="suggestions">Suggestions <span class="pill" id="sgCount"></span></button>
  <button data-tab="taught">Taught <span class="pill" id="tgCount"></span></button>
  <button data-tab="candidates">Mined questions <span class="pill" id="cqCount"></span></button>
  <button data-tab="fandom">Fandom <span class="pill" id="fdCount"></span></button>
  <button data-tab="edit">Edit a book</button>
  <button data-tab="authors">Authors <span class="pill" id="auDupPill"></span></button>
</nav>

<section id="books" class="on">
  <input type="search" id="bookSearch" placeholder="Search by title or author…">
  <div class="toolbar">
    <label><input type="checkbox" id="dupOnly"> Only duplicate candidates (<span id="dupCount">0</span>)</label>
    <select id="bookOrigin" title="How the row was added — only tracked for rows added after this filter shipped; older rows show as Unknown.">
      <option value="">Added via: all</option>
      <option value="resolved">Summarizer (auto-detected)</option>
      <option value="suggestion">Reader suggestion</option>
      <option value="manual">Manual (admin-typed)</option>
      <option value="unknown">Unknown (bulk harvest / pre-existing)</option>
    </select>
    <select id="bookPeriod">
      <option value="">Added: any time</option>
      <option value="1">Today</option>
      <option value="7">Last 7 days</option>
      <option value="30">Last 30 days</option>
    </select>
    <span id="shownCount"></span>
  </div>
  <div class="scroll"><table>
    <thead><tr>
      <th>Title</th><th>Author</th><th>Year</th><th>Rank</th>
      <th title="Richness: how many content subjects the row has. It decides how much an 'absent' answer is worth, so r=0 is a row that can barely answer anything.">r</th>
      <th></th><th></th>
    </tr></thead>
    <tbody id="bookRows"><tr><td colspan="7">Loading…</td></tr></tbody>
  </table></div>
  <p class="status" id="bookStatus"></p>
</section>

<section id="questions">
  <div class="scroll"><table>
    <thead><tr><th>ID</th><th>Wording</th><th></th></tr></thead>
    <tbody id="qRows"><tr><td colspan="3">Loading…</td></tr></tbody>
  </table></div>
  <p class="status" id="qStatus"></p>
</section>

<datalist id="authorNames"></datalist>

<section id="add">
  <p class="sub" style="margin-bottom:14px">Search first — Fandom, Open Library and Google Books, the same
  cascade the site's own book search already runs, not a new one built to guess at this page. Pick a match and
  its title/author/year/summary come from a real source. Nothing found? Create the row by hand below the search.
  Either way, review what the game would say about the book <strong>before</strong> it is added — a suggestion,
  never a silent guess, and Yes/No/Unknown here costs no commit until you press Add book.</p>
  <div class="card">
    <div class="field"><label>Search for the book</label>
      <input type="search" id="addSearch" placeholder="Title, or title and author…"></div>
    <div id="addSearchStatus" class="status"></div>
    <div id="addResults"></div>
    <p class="row" style="margin:10px 0"><button class="act ghost" id="addManual">Nothing matches — create it by hand</button></p>
  </div>
  <div id="addForm"></div>
</section>

<section id="edit">
  <p class="sub" style="margin-bottom:14px">Everything about one book on one screen. Title and author change what the
  reveal PRINTS and take effect as soon as GitHub Pages redeploys. Questions edit a DRAFT — nothing is written
  until Save, which writes every reviewed answer in ONE commit at the same verified bound a catalogued fact gets
  (0.90 / 0.15), then held against the nightly drain until cleared. The year is the exception and says so: it feeds a
  matrix bit only a full rebuild recomputes.</p>
  <input type="search" id="edSearch" placeholder="Find a book by title or author…">
  <div class="scroll"><table class="qhost">
    <colgroup><col style="width:44%"><col style="width:26%"><col style="width:12%"><col style="width:18%"></colgroup>
    <thead><tr><th>Title</th><th>Author</th><th>Year</th><th></th></tr></thead>
    <tbody id="edRows"></tbody>
  </table></div>
  <p class="status" id="edStatus"></p>
</section>

<section id="authors">
  <p class="sub" style="margin-bottom:14px">One author, everything about them. A fact set here applies to
  <strong>every book they wrote</strong>, which is what makes this worth doing and also what makes a wrong one
  expensive. <strong>Nothing on this tab is instant</strong> — facts feed matrix bits and aliases feed the author
  grouping, and only a local <code>build_matrix.py</code> run recomputes either. Merging teaches a name; it never
  rewrites a book count already exported.</p>
  <input type="search" id="auSearch" placeholder="Find an author by name…">
  <div class="toolbar">
    <label><input type="checkbox" id="auDupOnly"> Only possible duplicates (<span id="auDupCount">0</span>)</label>
    <select id="auOrigin" title="Matches an author with at least one book added this way — origin/date are tracked per BOOK, not per author.">
      <option value="">Added via: all</option>
      <option value="resolved">Summarizer (auto-detected)</option>
      <option value="suggestion">Reader suggestion</option>
      <option value="manual">Manual (admin-typed)</option>
      <option value="unknown">Unknown (bulk harvest / pre-existing)</option>
    </select>
    <select id="auPeriod">
      <option value="">Added: any time</option>
      <option value="1">Today</option>
      <option value="7">Last 7 days</option>
      <option value="30">Last 30 days</option>
    </select>
    <span id="auShown"></span>
    <button class="act ghost" id="auNew">New author profile</button>
  </div>
  <div id="auNewBox"></div>
  <div id="auDupes"></div>
  <div class="scroll"><table class="qhost">
    <colgroup><col style="width:32%"><col style="width:20%"><col style="width:10%">
    <col style="width:20%"><col style="width:18%"></colgroup>
    <thead><tr><th>Author</th><th>Identity</th><th>Books</th><th>Overlay</th><th></th></tr></thead>
    <tbody id="auRows"><tr><td colspan="5">Loading…</td></tr></tbody>
  </table></div>
  <p class="status" id="auStatus"></p>
</section>

<section id="taught">
  <p class="sub" style="margin-bottom:14px">What players answered about books they named, waiting on the
  8-play floor. The nightly drain writes nothing below that on purpose — a handful of players must not move a cell.
  <strong>You are not a handful of players.</strong> If you look the book up and decide, that is a different kind of
  act, so it writes the verified bound (0.90 / 0.15) rather than a posterior computed from three answers — and the
  drain then leaves that cell alone until you clear it.</p>
  <div class="row" style="margin-bottom:12px">
    <button class="act ghost" id="tgReload">Refresh</button>
    <span class="status" id="tgStatus" style="margin:0"></span>
  </div>
  <div class="scroll"><table>
    <thead><tr><th>Book</th><th>Question</th><th>Players said</th><th>Table says</th>
    <th title="What tonight's drain would write, if the cell ever clears the 8-play floor.">Drain would</th><th></th></tr></thead>
    <tbody id="tgRows"></tbody>
  </table></div>

  <h2 style="margin-top:30px">Clamps the rebuilt table has caught up with <span class="badge" id="auditPill"></span></h2>
  <p class="sub" style="margin-bottom:14px">Every cell you clamped by hand is held against the drain
  <strong>forever</strong>, until you clear it. So when a rebuild reaches a different conclusion — an author link
  now writes into admin_corrections.json, and book_traits() recomputes from it — the old clamp goes on overriding
  the new answer silently. This is the only place that says so.
  <br><br>A clamp counts as doing nothing only when it equals what the engine would answer <em>without</em> it, and
  for an absent cell that is absence_confidence(richness) — 0.45 on a bare record, 0.15 only on a richly documented
  one — never a flat 0.15. Comparing states instead of values would offer to delete clamps that are actually
  holding a cell, so it compares values. Conflicts are shown and never decided here.</p>
  <div class="row" style="margin-bottom:12px">
    <button class="act ghost" id="adReload">Run the audit</button>
    <button class="act ghost" id="adClear" disabled>Clear what is doing nothing</button>
    <span class="status" id="adStatus" style="margin:0"></span>
  </div>
  <div id="adRows"></div>
</section>

<section id="suggestions">
  <p class="sub" style="margin-bottom:14px">Reported by readers on the give-up screen, and verified against
  Google Books / Open Library before reaching this queue — a book that could not be found never arrives here.
  Nothing a reader typed is stored: the title below is the catalogue's own, looked up from what they typed.
  Approving runs the same action as doing it by hand on the other tabs.</p>
  <div class="row" style="margin-bottom:12px">
    <button class="act ghost" id="sgReload">Refresh</button>
    <select id="sgReason">
      <option value="">All reasons</option>
      <option value="missing">Not in the game (reader)</option>
      <option value="resolved">Found via summarizer</option>
      <option value="wrong_year">Wrong year</option>
      <option value="unreadable">Unreadable title</option>
    </select>
    <select id="sgPeriod">
      <option value="">All time</option>
      <option value="1">Today</option>
      <option value="7">Last 7 days</option>
      <option value="30">Last 30 days</option>
    </select>
    <span class="status" id="sgStatus" style="margin:0"></span>
  </div>
  <div id="sgAsks"></div>
  <div id="sgStats"></div>
  <div id="sgList"></div>
</section>

<section id="candidates">
  <p class="sub" style="margin-bottom:14px">Proposed by <code>scripts/akinator/propose_questions.py</code>, measured
  against the real corpus (or a real sample, for a prose-only candidate) before you ever see it — the frequency and
  any near-duplicate warning below are numbers, not guesses. Accepting only records a verdict here; it never edits
  <code>features.py</code> or <code>traits.py</code> and never triggers a rebuild. Copy the drafted rule and paste it
  in by hand — a bad keyword needs a human eye on the diff before it can silently mislabel 5,000 books.</p>
  <div class="row" style="margin-bottom:12px">
    <button class="act ghost" id="cqReload">Refresh</button>
    <span class="status" id="cqStatus" style="margin:0"></span>
  </div>
  <div id="cqList"></div>
</section>

<section id="fandom">
  <p class="sub" style="margin-bottom:14px">Books the game's own discovery pipeline has PROVEN have a real Fandom
  wiki (<code>scripts/akinator/discover_fandom.py</code>) but that the summarizer's live map
  (<code>games/data/fandom/wikis.json</code>) does not know about yet — invisible to <code>/search</code>,
  <code>quiz.py</code> and <code>summary.py</code> until one of these is approved. Proving a wiki exists is not the
  same judgement as trusting it live: open the wiki yourself and check the alias spelling, author and cover before
  approving. Approving writes straight into <code>wikis.json</code>, the same file <code>tools/fandom.py</code>
  reads (cached up to an hour), in one commit.</p>
  <div class="row" style="margin-bottom:12px">
    <button class="act ghost" id="fdReload">Refresh</button>
    <span class="status" id="fdStatus" style="margin:0"></span>
  </div>
  <div id="fdList"></div>
</section>

<footer>Verified against Google Books / Open Library before a row is added — this page cannot invent a book.
Excluding, adding and rewording reach players as soon as GitHub Pages redeploys the commit (about a minute) — no rebuild needed.
A fact correction (year) reaches nobody until the next full <code>build_matrix.py</code> run: it feeds a matrix bit only that recomputes.</footer>
</div>
<script>
${ESC_FN}
const DATA = "https://litheca.com/games/data/akinator";
let books = [], questions = [], excluded = new Set(), dupFlag = [];
let authorsData = {};

function tab(name){
  document.querySelectorAll("nav button").forEach(b=>b.classList.toggle("on", b.dataset.tab===name));
  document.querySelectorAll("section").forEach(s=>s.classList.toggle("on", s.id===name));
}
document.querySelectorAll("nav button").forEach(b=>b.addEventListener("click", ()=>tab(b.dataset.tab)));

async function post(path, body){
  const r = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
  const data = await r.json().catch(()=>({}));
  if (!r.ok) throw new Error(data.detail || ("HTTP " + r.status));
  return data;
}

// WHY THIS FLAG EXISTS, and what it deliberately does not claim.
//
// The owner excluded 人間失格 (rank 65, r=10, 1948) because a Japanese
// title looked wrong in an English game — without being able to see that
// "No Longer Human" (rank 4751, r=0, dated 2024) is the SAME book, and is
// the worse row of the two: it answers almost nothing and its year is an
// edition reprint, so five of the six era questions answer wrongly. The
// page showed title, author, year and rank, which is enough to feel sure
// and not enough to choose correctly.
//
// The rule is r = 0 AND the author has a richer row. Measured over the
// shipped 5,000: r = 0 alone fires on 406 rows (8%, noise), "r = 0 and the
// author has another row" on 106, and this on 80 (1.6%). The extra
// condition is what makes it actionable — if every row by that author is
// also empty there is no better row to prefer, so there is nothing to
// decide and a badge would only cry wolf.
//
// It does NOT name the twin. Pointing at the author's richest other row
// was tried and it lies: for "White Nights" that is Игрокъ, which is The
// Gambler, a different book. So the badge filters to the author instead
// and lets the owner read the list — a hint that a duplicate may be here,
// never an assertion about which row it is.
function computeDupFlags(){
  const byAuthor = {};
  books.forEach((b,i) => {
    const a = (b.a||"").trim();
    if (!a) return;
    (byAuthor[a] = byAuthor[a] || []).push(i);
  });
  dupFlag = books.map((b,i) => {
    if ((b.r||0) !== 0) return false;
    const a = (b.a||"").trim();
    if (!a) return false;
    return (byAuthor[a]||[]).some(j => j !== i && (books[j].r||0) > 0);
  });
  document.getElementById("dupCount").textContent = dupFlag.filter(Boolean).length;
}

function renderBooks(filter){
  const rows = document.getElementById("bookRows");
  const f = (filter||"").toLowerCase();
  const dupOnly = document.getElementById("dupOnly").checked;
  const originFilter = document.getElementById("bookOrigin").value;
  const periodDays = parseInt(document.getElementById("bookPeriod").value, 10);
  const cutoff = periodDays ? (Date.now() / 1000) - periodDays * 86400 : 0;
  const matching = books
    .map((b,i)=>({b,i}))
    .filter(({b,i}) => (!dupOnly || dupFlag[i])
      && (!f || (b.t||"").toLowerCase().includes(f) || (b.a||"").toLowerCase().includes(f))
      // Rows added before this filter shipped carry neither field — treated
      // as "unknown" origin and excluded from every period bucket except
      // "any time", never as a false match for either filter.
      && (!originFilter || (b.origin || "unknown") === originFilter)
      && (!cutoff || (b.addedAt || 0) >= cutoff));
  const shown = matching.slice(0, 300);
  document.getElementById("shownCount").textContent =
    matching.length > shown.length
      ? \`showing \${shown.length} of \${matching.length} — narrow the search to see the rest\`
      : \`\${matching.length} row\${matching.length===1?"":"s"}\`;
  rows.innerHTML = shown.map(({b,i}) => {
    const isOff = excluded.has(b.k);
    const r = b.r||0;
    const dup = dupFlag[i]
      ? \`<span class="badge dup dupBtn" data-author="\${esc(b.a||"")}" title="Empty row (r=0) by an author who has a better-described one. Often a translation or reprint of a book already in the game — click to see everything by this author and judge.">check duplicate</span>\`
      : "";
    return \`<tr class="\${isOff?"excluded":""}" data-key="\${esc(b.k)}">
      <td class="title">\${esc(b.t)}</td><td>\${esc(b.a||"—")}</td>
      <td>\${b.y ?? "—"}</td><td>\${i+1}</td>
      <td class="rich \${r===0?"thin":""}" title="\${r===0?"answers almost nothing":r+" content subjects"}">\${r}</td>
      <td>\${isOff ? '<span class="badge off">excluded</span> ' : ""}\${dup}</td>
      <td class="row">
        <button class="act ghost excludeBtn" data-key="\${esc(b.k)}" data-off="\${isOff}">\${isOff?"Restore":"Exclude"}</button>
        <input type="text" class="yearFix" placeholder="fix year" style="width:80px">
        <button class="act ghost fixYearBtn" data-key="\${esc(b.k)}">Fix</button>
        <button class="act ghost renameBtn" data-key="\${esc(b.k)}" data-i="\${i}"
          title="Change only what the reveal prints — useful when the catalogue's title or author is in a script the player cannot read.">Rename</button>
      </td></tr>\`;
  }).join("") || "<tr><td colspan=7>No matches.</td></tr>";
}

function setStatus(id, msg, ok){
  const el = document.getElementById(id);
  el.textContent = msg; el.className = "status " + (ok===true?"ok":ok===false?"err":"");
  // Reported live, 2026-08-26: three real merges landed correctly in
  // author_overrides.json while the admin saw "no evidence of success" —
  // #auStatus sits BELOW the whole (potentially long) duplicate-pairs list,
  // so a click on a card near the top writes a real confirmation that is
  // simply off-screen. Only for a TERMINAL result (ok is true or false, not
  // the null/undefined "Writing…" placeholder) — scrolling on every
  // keystroke-triggered status would be its own annoyance.
  if (ok === true || ok === false) el.scrollIntoView({block: "nearest", behavior: "smooth"});
}

document.getElementById("bookSearch").addEventListener("input", (e)=>renderBooks(e.target.value));
document.getElementById("dupOnly").addEventListener("change", ()=>
  renderBooks(document.getElementById("bookSearch").value));
document.getElementById("bookOrigin").addEventListener("change", ()=>
  renderBooks(document.getElementById("bookSearch").value));
document.getElementById("bookPeriod").addEventListener("change", ()=>
  renderBooks(document.getElementById("bookSearch").value));

document.getElementById("bookRows").addEventListener("click", async (e)=>{
  // Clicking the flag searches the author, so the owner compares the whole
  // shelf rather than trusting a guess about which row is the twin.
  if (e.target.classList.contains("dupBtn")){
    document.getElementById("dupOnly").checked = false;
    const box = document.getElementById("bookSearch");
    box.value = e.target.dataset.author;
    renderBooks(box.value);
    setStatus("bookStatus", "Everything by " + e.target.dataset.author +
      " — compare r before excluding; the emptiest row is usually the one to drop.");
    return;
  }
  const key = e.target.dataset.key;
  if (!key) return;
  if (e.target.classList.contains("excludeBtn")){
    const off = e.target.dataset.off === "true";
    e.target.disabled = true;
    try {
      const r = await post("/api/exclude", {work_key:key, excluded: !off});
      if (!off) excluded.add(key); else excluded.delete(key);
      renderBooks(document.getElementById("bookSearch").value);
      setStatus("bookStatus", (off?"Restored ":"Excluded ")+key+" — effect: "+r.effect, true);
    } catch (err) { setStatus("bookStatus", String(err.message||err), false); }
    finally { e.target.disabled = false; }
  }
  // Rename swaps the title and author cells for inputs in place. An extra
  // pair of always-visible boxes on all 5,000 rows would crowd out the two
  // actions actually used every session, for one that is needed 33 times.
  if (e.target.classList.contains("renameBtn")){
    const tr = e.target.closest("tr"), i = +e.target.dataset.i;
    if (e.target.textContent === "Rename"){
      tr.children[0].innerHTML = '<input type="text" class="dispT">';
      tr.children[1].innerHTML = '<input type="text" class="dispA" style="min-width:130px">';
      tr.querySelector(".dispT").value = books[i].t || "";
      tr.querySelector(".dispA").value = books[i].a || "";
      e.target.textContent = "Save";
      return;
    }
    const title = tr.querySelector(".dispT").value.trim();
    const author = tr.querySelector(".dispA").value.trim();
    if (!title){ setStatus("bookStatus", "a title cannot be blank", false); return; }
    e.target.disabled = true;
    try {
      const r = await post("/api/display", {work_key:key, title, author});
      books[i].t = title; books[i].a = author;
      renderBooks(document.getElementById("bookSearch").value);
      setStatus("bookStatus", "Renamed for display — effect: "+r.effect, true);
    } catch (err) {
      setStatus("bookStatus", String(err.message||err), false);
      e.target.disabled = false;
    }
    return;
  }
  if (e.target.classList.contains("fixYearBtn")){
    const input = e.target.parentElement.querySelector(".yearFix");
    const value = parseInt(input.value, 10);
    if (!value) { setStatus("bookStatus", "enter a year first", false); return; }
    e.target.disabled = true;
    try {
      const r = await post("/api/correction", {work_key:key, field:"first_publish_year", value});
      setStatus("bookStatus", "Queued year fix for "+key+" — effect: "+r.effect, true);
    } catch (err) { setStatus("bookStatus", String(err.message||err), false); }
    finally { e.target.disabled = false; }
  }
});

function renderQuestions(){
  const rows = document.getElementById("qRows");
  rows.innerHTML = questions.map(q => \`<tr data-id="\${esc(q.id)}">
    <td><code>\${esc(q.id)}</code></td>
    <td><input type="text" class="qText" value="\${esc(q.text)}"></td>
    <td><button class="act ghost saveQ" data-id="\${esc(q.id)}">Save</button></td>
  </tr>\`).join("");
}

document.getElementById("qRows").addEventListener("click", async (e)=>{
  if (!e.target.classList.contains("saveQ")) return;
  const id = e.target.dataset.id;
  const text = e.target.closest("tr").querySelector(".qText").value.trim();
  if (!text) { setStatus("qStatus", "wording cannot be empty", false); return; }
  e.target.disabled = true;
  try {
    const r = await post("/api/question", {question_id:id, text});
    setStatus("qStatus", "Reworded "+id+" — effect: "+r.effect, true);
  } catch (err) { setStatus("qStatus", String(err.message||err), false); }
  finally { e.target.disabled = false; }
});

// ── add a book: search first, review before it goes live ─────────────────
//
// WHY THIS REPLACED A FLAT FORM. The owner's point: the old form let an
// admin type a title/author/summary/themes blind and add the row on one
// click, with no source behind the text and no visibility into what the
// automatic pipeline was about to label. Two fixes, and neither invents
// anything new:
//
//   SEARCH reuses book_data.search_books_list() through /api/book/search
//   — the site's own book search (Fandom's hand-verified catalog first,
//   then a fuzzy subdomain guess mixed with Open Library, then Google
//   Books), not a cascade built to match a description. Picking a result
//   fetches the real title/author/year/description server-side; nothing
//   here re-implements that resolution.
//
//   REVIEW calls /api/book/preview, which runs the SAME extract() + trait
//   model _build_book_row runs at creation, plus a single-work Wikidata
//   lookup neither of those reach. The result is a DRAFT — same shape as
//   the Authors tab's, no network per click — reviewed and corrected
//   before Add book fires the one commit that uses it.
let addPicked = null;   // the chosen /search candidate, or null (manual)
let addAnswers = {};    // draft: question id -> true|false|null
let addTouched = new Set(); // ids the admin actually clicked, for the
                            // "reviewed" badge — addAnswers holds a value
                            // for EVERY suggested id from the start (see
                            // addSuggest), so it alone can't distinguish
                            // "still just the suggestion" from "reviewed"

const addNoAuthor = () => {
  const b = document.getElementById("addNoAuthor");
  return !!(b && b.checked);
};

function addAuthorCheck(){
  const box = document.getElementById("addAuthorField");
  const note = document.getElementById("addAuthorNote");
  if (!box) return null;
  // "No known author" is a VERDICT, so it takes the field out of play rather
  // than sitting next to a name that contradicts it. The server drops the
  // picked record's author for the same reason.
  if (addNoAuthor()){
    box.disabled = true;
    if (note) note.innerHTML = '<span class="sg-new">Recorded as having no author.</span> '
      + "Stored as a fact on the book, not as a blank \\u2014 an empty author field means "
      + "\\u201cwe do not know\\u201d and 173 shipped books are in that state because Open "
      + "Library never wrote one down. No question reads this yet. The book still has to be "
      + "picked from search: with no author to match on, a title alone can resolve to a "
      + "different book.";
    return null;
  }
  box.disabled = false;
  const text = box.value.trim();
  if (!text){ if (note) note.innerHTML = ""; return null; }
  const hit = authorByName(text);
  // AN INLINE WAY OUT, not just a refusal. Reported directly: a book
  // picked from Fandom search names an author the game has never held
  // (the ordinary case for a new web novel, not an edge case), and the
  // ONLY prior path forward was "go to the Authors tab, declare them,
  // come back, re-pick" — easy to miss, and a missed step here means
  // Add book silently refuses to fire at all, which reads as "I added
  // the book but it never showed up" because nothing was ever sent.
  if (note) note.innerHTML = hit
    ? '<span class="sg-new">' + esc(hit.name) + "</span> \\u00b7 <code>" + esc(hit.id)
      + "</code> \\u00b7 " + hit.books.length
      + (hit.books.length === 1 ? " book" : " books")
    : '<span class="sg-over">No author by that name.</span> '
      + '<button class="act ghost addDeclare" data-name="' + esc(text) + '">'
      + "Create \\u201c" + esc(text) + "\\u201d\\u2019s profile now</button>";
  return hit;
}

// The declare-and-repick shortcut. THE SERVER COMPUTES THE KEY, same as
// the Authors tab's own "New author profile" — /api/authors/resolve is
// called for the name_key before /api/authors/save writes a bare profile
// (declare:true, same shape). A client-side auNorm(name) would be a
// SECOND copy of the merge-key rule the Authors tab's own comment already
// warns against holding, for a page that must never disagree with the
// build about what an author's key is.
document.getElementById("addForm").addEventListener("click", async (e) => {
  if (!e.target.classList.contains("addDeclare")) return;
  const name = e.target.dataset.name;
  e.target.disabled = true;
  setStatus("addStatus", "Creating \\u201c" + name + "\\u201d\\u2019s profile\\u2026");
  try {
    const r = await post("/api/authors/resolve", {name});
    if (!r.name_key) throw new Error("that name folds to an empty key — it carries "
      + "nothing to identify an author by");
    await post("/api/authors/save", {key: r.name_key, declare: true, facts: {}, aliases: [],
                                     note: "profile created from Add a book"});
    authorOverrides[r.name_key] = {facts: {}, aliases: []};
    buildAuthorRows();
    document.getElementById("addAuthorField").value = name;
    addAuthorCheck();
    setStatus("addStatus", "Created " + r.name_key + " \\u2014 pick them from the list "
      + "and Add book when ready.", true);
  } catch (err) {
    setStatus("addStatus", String(err.message || err), false);
    e.target.disabled = false;
  }
});

// A SERIES OF VOLUMES IS ONE CANDIDATE, NOT EIGHT. Reported directly from
// a real search: "Circle of Inevitability" came back as 8 rows ("...,
// Volume 1: Nightmare" through "...Volume 8: Eternal Aeon"), one per
// Fandom volume, because search_books_list() is built for the SITE's
// reader-facing search — someone looking for a specific volume to read —
// not for "how many game rows should this become". Left ungrouped, eight
// "Use this one" buttons invite exactly the failure already sitting in
// the shipped data: /site/lord-of-the-mysteries-volume-3/7/8 are three
// stray admin-added rows fragmenting a novel that ALSO has its own
// properly-harvested /fandom/lordofthemysteries row — this quick-add path
// creating a ninth fragment would make that worse, not better.
//
// Grouped by (series title with the ", Volume N: Subtitle" suffix
// stripped, lowercased) + author — the same title-normalizing instinct
// similarRows() already uses for near-duplicate detection elsewhere on
// this page. Only Fandom-sourced rows are grouped: Open Library and
// Google Books already return one row per real edition, and merging THOSE
// by a shared prefix would hide a real different book with a similar name.
// DOUBLE BACKSLASHES, deliberately (\\s not \s) — same reason as
// titleTokens()'s /\\s+/ below: this whole file is a string inside the
// OUTER template literal that is page()'s return value, and a single \s
// or \d there is not a recognised JS string escape, so the outer parser
// silently drops the backslash — confirmed by extracting the emitted
// script and finding the pattern's \s and \d had become bare letters s
// and d, matching literal characters instead of whitespace/digits and
// never firing on a real title.
const _VOLUME_SUFFIX = /,?\\s*(?:vol(?:ume)?|book|part)\\.?\\s*\\d+\\s*:?.*$/i;

function groupSearchResults(results){
  const groups = new Map();
  const out = [];
  for (const r of results){
    const isFandom = r.source === "fandom" || r.source === "fandom_series";
    if (!isFandom){ out.push(r); continue; }
    const series = (r.title || "").replace(_VOLUME_SUFFIX, "").trim() || r.title;
    const key = series.toLowerCase() + "\\u0000" + (r.author || "").toLowerCase();
    const g = groups.get(key);
    if (g){ g.volumes += 1; if (!g.cover_url && r.cover_url) g.cover_url = r.cover_url; }
    else {
      const merged = Object.assign({}, r, {title: series, volumes: 1});
      groups.set(key, merged);
      out.push(merged);
    }
  }
  return out;
}

async function runAddSearch(q){
  const status = document.getElementById("addSearchStatus");
  const host = document.getElementById("addResults");
  if (!q.trim()){ host.innerHTML = ""; status.textContent = ""; return; }
  // STALE RESPONSES MUST NEVER OVERWRITE A NEWER ONE. Reported directly:
  // typing "circle of inevitability" showed "results with no relation to
  // the novel" — reproduced by querying the live search API with partial
  // prefixes ("circle", "circle of ine…") typed along the way: those DO
  // return real but unrelated Open Library noise (the Fandom catalog only
  // matches the full series name), and with a plain debounce and no
  // ordering guard, a slower response to an EARLIER partial query could
  // resolve after the correct final one and silently replace it on
  // screen. A monotonic ticket, checked before rendering, is the fix —
  // the same shape as any fetch-race guard, just not one this page had.
  const ticket = ++runAddSearch._ticket;
  status.textContent = "Searching Fandom, Open Library and Google Books…";
  try {
    const raw = await post("/api/book/search", {q});
    if (ticket !== runAddSearch._ticket) return;   // superseded — drop it
    const results = groupSearchResults(raw || []);
    host.innerHTML = results.slice(0, 12).map((r, i) => {
      const src = r.source === "fandom" || r.source === "fandom_series" ? "Fandom"
        : r.source === "open_library" ? "Open Library"
        : r.source === "google_books" ? "Google Books" : (r.source || "?");
      const vols = r.volumes > 1
        ? '<span class="badge" title="Grouped from ' + r.volumes + ' Fandom volume rows into one '
          + 'candidate. Adding it makes ONE thin row for the whole series \\u2014 for full '
          + 'per-chapter grounding, run the offline Fandom harvest instead.">'
          + r.volumes + " volumes</span>" : "";
      return '<div class="sg"><div class="sg-head"><span class="sg-reason">'
        + esc(r.title) + "</span>" + '<span class="badge">' + esc(src) + "</span>" + vols
        + (r.published_year ? '<span class="sg-when">' + esc(r.published_year) + "</span>" : "")
        + "</div><p>" + esc(r.author || "unknown author") + "</p>"
        + '<button class="act ghost addUse" data-i="' + i + '">Use this one</button></div>';
    }).join("") || '<p class="status">No matches in any source.</p>';
    status.textContent = results.length + " result(s)";
    host.dataset.results = JSON.stringify(results);
  } catch (err) {
    if (ticket !== runAddSearch._ticket) return;
    status.textContent = "Search failed: " + String(err.message || err);
  }
}
runAddSearch._ticket = 0;

let addSearchTimer = null;
document.getElementById("addSearch").addEventListener("input", (e) => {
  clearTimeout(addSearchTimer);
  const q = e.target.value;
  addSearchTimer = setTimeout(() => runAddSearch(q), 500);
});

document.getElementById("addResults").addEventListener("click", (e) => {
  if (!e.target.classList.contains("addUse")) return;
  const results = JSON.parse(document.getElementById("addResults").dataset.results || "[]");
  const r = results[+e.target.dataset.i];
  if (!r) return;
  addPicked = r;
  renderAddForm();
});

document.getElementById("addManual").addEventListener("click", () => {
  addPicked = null;
  renderAddForm();
});

// The whole review-and-submit form, built once a source is picked (or
// manual entry chosen). Rebuilt from scratch on each render — this form
// is short-lived (one book, then gone), so a full-draft object like the
// Authors tab's auDraft would be more machinery than the lifetime justifies.
function renderAddForm(){
  const host = document.getElementById("addForm");
  addAnswers = {}; addTouched = new Set();
  const p = addPicked;
  host.innerHTML = '<div class="card">'
    + (p ? '<p class="effect">From ' + esc(p.source || "search") + ': <strong>'
        + esc(p.title) + "</strong> \\u2014 " + esc(p.author || "unknown author")
        + "</p>" : "")
    + '<div class="field"><label>Title</label><input type="text" id="addTitle" value="'
      + esc(p ? p.title : "") + '"></div>'
    + '<div class="field"><label>Author \\u2014 pick one we already hold. A name typed one '
      + "letter differently from the one we have creates a SECOND author, with the facts "
      + "and the book count split between them.</label>"
      + '<input type="text" id="addAuthorField" list="authorNames" autocomplete="off" value="'
      + esc(p ? (p.author || "") : "") + '">'
      + '<label class="inline"><input type="checkbox" id="addNoAuthor"> This book has no '
      + "known author</label>"
      + '<p class="effect" id="addAuthorNote"></p></div>'
    + '<div class="field"><label>First published (blank = Open Library\\u2019s work-level '
      + "year; six era questions read this, and a wrong year is worse than none)</label>"
      + '<input type="text" id="addYear" inputmode="numeric" style="width:110px" value="'
      + esc(p && p.published_year ? p.published_year : "") + '"></div>'
    + '<div class="field"><label>Summary (grounds the theme labels \\u2014 the richer this '
      + "is, the more the book can answer). "
      + (p && (p.source === "fandom" || p.source === "fandom_series")
        ? '<strong>Fandom search results carry no description \\u2014 "Suggest answers" '
          + "cannot fetch one for this pick. Paste or write a synopsis yourself, or the "
          + "review will show almost every question as unknown, which is honest but "
          + "not very useful.</strong>"
        : "Leave blank when a source was picked and \\u201cSuggest answers\\u201d will "
          + "fetch the catalogue\\u2019s own description.")
      + '</label><textarea id="addSummary"></textarea></div>'
    + '<div class="field"><label>Themes, comma-separated (optional)</label>'
      + '<input type="text" id="addThemes"></div>'
    + '<div class="row"><button class="act ghost" id="addSuggest">Suggest answers</button>'
      + '<span class="status" id="addSuggestStatus"></span></div>'
    + '<div id="addReview"></div>'
    + '<div class="row" style="margin-top:12px"><button class="act" id="addSubmit">Add book</button></div>'
    + '<p class="status" id="addStatus"></p></div>';
  document.getElementById("addAuthorField").addEventListener("input", addAuthorCheck);
  document.getElementById("addNoAuthor").addEventListener("change", addAuthorCheck);
  addAuthorCheck();
}

// The review draft — same visual language as the Authors tab's question
// table (Question | Suggested | Your answer | Yes/No/Unknown), and the
// same rule: nothing is written while these buttons are clicked. Only
// "Add book" commits, once, with whatever is in addAnswers at that moment.
function renderAddReview(questions){
  const host = document.getElementById("addReview");
  const src = (s) => s === "subject" ? "from the subjects"
    : s === "trait model" ? "from the description"
    : s === "wikidata" ? "from Wikidata" : "";
  host.innerHTML = '<div class="scroll"><table class="qtable"><colgroup>'
    + '<col style="width:42%"><col style="width:20%"><col style="width:10%"><col style="width:28%">'
    + '</colgroup><thead><tr><th>Question</th><th>Suggested</th><th></th><th>Your answer</th>'
    + "</tr></thead><tbody>" + questions.map((q) => {
      const touched = addTouched.has(q.id);
      const v = Object.prototype.hasOwnProperty.call(addAnswers, q.id)
        ? addAnswers[q.id] : q.suggested;
      const sugg = q.suggested === true ? '<span class="sg-new">yes</span>'
        : q.suggested === false ? "no" : '<span class="sg-none">unknown</span>';
      const on = (val) => ((val === "yes" && v === true) || (val === "no" && v === false)
        || (val === "unknown" && v === null)) ? "act" : "act ghost";
      const btn = (val, label) => '<button class="' + on(val) + ' addSet" data-q="'
        + esc(q.id) + '" data-v="' + val + '">' + label + "</button>";
      return "<tr><td class=\\"q\\">" + esc(q.text) + '<br><span class="sg-none">'
        + esc(q.id) + "</span></td><td>" + sugg
        + '<br><span class="sg-none">' + src(q.source) + "</span></td><td>"
        + (touched ? '<span class="badge off">reviewed</span>' : "")
        + '</td><td class="row qa">' + btn("yes", "Yes") + btn("no", "No")
        + btn("unknown", "Unknown") + "</td></tr>";
    }).join("") + "</tbody></table></div>"
    + '<p class="effect">Yes/No/Unknown here edits the DRAFT only \\u2014 nothing is written '
    + "until Add book. Everything left unreviewed ships exactly as suggested above.</p>";
}

// window.__addLastQuestions holds the last preview's full question list
// (id/text/suggested/source) so a Yes/No/Unknown click can redraw the
// table from the SAME data with just addAnswers changed — no second
// network call for what is purely a draft edit.
document.getElementById("addForm").addEventListener("click", async (e) => {
  if (e.target.classList.contains("addSet")){
    const q = e.target.dataset.q, v = e.target.dataset.v;
    addAnswers[q] = v === "yes" ? true : v === "no" ? false : null;
    addTouched.add(q);
    if (window.__addLastQuestions) renderAddReview(window.__addLastQuestions);
    return;
  }

  if (e.target.id === "addSuggest"){
    const title = document.getElementById("addTitle").value.trim();
    const author = document.getElementById("addAuthorField").value.trim();
    const summary = document.getElementById("addSummary").value.trim();
    const themes = document.getElementById("addThemes").value.split(",")
      .map((s) => s.trim()).filter(Boolean);
    if (!title){ setStatus("addSuggestStatus", "a title is required first", false); return; }
    const who = authorByName(author);
    e.target.disabled = true;
    setStatus("addSuggestStatus", "Checking the subjects and the description\\u2026");
    try {
      const r = await post("/api/book/preview", {
        title, author, summary, subjects: themes,
        author_ol_key: who && !who.id.startsWith("name:") ? who.id : null,
        google_id: addPicked ? addPicked.google_id : null,
        openlibrary_id: addPicked ? addPicked.openlibrary_id : null,
      });
      if (r.fetched_summary && !summary)
        document.getElementById("addSummary").value = r.fetched_summary;
      if (r.fetched_year && !document.getElementById("addYear").value.trim())
        document.getElementById("addYear").value = r.fetched_year;
      window.__addLastQuestions = r.questions;
      // THE DRAFT STARTS EQUAL TO THE SUGGESTIONS, not empty. The table
      // shows the suggested button already highlighted as "your answer" —
      // found in testing that leaving addAnswers empty until a manual
      // click made that highlight a LIE: Add book would have sent {},
      // applying none of what the screen showed as selected, silently.
      // A fresh preview always REPLACES the draft (never merges into
      // whatever the admin had reviewed on a previous, different search).
      addAnswers = {}; addTouched = new Set();
      r.questions.forEach((q) => { addAnswers[q.id] = q.suggested; });
      renderAddReview(r.questions);
      setStatus("addSuggestStatus", r.trait_error
        ? "Suggested, but the description model failed: " + r.trait_error
        : r.questions.length + " question(s) reviewed", !r.trait_error);
    } catch (err) {
      setStatus("addSuggestStatus", String(err.message || err), false);
    } finally { e.target.disabled = false; }
    return;
  }

  if (e.target.id === "addSubmit"){
    const title = document.getElementById("addTitle").value.trim();
    const author = document.getElementById("addAuthorField").value.trim();
    const summary = document.getElementById("addSummary").value.trim();
    const themes = document.getElementById("addThemes").value.split(",")
      .map((s) => s.trim()).filter(Boolean);
    const rawYear = document.getElementById("addYear").value.trim();
    const none = addNoAuthor();
    if (!title){ setStatus("addStatus", "a title is required", false); return; }
    // The author gate is the whole reason this form has a picker, so it is
    // waived only by an explicit verdict, never by an empty box.
    if (!none && !author){
      setStatus("addStatus", "an author is required — tick \\u201cno known author\\u201d if "
        + "the book genuinely has none", false);
      return;
    }
    if (none && !addPicked){
      setStatus("addStatus", "a book with no author has to be picked from the search above: "
        + "with no author to match on, resolving by title alone could land on a different "
        + "book entirely", false);
      return;
    }
    const who = none ? null : addAuthorCheck();
    if (!none && !who){
      setStatus("addStatus", "\\u201c" + author + "\\u201d is not an author the game holds. "
        + "Create the profile on the Authors tab first.", false);
      return;
    }
    if (rawYear && !/^\\d{1,4}$/.test(rawYear)){
      setStatus("addStatus", "year must be digits only, or blank", false); return;
    }
    e.target.disabled = true;
    setStatus("addStatus", "Verifying and adding\\u2026");
    try {
      const r = await post("/api/book", {
        title, author: none ? "" : who.name, no_author: none, summary, themes,
        year: rawYear ? parseInt(rawYear, 10) : null,
        google_id: addPicked ? addPicked.google_id : null,
        openlibrary_id: addPicked ? addPicked.openlibrary_id : null,
        // A Fandom pick is verified server-side against the SAME Fandom
        // catalog/subdomain match search used to find it, instead of
        // Google Books/Open Library — see _fandom_verified in
        // akinator_admin.py for why re-running that second, unrelated,
        // occasionally-flaky check on top would refuse real web novels
        // that simply have no official English edition.
        source: addPicked ? addPicked.source : null,
        answers: addAnswers,
      });
      // Nothing to attach when there is deliberately no author, and calling
      // link anyway would be asking the server to name a book after an
      // author that does not exist. The no-author fact travelled with the
      // row itself, in the same commit — see append_book_row's extra_files.
      let linked = none
        ? " Recorded as having no known author; no author was attached, which is the point."
        : "";
      if (!none) try {
        const l = await post("/api/authors/link", {key: who.id, work_key: r.key,
                                                   note: "attached when the book was added"});
        const n = Object.keys(l.applied || {}).length;
        linked = " Attached to " + l.key + (n ? ", " + n + " answer(s) applied." : ".");
      } catch (err) {
        linked = " BUT it could not be attached to " + who.id + " (" + String(err.message || err)
          + ") \\u2014 the book is in, the author link is not. Attach it from the Authors tab.";
      }
      // THE MESSAGE GOES IN addSearchStatus, not addStatus — addStatus
      // lives INSIDE the form card this success clears via renderAddForm()
      // below, so setting it there and then wiping the form was deleting
      // the confirmation before the admin could read it. addSearchStatus
      // sits in the persistent search card above and survives the reset.
      setStatus("addSearchStatus", "Added \\u201c" + r.title + "\\u201d"
        + (r.author ? " by " + r.author : "")
        + (r.year ? " (" + r.year + ")" : " (no year)") + " \\u2014 effect: " + r.effect
        + "." + linked, true);
      addPicked = null;
      document.getElementById("addSearch").value = "";
      document.getElementById("addResults").innerHTML = "";
      renderAddForm();
    } catch (err) { setStatus("addStatus", String(err.message || err), false); }
    finally { e.target.disabled = false; }
  }
});

// ── the reader suggestion queue ──────────────────────────────────────────
//
// WHAT EACH CARD MUST SHOW, and the reason is a real result from building
// this. The one-click Rename approval sends the title AND author that the
// catalogue returned — and for the 人間失格 case Open Library answers
// "No Longer Human" with the author "太宰 治, Juliet Winters Carpenter".
// So a blind Approve would fix the unreadable title and hand back an
// unreadable author, which is half the bug the reader reported.
//
// The queue cannot fix that on its own: the alternative is storing the name
// the reader typed, and stranger-written text is the one thing this feature
// promises never to store. So the card prints CURRENT and PROPOSED side by
// side and the owner reads both before clicking. When the proposed author is
// no better, the Books tab's inline Rename is one search away and the owner
// types it themselves — which is a person editing, not a stranger.
let suggestions = [];

const REASON_LABEL = {
  missing: "Not in the game",
  wrong_year: "Wrong year",
  unreadable: "Unreadable title",
  // Machine-detected via /summary (queue_resolved_book), not reader-typed —
  // see tools/akinator_suggest.py's docstring for why this reason can never
  // arrive through the public /suggest endpoint.
  resolved: "Found via summarizer, not in game",
};

// Which resolutions each reason offers. Mirrors _ALLOWED in
// tools/akinator_suggest.py — the server refuses anything else with a 400,
// so this list decides what is OFFERED, never what is permitted.
const REASON_ACTIONS = {
  missing: [["book", "Add this book", "act"]],
  wrong_year: [["correction", "Apply the year fix", "act"]],
  unreadable: [["display", "Rename for display", "act"],
               ["exclude", "Exclude the row instead", "act danger"]],
  resolved: [["book", "Add this book", "act"]],
};

function when(ts){
  if (!ts) return "";
  return new Date(ts * 1000).toISOString().slice(0, 10);
}

// ── "is this book already here under another name?" ──────────────────────
//
// THE INTAKE CHECK CANNOT ANSWER THIS, and the number is measured, not
// feared. Eight books that ARE in the game were resolved the way a reader
// would have typed them: the exact-normalised-title check the server uses
// caught five. The other three — "The Brothers Karamazov" (we ship
// "Brothers Karamazov"), "Alice in Wonderland" (we ship "Alice's Adventures
// in Wonderland"), "Harry Potter and the Sorcerer's Stone" (we ship
// "Philosopher's Stone") — would arrive here labelled "not in the game".
//
// The Open Library work key does not rescue it either: every one of the
// eight resolved to a work key, and only four matched our row, because OL
// holds several work entities per book (Crime and Punishment came back as
// OL21062236W, not the row we ship). A match is conclusive; a miss means
// nothing.
//
// SO WHY NOT LOOSEN THE SERVER CHECK? Because the two errors are not
// symmetric. A false negative costs one queue entry a human closes in a
// second. A false positive tells a reader with a genuinely missing book
// "good news — the game does know it", and they never report it again. The
// fuzzy signal belongs where a person is already looking, and this is that
// place. It is a HINT, labelled as one; it never blocks anything.
const STOP = {the:1, a:1, an:1, of:1, and:1, in:1, to:1, "&":1};

function titleTokens(s){
  return String(s || "").toLowerCase()
    .normalize("NFKD").replace(/[\\u0300-\\u036f]/g, "")
    .replace(/[^a-z0-9 ]+/g, " ").split(/\\s+/)
    .filter((t) => t && !STOP[t]);
}

// Containment in EITHER direction, which is what these cases need: our
// "Brothers Karamazov" is inside their "The Brothers Karamazov", and their
// omnibus "Alice in Wonderland (Alice's Adventures... / Snark / ...)"
// contains our short one. Dividing by the smaller side scores both 1.0,
// where dividing by the query's length would score the omnibus 0.17.
function similarRows(title, limit){
  const a = titleTokens(title);
  if (!a.length) return [];
  const setA = new Set(a);
  const out = [];
  for (let i = 0; i < books.length; i++){
    const b = titleTokens(books[i].t);
    if (!b.length) continue;
    let shared = 0;
    const seen = new Set();
    for (const t of b) if (setA.has(t) && !seen.has(t)) { shared++; seen.add(t); }
    if (!shared) continue;
    const score = shared / Math.min(setA.size, new Set(b).size);
    if (score >= 0.6) out.push({ i, score, shared });
  }
  out.sort((x, y) => y.score - x.score || (books[y.i].r || 0) - (books[x.i].r || 0));
  return out.slice(0, limit || 4);
}

function renderSuggestions(){
  const host = document.getElementById("sgList");
  document.getElementById("sgCount").textContent = suggestions.length || "";

  const reasonFilter = document.getElementById("sgReason").value;
  const periodDays = parseInt(document.getElementById("sgPeriod").value, 10);
  const cutoff = periodDays ? (Date.now() / 1000) - periodDays * 86400 : 0;
  const shown = suggestions.filter((s) =>
    (!reasonFilter || s.reason === reasonFilter)
    && (!cutoff || (s.last_seen || s.first_seen || 0) >= cutoff));

  if (!shown.length){
    host.innerHTML = '<p class="status">' + (suggestions.length
      ? "Nothing matches this filter."
      : "Nothing pending. Reports arrive from the give-up screen when a "
        + "reader taps one of the three buttons there, or automatically "
        + "when the summarizer resolves a book the game does not have.")
      + "</p>";
    return;
  }
  const byKey = {};
  books.forEach((b) => { byKey[b.k] = b; });

  host.innerHTML = shown.map((s) => {
    const row = s.work_key ? byKey[s.work_key] : null;
    let current = "";
    if (s.work_key){
      current = row
        ? esc(row.t) + " — " + esc(row.a || "unknown") + (row.y ? " (" + row.y + ")" : " (no year)")
        : '<span class="sg-none">' + esc(s.work_key) + " — not in the loaded list</span>";
    } else {
      current = '<span class="sg-none">not in the game</span>';
    }

    let proposed;
    if (s.reason === "wrong_year"){
      proposed = '<span class="sg-new">' + esc(s.year) + "</span>";
    } else {
      proposed = '<span class="sg-new">' + esc(s.title) + "</span>" +
        (s.author ? " — " + esc(s.author)
                  : ' <span class="sg-none">(no author found)</span>');
      // The year the row would actually get if added. Six era questions read
      // it, so "no year" is a real cost and worth seeing BEFORE approving —
      // it was invisible until it was already committed. "resolved" behaves
      // identically here — it becomes a row through the exact same /book
      // action as "missing", just detected by the summarizer instead of typed.
      if (s.reason === "missing" || s.reason === "resolved"){
        proposed += s.year_hint
          ? ' <span class="badge">first published ' + esc(s.year_hint) + "</span>"
          : ' <span class="badge off" title="Open Library has no work-level year.'
            + ' Six era questions will answer &quot;unknown&quot; for this row.">no year found</span>';
        // What the reader ticked, shown as the SUBJECT WORDS approving would
        // actually write into books.json — not the raw feature ids. The id is
        // an internal name; the subject is the thing being committed, and the
        // reviewer should see the latter.
        const subs = s.theme_subjects || [];
        if (subs.length){
          proposed += '<div class="sg-themes">reader also says it is about: '
            + subs.map((t) => '<span class="badge">' + esc(t) + "</span>").join(" ")
            + "</div>";
        }
      }
    }

    // Duplicate hints, for "missing"/"resolved" claims only — the other two
    // reasons already name a row, so there is nothing to check for a
    // collision against.
    let dupes = "";
    if (s.reason === "missing" || s.reason === "resolved"){
      const exact = s.ol_key ? byKey[s.ol_key] : null;
      const near = similarRows(s.title, 4).filter((h) => books[h.i].k !== s.ol_key);
      if (exact){
        dupes += '<p class="sg-dupe sg-dupe--sure">Already in the game — same Open Library work '
          + '<code>' + esc(s.ol_key) + '</code>: <strong>' + esc(exact.t) + "</strong> — "
          + esc(exact.a || "unknown") + (exact.y ? " (" + exact.y + ")" : "")
          + ". Adding it would duplicate a row.</p>";
      }
      if (near.length){
        dupes += '<p class="sg-dupe">Possibly already here under another title — '
          + 'the check that let this through only compares titles exactly, so read these first:</p><ul class="sg-near">'
          + near.map((h) => {
              const b = books[h.i];
              return "<li><strong>" + esc(b.t) + "</strong> — " + esc(b.a || "unknown")
                + (b.y ? " (" + b.y + ")" : "") + ' <span class="sg-none">r=' + (b.r || 0)
                + ", #" + (h.i + 1) + ", " + Math.round(h.score * 100) + "% of the title words match</span></li>";
            }).join("") + "</ul>";
      }
      if (!exact && !near.length){
        dupes = '<p class="sg-dupe sg-dupe--clear">No row with a similar title, and '
          + (s.ol_key ? "its Open Library work is not one we ship"
                      : "Open Library gave no work key to compare")
          + ". Nothing found is weaker evidence than something found — a title we hold "
          + "under a different name would not show here.</p>";
      }
    }

    const votes = (s.votes || 1) > 1
      ? '<span class="badge">' + (s.reason === "resolved" ? "seen " : "reported ")
        + esc(s.votes) + " times</span>" : "";
    const buttons = (REASON_ACTIONS[s.reason] || []).map(([action, label, cls]) =>
      '<button class="' + cls + ' sgAct" data-id="' + esc(s.id) + '" data-action="' +
      action + '">' + label + "</button>").join("");

    return '<div class="sg" data-id="' + esc(s.id) + '">' +
      '<div class="sg-head"><span class="sg-reason">' +
        esc(REASON_LABEL[s.reason] || s.reason) + "</span>" + votes +
        '<span class="sg-when">first seen ' + esc(when(s.first_seen)) +
        ((s.votes || 1) > 1 ? ", last " + esc(when(s.last_seen)) : "") + "</span></div>" +
      '<dl class="sg-cmp"><dt>Current</dt><dd>' + current + "</dd>" +
        "<dt>" + (s.reason === "resolved" ? "Summarizer" : "Reader") + "</dt><dd>" + proposed + "</dd></dl>" + dupes +
      '<div class="row">' + buttons +
        '<button class="act ghost sgAct" data-id="' + esc(s.id) +
        '" data-action="reject">Dismiss</button></div></div>';
  }).join("");
}

// ── edit one book, everything about it ───────────────────────────────────
//
// ALMOST NO NEW BACKEND. Every field here already had an endpoint; what was
// missing was a place to see them together. Title and author go through
// /api/display, the year through /api/correction, and each of the 48
// questions through /api/taught/apply — which never required the cell to
// have play counts, so it was already a general "set this cell by hand".
//
// THE THREE FIELDS LAND AT THREE DIFFERENT TIMES and the panel says so per
// field rather than in a footnote. An admin page that implies a year change
// is live is worse than one that cannot change the year at all:
//
//   title/author   overrides display only        as soon as Pages redeploys
//   a question     overrides.json, clamp bound   same, and held from the drain
//   the year       feeds a matrix bit            NEXT FULL REBUILD ONLY
//
// The matrix is fetched here and nowhere else on this page: showing what a
// cell says today is the whole point, and "set it to yes" without showing
// that it already says yes is how you get an override that changes nothing.
let matrix = null, meta = null, overrides = {};
// admin_corrections.json — the queued-for-next-rebuild facts, as opposed to
// overrides.json's live clamps. Only no_author is read here so far, and it
// is read rather than remembered so the box is right on a fresh page load,
// not merely for the rest of the session that ticked it.
//
// NO BACKTICKS IN THIS FILE'S CLIENT SCRIPT, not even in a comment: the whole
// script is a template literal, so one closes it and the page stops parsing.
// check.mjs catches it, which is exactly why check.mjs exists.
let corrections = {};

function cellState(bookIndex, qIndex){
  if (!matrix || !meta) return null;
  const off = bookIndex * meta.bytes_per_row;
  return (matrix[off + (qIndex >> 2)] >> ((qIndex & 3) * 2)) & 3;
}

// ── the book editor: same shape as the Authors tab's, on purpose ─────────
//
// IT USED TO SAVE ON EVERY BUTTON — same failure the Authors tab's editor
// had before this session, for the same reason: setting a handful of
// answers meant a handful of commits and a handful of waits for "effect:
// held against the nightly drain" before the next click could be trusted.
// So this is the same fix, in the same shape: a DRAFT held in the page
// (edDraft, whose owner is edOpenKey), redrawn instantly, committed once
// via POST /taught/apply_batch (new this session — see akinator_drain.py).
// And the editor opens INSIDE the results table, in the row under the
// book it belongs to, rather than in a panel scrolled away from a list of
// up to 30 matches — the exact inline-embedding the Authors tab already
// moved to and the same reason: no way to tell which row a separate panel
// was about.
let edOpenKey = null, edDraft = null;

// {question id: true|false}, ONLY for cells with a genuine hand-verdict
// clamp (0.90/0.15) — a mid-range value is the drain's own guess from play
// data, not a prior decision to diff a draft against, so it is left out of
// both the seed and the dirty count entirely (see editPanelHtml's "learned
// from play" branch for where it is still shown).
function edCommitted(key){
  const ov = overrides[key] || {};
  const out = {};
  Object.keys(ov).forEach((qid) => {
    if (ov[qid] >= 0.9) out[qid] = true;
    else if (ov[qid] <= 0.15) out[qid] = false;
  });
  return out;
}

function edDirtyCount(){
  if (!edDraft || edOpenKey == null) return 0;
  const committed = edCommitted(edOpenKey);
  let n = 0;
  new Set([...Object.keys(committed), ...Object.keys(edDraft)]).forEach((q) => {
    const a = Object.prototype.hasOwnProperty.call(committed, q) ? committed[q] : "\\u2205";
    const b = Object.prototype.hasOwnProperty.call(edDraft, q) ? edDraft[q] : "\\u2205";
    if (a !== b) n++;
  });
  return n;
}

function edOpen(key){ edOpenKey = key; edDraft = edCommitted(key); }
function edClose(){ edOpenKey = null; edDraft = null; }

function editPanelHtml(i){
  const b = books[i], key = b.k;
  const rows = questions.map((q, qi) => {
    const st = cellState(i, qi);
    // Three states, and "unknown" is NOT "no". The table asserting nothing
    // is the case most worth editing, so it must not read as a denial.
    const table = st === 1 ? '<span class="sg-new">yes</span>'
      : st === 0 ? "no"
      : '<span class="sg-none">no record</span>';
    const has = Object.prototype.hasOwnProperty.call(edDraft, q.id);
    const v = has ? edDraft[q.id] : undefined;
    const o = (overrides[key] || {})[q.id];
    const hadCommitted = o !== undefined;
    // A PROBABILITY IS NOT AN ANSWER. 0.90/0.15 are the two clamp bounds a
    // hand verdict writes, so they map back to yes/no exactly; anything
    // else came from the drain and IS a probability, labelled as learned
    // rather than dressed up as a verdict the draft could diff against.
    let cur;
    if (has) cur = v === true ? '<span class="badge off">YES</span>' : '<span class="badge off">NO</span>';
    else if (hadCommitted && o > 0.15 && o < 0.9)
      cur = '<span class="badge">learned from play: ' + esc(o) + "</span>";
    else cur = '<span class="sg-none">\\u2014</span>';
    const on = (val) => (has && ((val === "yes" && v === true) || (val === "no" && v === false)))
      ? "act" : "act ghost";
    const btn = (val, label) => '<button class="' + on(val) + ' edSet" data-q="'
      + esc(q.id) + '" data-v="' + val + '">' + label + "</button>";
    return "<tr><td class=\\"q\\">" + esc(q.text) + '<br><span class="sg-none">' + esc(q.id) + "</span></td>"
      + "<td>" + table + "</td><td>" + cur + "</td>"
      + '<td class="row qa">' + btn("yes", "Yes") + btn("no", "No")
      + ((has || hadCommitted) ? '<button class="act ghost edSet" data-q="' + esc(q.id)
          + '" data-v="clear">Clear</button>' : "")
      + "</td></tr>";
  }).join("");

  const dirty = edDirtyCount();
  return '<div class="card" style="max-width:none;margin:4px 0" data-key="' + esc(key) + '">'
    + '<div class="field"><label>Title shown to the player</label>'
      + '<input type="text" class="edTitle" value="' + esc(b.t) + '"></div>'
    + '<button class="act ghost edSaveName">Save the title</button>'
    + '<p class="effect">Changes only what the reveal prints. No question and no '
    + "matrix bit reads it.</p>"
    // THE AUTHOR IS NOT A TEXT FIELD ANY MORE. It used to be, next to the
    // title, saving through /api/display — which renamed what was PRINTED
    // and left the book attributed to whoever it was attributed to before.
    // Typing a name here therefore looked like a fix and moved nothing the
    // engine reads. Attaching is the real operation: the printed name, the
    // author's known answers, and author_name/author_key for the next
    // rebuild, in one commit.
    + '<div class="field" style="margin-top:16px"><label>Author \\u2014 pick one the game '
      + "already holds. Attaching changes who the book is BY, not just what is printed."
      + '</label><input type="text" class="edAuthor" list="authorNames" '
      + 'autocomplete="off" value="' + esc(b.a || "") + '">'
      + '<p class="effect edAuthorNote"></p></div>'
    + '<button class="act ghost edLinkAuthor">Attach this book to that author</button>'
    + '<p class="effect">The name and the author\\u2019s known answers are instant; who the '
    + "book is BY lands at the next full build_matrix.py run, from "
    + "admin_corrections.json. Not on the list? Create the profile on the Authors tab "
    + "first \\u2014 a new name typed here would split an author rather than move a book."
    + "</p>"
    // THE OTHER ANSWER TO "who wrote this", and it is a claim rather than a
    // blank. 173 of the 5,000 shipped books carry no author at all, and
    // reading them says that is Open Library never writing one down — not
    // one of them is actually anonymous. So an empty author has to keep
    // meaning "we do not know", and a book that really has none says so
    // here, once, by hand.
    // CHECKED FROM THE COMMITTED FILE, not from nothing. Rendered blank it
    // sprang back up the instant the write succeeded — edRedraw() rebuilds
    // this panel — so a fact that HAD been recorded read as unset, which is
    // indistinguishable from a write that failed. Same failure the Add-a-book
    // review table had: a control whose visible state was not its real one.
    + '<label class="inline" style="margin-top:10px"><input type="checkbox" class="edNoAuthor"'
      + ((corrections[key] || {}).no_author ? " checked" : "")
      + "> This book has no known author</label>"
    + '<p class="effect">Recorded as a fact for the next rebuild, and it clears any '
    + "attachment above \\u2014 the two are the same claim negated. No question reads it yet: "
    + "at 3.5% of the corpus one could not clear the 5% floor, and a rule guessing it from a "
    + "blank author field would answer yes for 173 books that all have authors.</p>"
    + '<div class="field" style="margin-top:16px"><label>First published '
      + "(feeds six era questions)</label>"
      + '<input type="text" class="edYear" inputmode="numeric" style="width:120px" value="'
      + esc(b.y == null ? "" : b.y) + '"></div>'
    + '<button class="act ghost edSaveYear">Queue the year</button>'
    + '<p class="effect">NOT instant. A year feeds a matrix bit that only a local '
    + "build_matrix.py run recomputes.</p>"
    + '<div class="scroll"><table class="qtable"><colgroup><col style="width:42%">'
    + '<col style="width:15%"><col style="width:15%"><col style="width:28%"></colgroup>'
    + "<thead><tr><th>Question</th><th>Table says</th>"
    + "<th>Override</th><th></th></tr></thead><tbody>" + rows + "</tbody></table></div>"
    + '<div class="row" style="margin-top:12px">'
      + '<button class="act edSave"' + (dirty ? "" : " disabled") + ">Save"
      + (dirty ? " " + dirty + " change" + (dirty === 1 ? "" : "s") : " \\u2014 nothing changed")
      + "</button>"
      + '<button class="act ghost edRevert"' + (dirty ? "" : " disabled") + ">Discard</button>"
      + "</div>"
    + '<p class="effect">One commit for every question above, written the moment you click '
    + "Save \\u2014 instant after that, same as before. Only the year above is different: it "
    + "queues for the next rebuild.</p></div>";
}

function renderEdRows(filter){
  const rows = document.getElementById("edRows");
  const f = (filter || "").toLowerCase();
  let matching = f.length >= 2
    ? books.map((b, i) => ({b, i})).filter(({b}) =>
        (b.t || "").toLowerCase().includes(f) || (b.a || "").toLowerCase().includes(f))
    : [];
  // The open book stays visible even under a search that no longer
  // matches it — an editor must not vanish out from under an admin
  // mid-edit, same rule the Authors tab already keeps.
  if (edOpenKey != null && !matching.some(({b}) => b.k === edOpenKey)) {
    const oi = books.findIndex((b) => b.k === edOpenKey);
    if (oi >= 0) matching.unshift({b: books[oi], i: oi});
  }
  if (!matching.length) {
    rows.innerHTML = '<tr><td colspan="4">' +
      (f.length >= 2 ? "No match." : "Type at least 2 characters to search.") + "</td></tr>";
    return;
  }
  const shown = matching.slice(0, 30);
  rows.innerHTML = shown.map(({b, i}) => {
    const open = b.k === edOpenKey;
    const head = "<tr><td class=\\"title\\">" + esc(b.t) + "</td><td>" + esc(b.a || "\\u2014")
      + "</td><td>" + (b.y ?? "\\u2014") + '</td><td><button class="act ghost edOpenBtn" '
      + 'data-i="' + i + '">' + (open ? "Close" : "Edit") + "</button></td></tr>";
    return open
      ? head + '<tr class="auEdit"><td class="auEditCell" colspan="4">'
        + editPanelHtml(i) + "</td></tr>"
      : head;
  }).join("");
}

const edSearchBox = () => document.getElementById("edSearch");
const edRedraw = () => renderEdRows(edSearchBox().value);

document.getElementById("edSearch").addEventListener("input", edRedraw);

document.getElementById("edRows").addEventListener("input", (e) => {
  if (!e.target.classList.contains("edAuthor")) return;
  const card = e.target.closest(".card");
  const note = card.querySelector(".edAuthorNote");
  const text = e.target.value.trim();
  if (!text) { note.innerHTML = ""; return; }
  const hit = authorByName(text);
  note.innerHTML = hit
    ? '<span class="sg-new">' + esc(hit.name) + "</span> \\u00b7 <code>" + esc(hit.id)
      + "</code> \\u00b7 " + hit.books.length + (hit.books.length === 1 ? " book" : " books")
    : '<span class="sg-over">No author by that name.</span> Create the profile on the '
      + "Authors tab first.";
});

document.getElementById("edRows").addEventListener("click", async (e) => {
  if (e.target.classList.contains("edOpenBtn")) {
    const i = +e.target.dataset.i, key = books[i].k;
    const dirty = edDirtyCount();
    if (edOpenKey === key) {
      if (dirty && !confirm(dirty + " unsaved change(s) will be discarded. Close anyway?")) return;
      edClose();
    } else {
      if (dirty && !confirm(dirty + " unsaved change(s) on the open book will be "
          + "discarded. Switch anyway?")) return;
      edOpen(key);
    }
    edRedraw();
    return;
  }

  const card = e.target.closest(".card");
  if (!card) return;
  const key = card.dataset.key;
  const i = books.findIndex((b) => b.k === key);

  // Draft edits: no network, no commit, no wait — see the note above
  // edOpenKey for why this used to be a POST per click.
  if (e.target.classList.contains("edSet")) {
    const q = e.target.dataset.q, v = e.target.dataset.v;
    if (v === "clear") delete edDraft[q];
    else edDraft[q] = v === "yes";
    edRedraw();
    return;
  }

  if (e.target.classList.contains("edRevert")) { edOpen(key); edRedraw(); return; }

  // Its OWN write, not part of the question draft: this is a fact about the
  // book for the next rebuild, while the draft below is a set of live matrix
  // clamps. Batching them would make one Save mean two different kinds of
  // thing, which is how the year field already earned its separate button.
  if (e.target.classList.contains("edNoAuthor")) {
    const on = e.target.checked;
    e.target.disabled = true;
    setStatus("edStatus", on ? "Recording that it has no author\\u2026"
                             : "Clearing that\\u2026");
    try {
      const r = await post("/api/noauthor", {work_key: key, no_author: on});
      // Kept in the same shape the file has, so the redraw below reads the
      // committed truth rather than a second, parallel flag.
      if (r.no_author) (corrections[key] = corrections[key] || {}).no_author = true;
      else if (corrections[key]) delete corrections[key].no_author;
      // Only what the reveal prints, and only when the fact was SET — the
      // page cannot restore a name it never had, so unticking leaves the
      // display alone rather than inventing one.
      if (on && books[i]) books[i].a = "";
      edRedraw();
      setStatus("edStatus", (on
        ? "Recorded as having no known author."
        : "No longer marked \\u2014 back to \\u201cwe do not know\\u201d.")
        + " " + r.note, true);
    } catch (err) {
      e.target.checked = !on;
      setStatus("edStatus", String(err.message || err), false);
    } finally { e.target.disabled = false; }
    return;
  }

  if (e.target.classList.contains("edSave")) {
    const n = edDirtyCount();
    const committed = edCommitted(key);
    const answers = {};
    new Set([...Object.keys(committed), ...Object.keys(edDraft)]).forEach((qid) => {
      const a = Object.prototype.hasOwnProperty.call(committed, qid) ? committed[qid] : undefined;
      const b2 = Object.prototype.hasOwnProperty.call(edDraft, qid) ? edDraft[qid] : undefined;
      if (a === b2) return;
      answers[qid] = b2 === true ? "yes" : b2 === false ? "no" : "clear";
    });
    e.target.disabled = true;
    setStatus("edStatus", "Writing\\u2026");
    try {
      const r = await post("/api/taught/apply_batch", {work_key: key, answers});
      Object.entries(r.applied || {}).forEach(([qid, value]) => {
        if (value == null) delete (overrides[key] || {})[qid];
        else (overrides[key] = overrides[key] || {})[qid] = value;
      });
      edOpen(key); // resync the draft to what just committed
      edRedraw();
      setStatus("edStatus", n + " change" + (n === 1 ? "" : "s")
        + " saved in one commit \\u2014 " + r.note, true);
    } catch (err) {
      setStatus("edStatus", String(err.message || err), false);
      e.target.disabled = false;
    }
    return;
  }

  if (e.target.classList.contains("edSaveName")) {
    const title = card.querySelector(".edTitle").value.trim();
    if (!title) { setStatus("edStatus", "a title cannot be blank", false); return; }
    try {
      const r = await post("/api/display", {work_key: key, title});
      books[i].t = title;
      edRedraw();
      setStatus("edStatus", "Renamed for display — " + r.effect, true);
    } catch (err) { setStatus("edStatus", String(err.message || err), false); }
    return;
  }

  if (e.target.classList.contains("edLinkAuthor")) {
    const text = card.querySelector(".edAuthor").value.trim();
    const who = authorByName(text);
    if (!who) {
      setStatus("edStatus", "pick an author the game already holds \\u2014 create the "
        + "profile on the Authors tab if there is none", false);
      return;
    }
    e.target.disabled = true;
    setStatus("edStatus", "Attaching\\u2026");
    try {
      const r = await post("/api/authors/link", {work_key: key, key: who.id});
      books[i].a = r.author;
      const n = Object.keys(r.applied || {}).length;
      buildAuthorRows();
      setStatus("edStatus", "Now by " + r.author + " \\u2014 "
        + (n ? n + " answer(s) live now" : "no answers to apply") + ". "
        + r.effect + " " + r.note, true);
      edRedraw();
    } catch (err) {
      setStatus("edStatus", String(err.message || err), false);
      e.target.disabled = false;
    }
    return;
  }

  if (e.target.classList.contains("edSaveYear")) {
    const raw = card.querySelector(".edYear").value.trim();
    if (!/^\\d{1,4}$/.test(raw)) { setStatus("edStatus", "a year, digits only", false); return; }
    try {
      const r = await post("/api/correction",
        {work_key: key, field: "first_publish_year", value: parseInt(raw, 10)});
      setStatus("edStatus", "Year queued — " + r.effect, true);
    } catch (err) { setStatus("edStatus", String(err.message || err), false); }
  }
});

// ── authors: identity, and facts that fan out to a whole shelf ───────────
//
// WHERE THE LIST COMES FROM, and why it is not just authors.json's
// authors[] array. That array is the ASKABLE set -- authors with 2+ books,
// 795 of them. Its books[] array references 3,939, and a ONE-BOOK author is
// exactly the case this tab exists for: a manually added row carries no
// Open Library author key, so its author is identified by name alone and is
// the one most likely to be a split identity.
//
// It is also STALE. append_book_row() rewrites books.json, matrix.bin and
// meta.json and leaves authors.json alone — measured at 6 rows behind on
// 2026-08-24, every one of them a manually added /site/ book. Those rows are
// padded back in from books.json here, the same way the server does it: a
// synced row always has an empty author_key, so name:{merge_key} of its 'a'
// field IS its identity.
//
// WHAT THIS CANNOT SEE, said out loud rather than hidden: books.json carries
// only the FIRST author's name, so 1,063 of the 3,939 ids are second authors
// whose name we do not have. They are listed by id and excluded from the
// duplicate detector — comparing two Open Library ids as if they were names
// flags OL2816667A against OL2816668A, which is noise, not a duplicate.
let authorRows = [], authorById = {}, authorDupes = [], authorOverrides = {};

function auNorm(s){
  return String(s || "").toLowerCase().normalize("NFKD")
    .replace(/[\\u0300-\\u036f]/g, "")
    .replace(/[.,"'\`\\u2018\\u2019\\u201c\\u201d()\\[\\]]+/g, " ")
    .replace(/\\s+/g, " ").trim();
}

function buildAuthorRows(){
  const per = (authorsData.books || []).map((r) => Array.isArray(r) ? r : []);
  // Every id at an index below this came from the BUILD and is exactly what
  // AuthorIndex computed. Everything above it is this file's guess.
  const built = per.length;
  // Padding for rows appended since the last full rebuild — see above.
  for (let i = per.length; i < books.length; i++){
    const nk = auNorm(books[i].a || "");
    per.push(nk ? ["name:" + nk] : []);
  }
  const listed = {};
  (authorsData.authors || []).forEach((a) => { listed[a.id] = a; });

  authorById = {};
  per.forEach((ids, i) => ids.forEach((id, slot) => {
    const p = authorById[id] || (authorById[id] = {id, name:"", books:[], approx:true});
    // AN ID THIS PAGE GUESSED IS NOT AN IDENTITY, and the difference is not
    // cosmetic. auNorm above folds case, diacritics and punctuation, but
    // canonical_author in authors.py ALSO strips catalogue date suffixes and
    // un-inverts "Last, First" — so a padded row for "Lord George Gordon
    // Byron, 1788-" is called name:lord george gordon byron 1788- here and
    // name:lord george gordon byron by the build, and "Lord George Gordon
    // Byron, George Gordon Byron" lands on a different token order again.
    // The first of those two is not even a key the server will accept:
    // is_valid_key re-derives it and refuses anything that does not match.
    //
    // So an id is marked approximate unless a BUILD row asserted it, and
    // auSave asks the server for the real one before writing. Copying
    // canonical_author's rules into this file instead would be a fourth
    // normalizer to keep in step — the exact thing author_overrides.py's
    // docstring says produces keys that silently match nothing.
    if (i < built) p.approx = false;
    p.books.push(i);
    // books.json's 'a' is the FIRST author only, so it names ids[0] and
    // nothing else. Claiming it for a co-author would put the lead author's
    // name on somebody else's row.
    if (!p.name && slot === 0) p.name = books[i].a || "";
  }));
  // Authors who exist only in the overlay: declared on this tab before
  // they have a book. Without them a new profile would be invisible the
  // moment it was created, and Add-a-book — which will not accept an
  // author who has no profile — could never be unblocked.
  Object.keys(authorOverrides).forEach((id) => {
    // A key already in the overlay came back from the server, so it is real.
    if (!authorById[id]) authorById[id] = {id, name:"", books:[], declared:true,
                                           approx:false};
  });
  Object.values(authorById).forEach((p) => {
    if (!p.name) p.name = (listed[p.id] || {}).n
      || (p.id.startsWith("name:") ? p.id.slice(5) : "");
  });
  authorRows = Object.values(authorById).sort((a,b) =>
    b.books.length - a.books.length || (a.name||a.id).localeCompare(b.name||b.id));
  computeAuthorDupes();
  renderAuthorDatalist();
}

// The picker every author name is typed into now — Add-a-book and the
// book editor both. WHY A LIST AND NOT A TEXT BOX: a typed name that
// differs from the one we hold by a single letter creates a SECOND author,
// silently, with the facts and the book count split between them. That is
// the whole failure this tab exists to repair, and the Add form was the
// easiest place in the product to cause it.
function renderAuthorDatalist(){
  const seen = new Set();
  const opts = authorRows.filter((p) => {
    if (!p.name || seen.has(p.id)) return false;
    seen.add(p.id);
    return true;
  }).map((p) => '<option value="' + esc(p.name) + '">' + esc(p.id) + " \\u00b7 "
      + p.books.length + (p.books.length === 1 ? " book" : " books") + "</option>");
  document.getElementById("authorNames").innerHTML = opts.join("");
}

// The author the admin means, from what they typed. Matched on the same
// folding AuthorIndex uses, so "J.R.R. Tolkien" finds "J. R. R. Tolkien" —
// the spelling noise the build already merges must not be a reason to
// refuse here. Returns null when nothing matches, which is the case the
// caller has to handle rather than paper over.
function authorByName(text){
  const n = auNorm(text || "");
  if (!n) return null;
  const hits = authorRows.filter((p) => auNorm(p.name) === n);
  if (hits.length) {
    // Prefer a real Open Library identity, then the one with more books.
    hits.sort((a, b) => (a.id.startsWith("name:") ? 1 : 0) - (b.id.startsWith("name:") ? 1 : 0)
                     || b.books.length - a.books.length);
    return hits[0];
  }
  return authorById[text] || authorById["name:" + n] || null;
}

// ── "are these two the same person?" ─────────────────────────────────────
//
// MEASURED BEFORE IT WAS WRITTEN, like computeDupFlags() for books was. Over
// the real 3,941-author population this flags 28 pairs — 0.71% — and the
// version with only the surname rules flagged 25 while MISSING both real
// splits in the live game, because they differ by SPELLING and not by
// initials: Fyodor Dostoyevsky / Fyodor Dostoevsky, Paulo Coelho / Paulo
// Coello. Those two are the only mergeable pairs today and a detector that
// could not see them would have been decoration.
//
// So there are two rules. Same surname, compare the forenames. Nearly the
// same surname (first four letters), compare the whole name by edit
// distance — capped at two edits, and only for names long enough that two
// edits is a typo rather than a different person, which is what keeps
// short names out. Dropping the fuzzy bucket costs the two real cases;
// widening it past two edits flagged unrelated people in testing.
//
// IT IS A HINT AND IT SAYS SO. Merging is a judgement about two human
// beings, taken by a person looking at both shelves — never automatic, for
// the same reason the reader-suggestion dedup check stays strict.
function auLev(a, b, cap){
  if (Math.abs(a.length - b.length) > cap) return cap + 1;
  let prev = [], cur = [];
  for (let j = 0; j <= b.length; j++) prev[j] = j;
  for (let i = 1; i <= a.length; i++){
    cur = [i];
    let best = i;
    for (let j = 1; j <= b.length; j++){
      cur[j] = Math.min(prev[j] + 1, cur[j-1] + 1,
                        prev[j-1] + (a[i-1] === b[j-1] ? 0 : 1));
      if (cur[j] < best) best = cur[j];
    }
    if (best > cap) return cap + 1;
    prev = cur;
  }
  return prev[b.length];
}

function auInitials(a, b){
  if (a.length !== b.length || !a.length) return false;
  let hit = false;
  for (let i = 0; i < a.length; i++){
    if (a[i] === b[i]) continue;
    if (a[i].length === 1 && b[i].startsWith(a[i])) hit = true;
    else if (b[i].length === 1 && a[i].startsWith(b[i])) hit = true;
    else return false;
  }
  return hit;
}

function computeAuthorDupes(){
  const groups = {};
  authorRows.forEach((p) => {
    p.n = auNorm(p.name);
    p.toks = p.n ? p.n.split(" ").filter(Boolean) : [];
    const real = p.toks.filter((t) => t.length > 1 && !/\\d/.test(t));
    p.sur = real.length ? real[real.length-1] : "";
    // No name means no comparison. See the note above on the 1,063 ids
    // books.json cannot name.
    if (!p.name || !p.sur) return;
    (groups[p.sur] = groups[p.sur] || []).push(p);
    if (p.sur.length >= 4){
      const k = "~" + p.sur.slice(0, 4);
      (groups[k] = groups[k] || []).push(p);
    }
  });

  const seen = new Set();
  authorDupes = [];
  Object.entries(groups).forEach(([bucket, list]) => {
    if (list.length < 2) return;
    const fuzzy = bucket.startsWith("~");
    for (let i = 0; i < list.length; i++)
    for (let j = i + 1; j < list.length; j++){
      const A = list[i], B = list[j];
      const pk = A.id < B.id ? A.id + "|" + B.id : B.id + "|" + A.id;
      if (seen.has(pk)) continue;
      let why = null;
      if (A.sur === B.sur){
        const fa = A.toks.filter((t) => t !== A.sur);
        const fb = B.toks.filter((t) => t !== B.sur);
        const sa = new Set(fa), sb = new Set(fb);
        const subset = (x, y) => x.size < y.size && [...x].every((t) => y.has(t));
        if (A.n === B.n) why = "identical name under two ids";
        else if (!fa.length || !fb.length) why = "one is a bare surname";
        else if (auInitials(fa, fb)) why = "initials";
        else if (subset(sa, sb) || subset(sb, sa)) why = "one name contains the other";
      } else if (fuzzy && A.n !== B.n){
        const shortest = Math.min(A.n.length, B.n.length);
        const d = auLev(A.n, B.n, 2);
        if (d <= 2 && shortest >= 8 && d * 6 <= shortest)
          why = "spelling \\u2014 " + d + (d === 1 ? " edit" : " edits") + " apart";
      }
      if (why){ seen.add(pk); authorDupes.push({a: A, b: B, why}); }
    }
  });
  document.getElementById("auDupCount").textContent = authorDupes.length;
  const merge = authorDupes.filter(auMergeable).length;
  document.getElementById("auDupPill").textContent = merge || "";
}

// An alias is consulted ONLY for an author with no Open Library key —
// authors.py is explicit that a real id outranks a spelling. So a pair where
// BOTH sides carry a key cannot be merged from here at all, and the card
// says so instead of offering a button the server would refuse. Two OL
// records for one person is an Open Library fix.
function auMergeable(pair){
  return pair.a.id.startsWith("name:") || pair.b.id.startsWith("name:");
}

function auOverlay(id){ return authorOverrides[id] || null; }

// Was this name already folded into this id? The dupe DETECTOR has no way
// to know — computeAuthorDupes() groups purely on name-string similarity
// and never reads authorOverrides, so a pair stays flagged identically
// forever after a successful Fold. Checked against the SAVED data, not a
// this-session flag, so it survives a redraw or a page reload — and reads
// straight off authorOverrides, which is refreshed from the live file on
// every save, not a second copy of what "folded" means.
function auAlreadyFolded(intoId, name){
  const ov = auOverlay(intoId);
  const want = (name || "").trim().toLowerCase();
  return !!(ov && Array.isArray(ov.aliases)
    && ov.aliases.some((a) => (a || "").trim().toLowerCase() === want));
}

function auTitles(p, limit){
  return p.books.slice(0, limit || 4)
    .map((i) => books[i] ? books[i].t : "?").join(" \\u00b7 ")
    + (p.books.length > (limit || 4) ? " \\u2026" : "");
}

function renderAuthorDupes(){
  const host = document.getElementById("auDupes");
  if (!document.getElementById("auDupOnly").checked){ host.innerHTML = ""; return; }
  if (!authorDupes.length){
    host.innerHTML = '<p class="status">No pair looks like a duplicate. That is weaker '
      + "evidence than it sounds \\u2014 two spellings that share no surname and are more "
      + "than two edits apart would not show here.</p>";
    return;
  }
  host.innerHTML = authorDupes.map((d, k) => {
    const side = (p, other) => "<dd><strong>" + esc(p.name || p.id) + "</strong> "
      + '<span class="sg-none">' + esc(p.id) + ", " + p.books.length
      + (p.books.length === 1 ? " book" : " books") + "</span><br>"
      + '<span class="sg-none">' + esc(auTitles(p)) + "</span>"
      // THE BUTTON BELONGS TO THE SIDE THE OTHER ONE CAN BE FOLDED INTO, and
      // the test is on the OTHER side, not this one. It used to ask whether
      // THIS side had an Open Library key, which is right for an OL-vs-name
      // pair by accident and wrong for name-vs-name: both sides failed it, so
      // five of the seven mergeable pairs — the whole Byron cluster, one poet
      // split across four identities — printed "Folding adds one name as an
      // alias of the other" above no button at all. An alias is only ever
      // consulted for an author with NO key, so what has to be true is that
      // the side being folded IN has none; where it is going does not matter.
      + (other.id.startsWith("name:")
          ? (auAlreadyFolded(p.id, other.name)
              ? '<br><span class="sg-new">\\u2713 Already folded into this one</span>'
                + '<p class="effect">Still listed as a possible duplicate because the '
                + "detector cannot see author_overrides.json \\u2014 that is expected, not "
                + "a sign anything failed.</p>"
              : '<br><button class="act ghost auMerge" data-into="' + esc(p.id)
                + '" data-alias="' + esc(other.name) + '">Fold \\u201c'
                + esc(other.name) + '\\u201d into this one</button>')
          : "") + "</dd>";
    return '<div class="sg"><div class="sg-head"><span class="sg-reason">'
      + esc(d.why) + "</span></div>"
      + '<dl class="sg-cmp"><dt>A</dt>' + side(d.a, d.b)
      + "<dt>B</dt>" + side(d.b, d.a) + "</dl>"
      + (auMergeable(d)
          ? '<p class="effect">Folding adds one name as an alias of the other. It takes '
            + "effect at the NEXT full rebuild, not now, and it does not rewrite the book "
            + "count either side already has.</p>"
          : '<p class="sg-dupe">Both sides have an Open Library author key, so this cannot '
            + "be merged here: an alias is only ever consulted for an author with no key. "
            + "If they really are one person, the fix belongs in Open Library.</p>")
      + "</div>";
  }).join("");
}

function renderAuthors(filter){
  const rows = document.getElementById("auRows");
  const f = auNorm(filter || "");
  const dupOnly = document.getElementById("auDupOnly").checked;
  const originFilter = document.getElementById("auOrigin").value;
  const periodDays = parseInt(document.getElementById("auPeriod").value, 10);
  const cutoff = periodDays ? (Date.now() / 1000) - periodDays * 86400 : 0;
  const inDupe = new Set();
  authorDupes.forEach((d) => { inDupe.add(d.a.id); inDupe.add(d.b.id); });
  // Origin/addedAt are tracked per BOOK (see akinator_sync.append_book_row),
  // not per author — authors.json has no such field, and doesn't need one:
  // an author is just whoever wrote the books they're credited on here. So
  // an author matches an origin/period filter if ANY of their books does.
  const matching = authorRows.filter((p) =>
    (!dupOnly || inDupe.has(p.id))
    && (!f || auNorm(p.name).includes(f) || p.id.toLowerCase().includes(f))
    && (!originFilter || p.books.some((i) => (books[i].origin || "unknown") === originFilter))
    && (!cutoff || p.books.some((i) => (books[i].addedAt || 0) >= cutoff)));
  const shown = matching.slice(0, 200);
  document.getElementById("auShown").textContent = matching.length > shown.length
    ? "showing " + shown.length + " of " + matching.length + " \\u2014 narrow the search"
    : matching.length + (matching.length === 1 ? " author" : " authors");
  renderAuthorDupes();
  // The open author is forced into view even when the search no longer
  // matches them — an editor that vanishes mid-edit because a keystroke
  // narrowed the list would throw away unsaved work.
  if (auOpenId && !shown.some((p) => p.id === auOpenId) && authorById[auOpenId]) {
    shown.unshift(authorById[auOpenId]);
  }
  rows.innerHTML = shown.map((p) => {
    const ov = auOverlay(p.id);
    const nFacts = ov ? Object.keys(ov.facts || {}).length : 0;
    const nAl = ov ? (ov.aliases || []).length : 0;
    const open = p.id === auOpenId;
    const head = "<tr><td class=\\"title\\">" + esc(p.name || "<unnamed>")
      + (p.name ? "" : ' <span class="sg-none">(co-author \\u2014 books.json names only the first)</span>')
      + (p.declared && !p.books.length
          ? ' <span class="badge" title="A profile with no book yet. Created here so the author can be picked when adding one.">no books yet</span>'
          : "")
      + '</td><td class="ident"><code>' + esc(p.id) + "</code>"
      + (p.id.startsWith("name:")
          ? ' <span class="badge" title="No Open Library author key. Identified by name alone \\u2014 the fragile case.">name only</span>'
          : "")
      + (p.approx
          ? ' <span class="badge off" title="This page worked the id out from the name, because the book was added after the last full rebuild and authors.json has not caught up. The build folds names by rules this page does not copy, so the real id is asked for from the server before anything is written.">id guessed</span>'
          : "") + "</td>"
      + '<td class="rich">' + p.books.length + "</td>"
      + "<td>" + (nFacts || nAl
          ? '<span class="badge off">' + nFacts + " fact(s), " + nAl + " alias(es)</span>"
          : '<span class="sg-none">\\u2014</span>') + "</td>"
      + '<td><button class="act ghost auOpen" data-id="' + esc(p.id) + '">'
      + (open ? "Close" : "Edit") + "</button></td></tr>";
    // The editor opens INSIDE the table, in the row under the name it
    // belongs to. It used to sit in one panel below the whole list, which
    // meant scrolling away from the author you were editing to see the
    // form — and no way to tell which of 3,941 names it was about.
    return open
      ? head + '<tr class="auEdit"><td class="auEditCell" colspan="5">'
        + authorPanelHtml(p.id) + "</td></tr>"
      : head;
  }).join("") || '<tr><td colspan="5">No match.</td></tr>';
}

// The eight author questions, as the SHIPPED game asks them. Anything the
// build dropped is not offered: setting a fact for a question nobody is
// asked changes nothing, the same rule the Taught tab applies to retired
// cells. An overlay that already holds one is still shown, so it can be
// cleared.
function auQuestions(id){
  const shipped = questions.filter((q) => q.id.startsWith("author:"));
  const have = new Set(shipped.map((q) => q.id));
  // The DRAFT, not the committed overlay: a retired id already in the file
  // has to stay visible so it can be cleared, and it must not vanish from
  // the panel the moment Clear is pressed but before Save.
  const facts = (auOpenId === id && auDraft)
    ? auDraft.facts : ((auOverlay(id) || {}).facts || {});
  Object.keys(facts).forEach((q) => {
    if (!have.has(q)) shipped.push({id: q, text: q, retired: true});
  });
  return shipped;
}

// What the game answers TODAY, read out of matrix.bin. Taken from a book
// where this author is FIRST, because book_traits() gives a co-authored book
// the lead author's facts and reading a row where they are second would show
// somebody else's answers.
function auLeadBook(p){
  const per = authorsData.books || [];
  for (const i of p.books){
    if ((per[i] || [])[0] === p.id) return i;
  }
  return p.books.length ? p.books[0] : -1;
}

// ── the editor, and why every click no longer costs a commit ─────────────
//
// IT USED TO SAVE ON EVERY BUTTON. Each Yes/No/Unknown/Clear was its own
// POST, its own GitHub commit, and its own wait — so setting six answers
// meant six round trips, six commits in the history for one decision, and
// six pauses staring at "effect: next full rebuild only" before the next
// click could be trusted. The owner said so, and they were right: the
// wait is not incidental, it is the cost of having modelled one decision
// as six writes.
//
// So the panel edits a DRAFT held in the page, redraws instantly, and
// commits once. auDraft is the working copy; auOpenId is whose it is.
// Nothing leaves the browser until Save, and the button says how many
// changes are waiting — an editor that looks saved but is not is worse
// than a slow one.
let auOpenId = null, auDraft = null, auResolveCache = {};

function auDirtyCount(){
  if (!auDraft || !auOpenId) return 0;
  const ov = auOverlay(auOpenId) || {facts:{}, aliases:[]};
  let n = 0;
  const keys = new Set([...Object.keys(ov.facts || {}), ...Object.keys(auDraft.facts)]);
  keys.forEach((q) => {
    const a = Object.prototype.hasOwnProperty.call(ov.facts || {}, q) ? ov.facts[q] : "\\u2205";
    const b = Object.prototype.hasOwnProperty.call(auDraft.facts, q) ? auDraft.facts[q] : "\\u2205";
    if (a !== b) n++;
  });
  if ((ov.aliases || []).join("|") !== auDraft.aliases.join("|")) n++;
  return n;
}

function auOpen(id){
  const ov = auOverlay(id) || {facts:{}, aliases:[]};
  auOpenId = id;
  auDraft = {facts: Object.assign({}, ov.facts), aliases: (ov.aliases || []).slice()};
}

function auClose(){ auOpenId = null; auDraft = null; }

function authorPanelHtml(id){
  const p = authorById[id];
  if (!p) return "";
  const lead = auLeadBook(p);
  const isLead = lead >= 0 && ((authorsData.books || [])[lead] || [])[0] === p.id;
  const qIndex = {}; questions.forEach((q, i) => { qIndex[q.id] = i; });

  const rows = auQuestions(id).map((q) => {
    const st = (lead >= 0 && qIndex[q.id] !== undefined)
      ? cellState(lead, qIndex[q.id]) : null;
    const table = q.retired ? '<span class="sg-none">not asked</span>'
      : st === 1 ? '<span class="sg-new">yes</span>'
      : st === 0 ? "no"
      : st === 2 ? '<span class="sg-none">no record</span>'
      : '<span class="sg-none">\\u2026</span>';
    const has = Object.prototype.hasOwnProperty.call(auDraft.facts, q.id);
    const v = has ? auDraft.facts[q.id] : undefined;
    const cur = !has ? '<span class="sg-none">\\u2014</span>'
      : v === true ? '<span class="badge off">YES</span>'
      : v === false ? '<span class="badge off">NO</span>'
      : '<span class="badge off">back to UNKNOWN</span>';
    const on = (val) => (has && ((val === "yes" && v === true)
      || (val === "no" && v === false) || (val === "unknown" && v === null)))
      ? "act" : "act ghost";
    const btn = (val, label) => '<button class="' + on(val) + ' auSet" data-q="'
      + esc(q.id) + '" data-v="' + val + '">' + label + "</button>";
    return "<tr><td class=\\"q\\">" + esc(q.text)
      + (q.retired ? ' <span class="badge">retired</span>' : "")
      + '<br><span class="sg-none">' + esc(q.id) + "</span></td>"
      + "<td>" + table + "</td><td>" + cur + "</td>"
      + '<td class="row qa">' + btn("yes", "Yes") + btn("no", "No")
      + btn("unknown", "Unknown")
      + (has ? '<button class="act ghost auSet" data-q="' + esc(q.id)
               + '" data-v="clear">Clear</button>' : "") + "</td></tr>";
  }).join("");

  const dirty = auDirtyCount();
  return '<div class="card" style="max-width:none;margin:4px 0" data-id="' + esc(id) + '">'
    + '<p class="sub" style="margin-bottom:10px"><code>' + esc(id) + "</code> \\u00b7 "
      + p.books.length + (p.books.length === 1 ? " book" : " books")
      + (p.books.length ? " \\u00b7 " + esc(auTitles(p, 8)) : "") + "</p>"
    + (isLead || !p.books.length ? "" : '<p class="sg-dupe">Not the FIRST author on any '
        + "book we ship, so \\u201cthe game says\\u201d below is read from a row whose facts "
        + "come from somebody else.</p>")
    // ONE PER LINE, NOT COMMA-SEPARATED — found live, 2026-08-26: "Lord George
    // Gordon Byron, 1788-" is a real catalogued name that itself CONTAINS a
    // comma (Open Library's own "Name, birth-" convention). A comma-joined
    // field has no way to tell that internal comma apart from a delimiter,
    // so re-saving the field after it already held that name split it into
    // "Lord George Gordon Byron" and "1788-" — the second half carries no
    // letters, name_key() returns empty, and the save 400s. A newline can
    // never appear inside a name typed into a single-line browser field, so
    // it is the one delimiter that cannot collide with real content.
    + '<div class="field"><label>Aliases \\u2014 other spellings that fold into this author, '
      + "one per line. Only consulted for a book with no Open Library key.</label>"
      + '<textarea class="auAliases" rows="' + Math.max(2, auDraft.aliases.length) + '">'
      + esc(auDraft.aliases.join("\\n")) + '</textarea></div>'
    + '<div class="scroll"><table class="qtable"><colgroup><col style="width:42%">'
      + '<col style="width:15%"><col style="width:15%"><col style="width:28%"></colgroup>'
      + "<thead><tr><th>Question</th>"
      + "<th>The game says today</th><th>Your overlay</th><th></th></tr></thead><tbody>"
      + rows + "</tbody></table></div>"
    + '<div class="row" style="margin-top:12px">'
      + '<button class="act auSave"' + (dirty ? "" : " disabled") + ">Save"
      + (dirty ? " " + dirty + " change" + (dirty === 1 ? "" : "s") : " \\u2014 nothing changed")
      + "</button>"
      + '<button class="act ghost auRevert"' + (dirty ? "" : " disabled") + ">Discard</button>"
      + '<button class="act ghost auLookup">Look up in Open Library</button>'
      + "</div>"
    + '<p class="effect">One commit for everything above, and it lands at the next full '
      + "build_matrix.py run. Nothing is written while you click.</p>"
    + '<div class="auResolve">' + (auResolveCache[id] || "") + "</div>"
    + linkBoxHtml(id) + "</div>";
}

// ── attach a book we already ship to this author ─────────────────────────
//
// WHAT "IMMEDIATELY" CAN AND CANNOT MEAN. Who a book is BY lives on the
// corpus row, and the corpus is a local file this server has never seen —
// so the durable half of a link is a correction, and corrections land at
// the next build like every other one. What CAN be instant is the two
// things the browser reads at play time: the name the reveal prints, and
// the cells overrides.json clamps. So a link writes all three at once and
// the panel says which is which, rather than implying the whole thing is
// live or making the owner wait for a rebuild to see anything.
function linkBoxHtml(id){
  const p = authorById[id];
  return '<div class="field" style="margin-top:16px"><label>Attach a book to '
    + esc(p.name || id) + " \\u2014 search a title we already ship</label>"
    + '<input type="search" class="auBookSearch" placeholder="Title or current author\\u2026"></div>'
    + '<div class="auBookHits"></div>'
    + '<p class="effect">Attaching writes the name and this author\\u2019s known answers '
    + "straight into the live game, and writes author_name/author_key into "
    + "admin_corrections.json so the next rebuild reaches the same conclusion on its own "
    + "\\u2014 which is what makes the clamps disposable later instead of load-bearing "
    + "forever. <strong>author:prolific is never written this way</strong>: it counts our "
    + "own corpus and has to stay free to move.</p>";
}

function renderBookHits(host, filter, authorId){
  const f = (filter || "").toLowerCase();
  if (f.length < 2){ host.innerHTML = ""; return; }
  const hits = books.map((b, i) => ({b, i}))
    .filter(({b}) => (b.t || "").toLowerCase().includes(f)
                  || (b.a || "").toLowerCase().includes(f)).slice(0, 10);
  host.innerHTML = hits.length
    ? '<div class="scroll"><table><tbody>' + hits.map(({b, i}) =>
        '<tr><td class="title">' + esc(b.t) + "</td><td>" + esc(b.a || "\\u2014")
        + "</td><td>" + (b.y ?? "\\u2014") + '</td><td class="rich">#' + (i + 1) + "</td>"
        + '<td><button class="act ghost auLink" data-k="' + esc(b.k) + '" data-a="'
        + esc(authorId) + '">Attach</button></td></tr>').join("")
      + "</tbody></table></div>"
    : '<p class="status">No match.</p>';
}

// An id this page guessed, mapped to the one the server says the build
// computes. Cached so a second write on the same author costs no round trip,
// and so the row keeps showing its overlay under the id it is drawn with.
let auReal = {};

async function auSave(body, okMsg){
  setStatus("auStatus", "Writing…");
  // THE SERVER COMPUTES THE KEY. Same rule the "create a profile" flow
  // already states: the key has to be exactly what the build computes, and
  // this page must never hold a second copy of that rule. Only asked for
  // when the id came from padding rather than from the build — see
  // buildAuthorRows — because a build id is already the real one and asking
  // about it could only make it worse: books.json carries the DISPLAY name,
  // which display_overrides.json is allowed to rewrite, so resolving that
  // string answers about a name the corpus may not hold. For a padded row
  // the display name is the only name there is, so that limit stands; it is
  // narrower than the bug it replaces, which was every padded row.
  const shown = body.key;
  if (body.key.startsWith("name:")){
    const p = authorById[body.key];
    if (auReal[body.key]) body = Object.assign({}, body, {key: auReal[body.key]});
    else if (p && p.approx && p.name){
      const got = await post("/api/authors/resolve", {name: p.name});
      if (got && got.name_key && got.name_key !== body.key){
        auReal[shown] = got.name_key;
        body = Object.assign({}, body, {key: got.name_key});
      }
    }
  }
  const r = await post("/api/authors/save", body);
  if (r.entry) authorOverrides[r.key] = r.entry;
  else delete authorOverrides[r.key];
  // The overlay is filed under the REAL key, but the row on screen is drawn
  // under the shown one. Without this the editor would go blank the moment
  // it saved, which reads exactly like the write having failed.
  if (shown !== r.key){
    if (r.entry) authorOverrides[shown] = r.entry;
    else delete authorOverrides[shown];
  }
  buildAuthorRows();
  if (auOpenId === shown) auOpen(shown);
  renderAuthors(document.getElementById("auSearch").value);
  setStatus("auStatus", okMsg + " — effect: " + r.effect
    + (shown !== r.key ? " (written as " + r.key + ", which is what the build "
       + "computes for this name — the id shown here is this page's own "
       + "approximation for a book added since the last rebuild)" : ""), true);
  return r;
}

const auSearchBox = () => document.getElementById("auSearch");
const auRedraw = () => renderAuthors(auSearchBox().value);

document.getElementById("auSearch").addEventListener("input", auRedraw);
document.getElementById("auDupOnly").addEventListener("change", auRedraw);
document.getElementById("auOrigin").addEventListener("change", auRedraw);
document.getElementById("auPeriod").addEventListener("change", auRedraw);

// A profile BEFORE a book, which is the order Add-a-book now forces: that
// form refuses an author it does not know, so there has to be somewhere to
// make one known. Declaring writes an entry holding nothing, which changes
// no artifact at all — it only makes the name pickable and gives the facts
// somewhere to live.
document.getElementById("auNew").addEventListener("click", () => {
  const host = document.getElementById("auNewBox");
  if (host.innerHTML){ host.innerHTML = ""; return; }
  host.innerHTML = '<div class="card" style="margin:10px 0">'
    + '<div class="field"><label>The author’s name, spelled the way it should appear '
    + 'on their books</label><input type="text" id="auNewName"></div>'
    + '<button class="act" id="auNewSave">Create the profile</button>'
    + '<p class="effect">Writes an entry with no facts and no aliases, which changes no '
    + "artifact — it makes the author pickable when adding a book. Look them up in Open "
    + "Library afterwards to fill the facts in.</p></div>";
});

document.getElementById("auNewBox").addEventListener("click", async (e) => {
  if (e.target.id !== "auNewSave") return;
  const name = document.getElementById("auNewName").value.trim();
  if (!name){ setStatus("auStatus", "a name is required", false); return; }
  const existing = authorByName(name);
  if (existing){
    // Creating a second entity for a name we already hold is the exact
    // failure this tab exists to repair. Open the one that exists.
    setStatus("auStatus", "“" + name + "” is already " + existing.id
      + " — opened it instead of making a second one.", true);
    document.getElementById("auNewBox").innerHTML = "";
    auOpen(existing.id);
    auSearchBox().value = name;
    auRedraw();
    return;
  }
  // The SERVER computes the key, because the key has to be exactly what the
  // build computes and this page must never hold a second copy of that rule.
  e.target.disabled = true;
  setStatus("auStatus", "Creating…");
  try {
    const r = await post("/api/authors/resolve", {name});
    if (!r.name_key) throw new Error("that name folds to an empty key — it carries "
      + "nothing to identify an author by");
    await auSave({key: r.name_key, declare: true, facts: {}, aliases: [],
                  note: "profile created before the first book"},
      "Created " + r.name_key);
    document.getElementById("auNewBox").innerHTML = "";
    auOpen(r.name_key);
    auSearchBox().value = name;
    auRedraw();
  } catch (err) { setStatus("auStatus", String(err.message || err), false); }
  finally { e.target.disabled = false; }
});

document.getElementById("auDupes").addEventListener("click", async (e) => {
  if (!e.target.classList.contains("auMerge")) return;
  const into = e.target.dataset.into, alias = e.target.dataset.alias;
  e.target.disabled = true;
  try {
    await auSave({key: into, add_aliases: [alias], declare: true},
      "Taught “" + alias + "” as an alias of " + into);
  } catch (err) {
    setStatus("auStatus", String(err.message || err), false);
    e.target.disabled = false;
  }
});

// ONE listener for the whole table, because the editor lives inside it now.
document.getElementById("auRows").addEventListener("input", (e) => {
  if (e.target.classList.contains("auAliases")){
    auDraft.aliases = e.target.value.split("\\n").map((s) => s.trim()).filter(Boolean);
    // Only the two buttons are refreshed, never the whole panel: redrawing
    // here would take the caret out of the box being typed in.
    const card = e.target.closest(".card");
    const n = auDirtyCount();
    const save = card.querySelector(".auSave"), rev = card.querySelector(".auRevert");
    save.disabled = !n; rev.disabled = !n;
    save.textContent = n ? "Save " + n + " change" + (n === 1 ? "" : "s")
                         : "Save — nothing changed";
    return;
  }
  if (e.target.classList.contains("auBookSearch")){
    const card = e.target.closest(".card");
    renderBookHits(card.querySelector(".auBookHits"), e.target.value, card.dataset.id);
  }
});

document.getElementById("auRows").addEventListener("click", async (e) => {
  if (e.target.classList.contains("auOpen")){
    const id = e.target.dataset.id;
    const dirty = auDirtyCount();
    if (auOpenId === id){
      if (dirty && !confirm(dirty + " unsaved change(s) will be discarded. Close anyway?")) return;
      auClose();
    } else {
      if (dirty && !confirm(dirty + " unsaved change(s) on the open author will be "
          + "discarded. Switch anyway?")) return;
      auOpen(id);
    }
    auRedraw();
    return;
  }

  const card = e.target.closest(".card");
  if (!card) return;
  const id = card.dataset.id;

  // Draft edits: no network, no commit, no wait. See the note above
  // auDirtyCount for why this used to be a POST per click.
  if (e.target.classList.contains("auSet")){
    const q = e.target.dataset.q, v = e.target.dataset.v;
    if (v === "clear") delete auDraft.facts[q];
    else auDraft.facts[q] = v === "yes" ? true : v === "no" ? false : null;
    auRedraw();
    return;
  }

  if (e.target.classList.contains("auRevert")){ auOpen(id); auRedraw(); return; }

  if (e.target.classList.contains("auSave")){
    const n = auDirtyCount();
    e.target.disabled = true;
    try {
      await auSave({key: id, facts: auDraft.facts, aliases: auDraft.aliases,
                    declare: true},
        n + " change" + (n === 1 ? "" : "s") + " saved in one commit");
    } catch (err) {
      setStatus("auStatus", String(err.message || err), false);
      e.target.disabled = false;
    }
    return;
  }

  if (e.target.classList.contains("auLookup")){
    const p = authorById[id];
    e.target.disabled = true;
    setStatus("auStatus", "Searching Open Library…");
    try {
      const r = await post("/api/authors/resolve", {name: p.name || id});
      auResolveCache[id] = resolveHtml(r);
      auRedraw();
      setStatus("auStatus", r.searched
        ? (r.candidates.length + " candidate(s)")
        : "Open Library could not be reached (" + (r.search_error || "unknown")
          + ") — that is NOT the same as it having no record.", r.searched);
    } catch (err) {
      setStatus("auStatus", String(err.message || err), false);
      e.target.disabled = false;
    }
    return;
  }

  if (e.target.classList.contains("auPick")){
    const p = authorById[id];
    e.target.disabled = true;
    setStatus("auStatus", "Asking Wikidata…");
    try {
      const r = await post("/api/authors/resolve",
        {name: p.name || id, ol_key: e.target.dataset.k});
      auResolveCache[id] = resolveHtml(r);
      auRedraw();
      setStatus("auStatus", r.wikidata
        ? "Wikidata: " + r.wikidata.qid
        : (r.wikidata_error
            ? "Wikidata could not be reached (" + r.wikidata_error
              + ") — NOT the same as it knowing nothing."
            : "Wikidata has no item with that Open Library id."),
        !r.wikidata_error);
    } catch (err) {
      setStatus("auStatus", String(err.message || err), false);
      e.target.disabled = false;
    }
    return;
  }

  // Into the DRAFT, not straight to a commit. Taking Wikidata's answers is
  // a proposal the owner reviews beside everything else before one Save.
  if (e.target.classList.contains("auApplyTraits")){
    Object.assign(auDraft.facts, JSON.parse(e.target.dataset.f));
    auRedraw();
    setStatus("auStatus", "Copied into the draft — nothing is written until you press "
      + "Save.", true);
    return;
  }

  if (e.target.classList.contains("auLink")){
    const work = e.target.dataset.k, who = e.target.dataset.a;
    // Attaching uses the SAVED facts, because the server reads the committed
    // overlay. Saying so beats silently applying a stale set.
    const dirty = auDirtyCount();
    if (dirty && !confirm("This author has " + dirty + " unsaved change(s). Attaching "
        + "commits the book now and uses the SAVED facts, not the unsaved ones. Continue?")) return;
    e.target.disabled = true;
    setStatus("auStatus", "Attaching…");
    try {
      const r = await post("/api/authors/link", {key: who, work_key: work});
      const n = Object.keys(r.applied || {}).length;
      // books.json is what every other tab reads, so the rename is reflected
      // here too — otherwise the Books tab keeps showing the old author until
      // a reload.
      const row = books.find((b) => b.k === work);
      if (row) row.a = r.author;
      buildAuthorRows();
      auRedraw();
      setStatus("auStatus", "“" + (row ? row.t : work) + "” is now by "
        + r.author + " — " + (n ? n + " answer(s) live now" : "no answers to apply")
        + ". " + r.effect + " " + r.note, true);
    } catch (err) {
      setStatus("auStatus", String(err.message || err), false);
      e.target.disabled = false;
    }
  }
});

// WHY A CANDIDATE LIST AND NOT A MATCH. Searching Open Library for "John
// Gillow" returns the textile historian (19 works, top work "Indian
// textiles" — three of which this game ships) AND a different John Gillow
// who died in 1877. "Colleen Hoover" returns four records, one of them a
// French translator's name glued to hers. A name is not an identifier, and
// picking for the owner here would fan a wrong person's gender, nationality
// and dates across a whole shelf.
function resolveHtml(r){
  let html = '<p class="effect" style="margin-top:10px">The build would identify a keyless '
    + "book by this author as <code>" + esc(r.name_key || "(no usable key)") + "</code>"
    + (r.claimed_by ? " \\u2014 already claimed as an alias of <code>"
        + esc(r.claimed_by) + "</code>" : "") + ".</p>";

  if (r.candidates){
    html += r.candidates.length
      ? '<div class="scroll"><table><thead><tr><th>Open Library</th><th>Born</th>'
        + "<th>Died</th><th>Works</th><th>Best known for</th><th></th></tr></thead><tbody>"
        + r.candidates.map((c) =>
            "<tr><td>" + esc(c.name) + '<br><span class="sg-none">' + esc(c.ol_key)
            + "</span></td><td>" + esc(c.birth_date || "\\u2014") + "</td><td>"
            + esc(c.death_date || "\\u2014") + '</td><td class="rich">' + esc(c.work_count)
            + "</td><td>" + esc(c.top_work || "\\u2014")
            + '</td><td><button class="act ghost auPick" data-k="' + esc(c.ol_key)
            + '">Use this one</button></td></tr>').join("")
        + "</tbody></table></div>"
      : '<p class="sg-dupe">' + (r.searched
          ? "Open Library has no author record under that name. The <code>name:</code> key "
            + "above is then the whole identity, which is fine \\u2014 set the facts by hand."
          : "Open Library could not be reached, so this proves nothing about whether it "
            + "has a record.") + "</p>";
  }

  if (r.traits){
    const wd = r.wikidata || {};
    const set = {};
    Object.entries(r.traits).forEach(([q, v]) => {
      // author:prolific is NOT a Wikidata fact. traits_for() computes it
      // from OUR corpus — book_count >= 5 — and it is the one answer that
      // is right without any match at all. Copying it into the overlay
      // would FREEZE it: John Gillow ships three books today, so it reads
      // false, and an override saying false would still say false after
      // his fourth and fifth were added. Shown below, never copied.
      if (v !== null && q !== "author:prolific") set[q] = v;
    });
    const dates = (wd.birth || wd.death)
      ? " \\u00b7 " + esc(wd.birth || "?") + "\\u2013" + esc(wd.death || "")
      : "";
    html += '<p class="effect" style="margin-top:10px">Wikidata <code>' + esc(wd.qid || "")
      + "</code>: " + esc((wd.countries || []).join(", ") || "no nationality") + " \\u00b7 "
      + esc(wd.gender || "no gender") + dates + "</p>"
      + '<p class="sg-none">Which answers: '
      + Object.entries(r.traits).map(([q, v]) => '<span class="badge"'
          + (q === "author:prolific"
              ? ' title="Counted from our own corpus, not from Wikidata — the one answer that is right with no match at all. Not copied, because an override would freeze it as this author gains books."'
              : "")
          + ">" + esc(q) + " = " + (v === null ? "unknown" : v)
          + (q === "author:prolific" ? " (ours)" : "") + "</span>").join(" ") + "</p>"
      + (Object.keys(set).length
          ? '<button class="act auApplyTraits" data-f="'
            + esc(JSON.stringify(set)) + '">Take the ' + Object.keys(set).length
            + " answer(s) Wikidata is sure about</button>"
            + '<p class="effect">Copies them into the DRAFT above, to review beside '
            + "everything else before one Save. They beat whatever the next rebuild computes. "
            + "Everything Wikidata is silent about stays untouched \\u2014 an unmatched fact "
            + "must not become a \\u201cno\\u201d.</p>"
          : '<p class="sg-none">Wikidata is sure about nothing here, so there is nothing '
            + "to copy.</p>");
  } else if (r.wikidata_error){
    html += '<p class="sg-dupe">Wikidata could not be reached: ' + esc(r.wikidata_error)
      + ". That says nothing about the author.</p>";
  } else if (r.ol_key && r.wikidata === null){
    html += '<p class="sg-dupe">No Wikidata item carries that Open Library id. Set the '
      + "facts by hand \\u2014 they will still fan out to every book by this author.</p>";
  }
  return html;
}

// ── taught cells: the owner's hand, ahead of the 8-play floor ────────────
//
// MIN_PLAYS stays at 8 and this does not lower it. It goes around it for the
// one case the floor was never meant to catch: the floor exists because three
// players agreeing is not evidence, and the owner looking a book up is not
// three players agreeing — it is a different KIND of act. So the verdict
// writes the clamp bound directly (0.90 / 0.15), never a posterior computed
// from a handful of answers. Measured on a real cell: table says no, three
// readers say yes, the drain would have written 0.4231 — a shrug — where a
// hand verdict writes 0.90.
let taught = [];

function renderTaught(){
  const rows = document.getElementById("tgRows");
  document.getElementById("tgCount").textContent = taught.length || "";
  if (!taught.length){
    rows.innerHTML = '<tr><td colspan="6">Nothing waiting. Cells appear here when a '
      + 'player taps "Teach it this book" on the give-up screen.</td></tr>';
    return;
  }
  const byKey = {};
  books.forEach((b) => { byKey[b.k] = b; });
  const qText = {};
  questions.forEach((q) => { qText[q.id] = q.text; });

  rows.innerHTML = taught.map((c) => {
    const b = byKey[c.work_key];
    const said = Object.entries(c.tally)
      .filter(([a]) => a !== "unknown")
      .map(([a, n]) => n + "\\u00d7 " + a.replace("_", " ")).join(", ")
      + (c.unknown ? ' <span class="sg-none">(+' + c.unknown + " don\\u2019t know)</span>" : "");
    // Three states, not two: the table can also assert NOTHING about a cell,
    // and that is the case most worth teaching, so it must not read as "no".
    const table = c.matrix === true ? "yes"
      : c.matrix === false ? "no"
      : '<span class="sg-none">no record</span>';
    return "<tr" + (c.locked ? ' class="excluded"' : "") + ">"
      + '<td class="title">' + esc(b ? b.t : c.work_key)
        + (c.locked ? ' <span class="badge off">set by hand</span>' : "")
        + (c.below_floor ? ' <span class="badge">below the floor</span>' : "") + "</td>"
      + "<td>" + esc(qText[c.question_id] || c.question_id) + "</td>"
      + "<td>" + said + "</td>"
      + "<td>" + table + "</td>"
      + '<td class="rich">' + (c.drain_would_write == null ? "—" : c.drain_would_write) + "</td>"
      + '<td class="row">'
        + '<button class="act ghost tgSet" data-k="' + esc(c.work_key) + '" data-q="'
          + esc(c.question_id) + '" data-v="yes">Yes</button>'
        + '<button class="act ghost tgSet" data-k="' + esc(c.work_key) + '" data-q="'
          + esc(c.question_id) + '" data-v="no">No</button>'
        + (c.locked ? '<button class="act ghost tgSet" data-k="' + esc(c.work_key)
            + '" data-q="' + esc(c.question_id) + '" data-v="clear">Clear</button>' : "")
      + "</td></tr>";
  }).join("");
}

async function loadTaught(){
  setStatus("tgStatus", "Loading\\u2026");
  try {
    const r = await post("/api/taught", {});
    taught = r.cells || [];
    renderTaught();
    const retired = r.retired
      ? "  — " + r.retired + " more are for RETIRED questions and are not "
        + "shown: the game no longer asks them, so there is nothing to set."
      : "";
    setStatus("tgStatus", (taught.length
      ? taught.length + " cell(s) across " + r.books + " book(s); the drain writes at "
        + r.min_plays + " plays"
      : "Nothing taught yet.") + retired, true);
  } catch (err) {
    setStatus("tgStatus", "Could not read the play counts: "
      + String(err.message || err) + " — this is NOT the same as there being none.", false);
  }
}

document.getElementById("tgReload").addEventListener("click", loadTaught);

// ── the clamp audit ──────────────────────────────────────────────────────
//
// NOT LOADED WITH THE PAGE. Every other count on this page is a queue that
// grows on its own and wants a badge; this one only changes when the owner
// rebuilds the corpus, so polling it on every load would spend a Render
// wake-up on an answer that is almost always the same one.
let auditRows = [];

const AUDIT_WHY = {
  conflicts: "The table now says the OPPOSITE. One of the two is out of date "
    + "and only you can say which.",
  orphan_lock: "Held against the drain with no value behind it — it asserts "
    + "nothing and only stops the cell being learned. Safe to clear.",
  stale: "The book or the question is no longer in the shipped game, so the "
    + "lock guards a cell that is not there.",
  redundant: "The engine would answer exactly this without the clamp. Safe "
    + "to clear.",
  stronger: "Same answer, held firmer than the record alone would justify. "
    + "That is a standing assertion, not drift — left alone.",
  asserts: "The table asserts nothing here, so the clamp is the only answer. "
    + "This is what most of them are for.",
};

function renderAudit(counts){
  const host = document.getElementById("adRows");
  const shown = auditRows.filter((r) => r.verdict !== "asserts");
  const pill = document.getElementById("auditPill");
  pill.textContent = counts.conflicts ? counts.conflicts + " to look at" : "";
  if (!auditRows.length){
    host.innerHTML = '<p class="status">No clamp is held anywhere. Nothing to audit.</p>';
    return;
  }
  const state = (m) => m === true ? '<span class="sg-new">yes</span>'
    : m === false ? "no" : '<span class="sg-none">no record</span>';
  const rows = (shown.length ? shown : []).map((r) =>
    '<tr><td class="q">' + esc(r.question_id) + '<br><span class="sg-none">'
    + esc(r.work_key) + "</span></td>"
    + "<td>" + (r.clamp == null ? '<span class="sg-none">none</span>' : esc(r.clamp))
    + "</td><td>" + esc(r.without_clamp) + '<br><span class="sg-none">richness '
    + esc(r.richness == null ? "?" : r.richness) + "</span></td>"
    + "<td>" + state(r.matrix) + "</td>"
    + '<td><span class="badge' + (r.verdict === "conflicts" ? "" : " off") + '">'
    + esc(r.verdict) + "</span></td></tr>").join("");
  host.innerHTML = '<p class="status">' + esc(auditRows.length) + " held cell(s): "
    + Object.entries(counts).filter(([, n]) => n)
        .map(([k, n]) => esc(n + " " + k)).join(", ") + ".</p>"
    + (shown.length
        ? '<div class="scroll"><table class="qtable"><colgroup><col style="width:40%">'
          + '<col style="width:12%"><col style="width:18%"><col style="width:14%">'
          + '<col style="width:16%"></colgroup><thead><tr><th>Cell</th><th>Clamp</th>'
          + "<th>Without it</th><th>Table says</th><th></th></tr></thead><tbody>"
          + rows + "</tbody></table></div>"
        : '<p class="sg-none">Nothing needs attention — every held cell is answering a '
          + "question the table has no record for, which is what they are for.</p>")
    + Object.keys(AUDIT_WHY).filter((v) => counts[v])
        .map((v) => '<p class="effect"><strong>' + esc(v) + ":</strong> "
          + esc(AUDIT_WHY[v]) + "</p>").join("");
}

async function loadAudit(){
  setStatus("adStatus", "Reading the live matrix against every held cell\\u2026");
  const clear = document.getElementById("adClear");
  clear.disabled = true;
  try {
    const r = await post("/api/taught/audit", {});
    auditRows = r.cells || [];
    renderAudit(r.counts || {});
    const n = (r.clearable || []).length;
    clear.disabled = !n;
    clear.dataset.cells = JSON.stringify(r.clearable || []);
    setStatus("adStatus", auditRows.length + " held cell(s) across " + r.books
      + " book(s) \\u2014 " + (r.counts.conflicts || 0) + " conflict(s), "
      + n + " doing nothing.", true);
  } catch (err) {
    setStatus("adStatus", String(err.message || err), false);
  }
}

document.getElementById("adReload").addEventListener("click", loadAudit);

// ONE COMMIT PER BOOK, through apply_batch — the same "one decision, one
// write" the Edit tab and the Authors tab already hold, and the reason
// apply_batch exists. Only redundant and orphaned cells are ever sent: a
// conflict is a judgement and has no button here at all.
document.getElementById("adClear").addEventListener("click", async (e) => {
  const cells = JSON.parse(e.target.dataset.cells || "[]");
  if (!cells.length) return;
  const byBook = {};
  cells.forEach((c) => {
    (byBook[c.work_key] = byBook[c.work_key] || {})[c.question_id] = "clear";
  });
  const books = Object.keys(byBook);
  if (!confirm("Clear " + cells.length + " clamp(s) across " + books.length
      + " book(s)? Each one either equals what the engine already answers, or "
      + "holds a cell with no value behind it. Nothing in conflict is touched.")) return;
  e.target.disabled = true;
  let done = 0;
  try {
    for (const key of books){
      setStatus("adStatus", "Clearing " + key + "\\u2026");
      await post("/api/taught/apply_batch", {work_key: key, answers: byBook[key]});
      done += Object.keys(byBook[key]).length;
    }
    setStatus("adStatus", "Cleared " + done + " clamp(s). Re-running the audit\\u2026", true);
    await loadAudit();
  } catch (err) {
    setStatus("adStatus", "Cleared " + done + " of " + cells.length + ", then stopped: "
      + String(err.message || err) + " \\u2014 run the audit again to see what is left.", false);
    e.target.disabled = false;
  }
});

document.getElementById("tgRows").addEventListener("click", async (e)=>{
  if (!e.target.classList.contains("tgSet")) return;
  const {k, q, v} = e.target.dataset;
  const tr = e.target.closest("tr");
  tr.querySelectorAll("button").forEach(b => { b.disabled = true; });
  setStatus("tgStatus", "Writing\\u2026");
  try {
    const r = await post("/api/taught/apply", {work_key: k, question_id: q, verdict: v});
    setStatus("tgStatus", (r.value == null
      ? "Cleared — " : "Set to " + r.value + " — ") + r.note + " (" + r.effect + ")", true);
    await loadTaught();
  } catch (err) {
    setStatus("tgStatus", String(err.message || err), false);
    tr.querySelectorAll("button").forEach(b => { b.disabled = false; });
  }
});

// ── dimensions the tick-list does not offer ──────────────────────────────
//
// WHAT ACCEPTING DOES, and the label has to be honest because the button
// looks like it does more. It does NOT create a question. A question is a
// column in matrix.bin, which needs a full build_matrix.py run against a
// corpus that is gitignored and has never been on Render — and it would then
// have to cover 5% of that corpus (250 books) or the build retires it the
// same day. What accepting writes is a reviewed, committed list for whoever
// next edits features.SUBJECT_RULES by hand.
//
// The count shown is Open Library's WHOLE catalogue, not our 5,000, and the
// panel says so. A big number here is evidence the subject is real; it is
// not evidence it would clear the floor.
function renderAsks(asks){
  const host = document.getElementById("sgAsks");
  if (!asks.length){ host.innerHTML = ""; return; }
  host.innerHTML =
    '<details class="sg-stats" open><summary>Dimensions readers asked for that '
    + 'the tick-list does not offer <span class="sg-none">— ' + asks.length
    + "</span></summary>"
    + '<div class="scroll"><table><thead><tr><th>Subject</th><th>Readers asked</th>'
    + '<th title="Works catalogued under this subject in Open Library as a whole — '
    + 'NOT in our 5,000. Whether it clears the 5% question floor (250 of our books) '
    + 'can only be found by a local build.">In Open Library</th><th></th></tr></thead><tbody>'
    + asks.map((a) =>
        "<tr><td>" + esc(a.subject) + "</td>"
        + '<td class="rich">' + esc(a.asks) + "</td>"
        + '<td class="rich">' + esc(a.ol_works.toLocaleString()) + "</td>"
        + '<td class="row"><button class="act ghost askBtn" data-s="' + esc(a.subject)
        + '" data-a="1">Record it</button>'
        + '<button class="act ghost askBtn" data-s="' + esc(a.subject)
        + '" data-a="0">Dismiss</button></td></tr>').join("")
    + "</tbody></table></div>"
    + '<p class="effect">Recording does not create a question. It commits the subject '
    + "to theme_requests.json for whoever next edits features.SUBJECT_RULES — a new "
    + "question needs a full local rebuild and must then cover 250 of the 5,000 books "
    + "to survive the frequency floor.</p></details>";
}

document.getElementById("sgAsks").addEventListener("click", async (e)=>{
  if (!e.target.classList.contains("askBtn")) return;
  const subject = e.target.dataset.s, accept = e.target.dataset.a === "1";
  e.target.closest("tr").querySelectorAll("button").forEach(b => { b.disabled = true; });
  setStatus("sgStatus", accept ? "Recording…" : "Dismissing…");
  try {
    const r = await post("/api/suggestions/theme", {subject, accept});
    setStatus("sgStatus", (accept ? "Recorded — " : "Dismissed. ") + r.effect
      + (r.note ? " (" + r.note + ")" : ""), true);
    await loadSuggestions();
  } catch (err) {
    setStatus("sgStatus", String(err.message || err), false);
    await loadSuggestions();
  }
});

// ── what readers keep asking for ─────────────────────────────────────────
//
// RANKED BY WHAT STANDS OUT, NOT BY WHAT IS COMMON. A raw tick count would
// put "Fiction" on top forever — true of most books, and no answer at all to
// "which dimension are readers asking for?". Every row is therefore shown
// against the share of the shipped 5,000 that answers YES to the same
// question, and only a theme that clears all three server-side floors
// (20 reports, 5 ticks, 2x, and a Wilson lower bound above the baseline) is
// labelled over-represented.
//
// The floors matter more than they look. Measured while building this: three
// reports all ticking "web novel" produce a 95% lower bound of 0.207, which
// clears a 0.8% corpus share and printed "128x" — a headline off three
// people. Small-sample overconfidence is failure shape #5 in this project,
// and it has reversed two results already.
function renderStats(st){
  const host = document.getElementById("sgStats");
  if (!st || st.available === false){
    host.innerHTML = '<p class="sg-dupe">Tick counters unreadable right now — '
      + 'this is not the same as nobody having ticked anything.</p>';
    return;
  }
  const rows = (st.themes || []);
  if (!rows.length){ host.innerHTML = ""; return; }

  const pct = (v) => (v == null ? "—" : (v * 100).toFixed(1) + "%");
  const enough = st.reports >= 20;
  host.innerHTML =
    '<details class="sg-stats" open><summary>What readers say the missing books are about '
    + '<span class="sg-none">— ' + st.reports + " themed report"
    + (st.reports === 1 ? "" : "s") + "</span></summary>"
    + (enough ? ""
        : '<p class="sg-dupe">Fewer than 20 themed reports, so nothing here is '
          + 'called over-represented yet — the counts are real, the ratios '
          + 'would not be.</p>')
    + '<div class="scroll"><table><thead><tr><th>Theme</th><th>Ticks</th>'
    + "<th>Of reports</th><th>Of the game</th><th>Stands out</th></tr></thead><tbody>"
    + rows.map((r) =>
        "<tr><td>" + esc(r.subject || r.id) + "</td>"
        + '<td class="rich">' + esc(r.ticks) + "</td>"
        + '<td class="rich">' + pct(r.reader_share) + "</td>"
        + '<td class="rich">' + (r.corpus_share == null
            ? '<span class="sg-none" title="Not a question the shipped game asks — '
              + 'ticking it changes nothing today.">retired</span>'
            : pct(r.corpus_share)) + "</td>"
        + '<td class="rich">' + (r.over
            ? '<strong class="sg-over">' + esc(r.over) + "\\u00d7</strong>"
            : '<span class="sg-none">—</span>') + "</td></tr>").join("")
    + "</tbody></table></div>"
    + '<p class="effect">A ratio appears only when at least 20 reports and 5 ticks '
    + "support it, the share is at least double the corpus, and the 95% lower bound "
    + "still clears it. Everything else is listed without a claim.</p></details>";
}

async function loadSuggestions(){
  setStatus("sgStatus", "Loading…");
  try {
    const r = await post("/api/suggestions", {});
    suggestions = r.pending || [];
    renderSuggestions();
    // Deliberately rendered even when the queue is EMPTY: the counters
    // outlive the entries, so an empty backlog with 40 themed reports behind
    // it is exactly the state where this panel is the only thing left to
    // read. Clearing the queue must not clear what it taught.
    renderStats(r.theme_stats);
    renderAsks(r.theme_asks || []);
    setStatus("sgStatus", suggestions.length
      ? suggestions.length + " waiting" : "Queue is empty.", true);
  } catch (err) {
    // "Unreadable" and "empty" are different answers and the server is
    // careful to distinguish them (503 vs. an empty list). Do not collapse
    // them here into a reassuring blank page.
    setStatus("sgStatus", "Could not read the queue: " + String(err.message || err) +
      " — this is NOT the same as it being empty.", false);
  }
}

document.getElementById("sgReload").addEventListener("click", loadSuggestions);
document.getElementById("sgReason").addEventListener("change", renderSuggestions);
document.getElementById("sgPeriod").addEventListener("change", renderSuggestions);

document.getElementById("sgList").addEventListener("click", async (e)=>{
  if (!e.target.classList.contains("sgAct")) return;
  const id = e.target.dataset.id, action = e.target.dataset.action;
  const card = e.target.closest(".sg");
  card.querySelectorAll("button").forEach(b => { b.disabled = true; });
  setStatus("sgStatus", action === "reject" ? "Dismissing…" : "Applying…");
  try {
    const r = await post("/api/suggestions/resolve", {id, action});
    suggestions = suggestions.filter(s => s.id !== id);
    renderSuggestions();
    const effect = (r.result && r.result.effect) ? " — effect: " + r.result.effect : "";
    setStatus("sgStatus", (action === "reject" ? "Dismissed." : "Applied.") + effect +
      (r.removed === false ? " (committed, but it could not be cleared from the "
        + "queue — it will reappear on the next refresh)" : ""), true);
  } catch (err) {
    setStatus("sgStatus", String(err.message || err), false);
    card.querySelectorAll("button").forEach(b => { b.disabled = false; });
  }
});

// ── mined questions ───────────────────────────────────────────────────────
//
// FETCHED DIRECTLY, NOT THROUGH A RELAY — same as books.json/questions.json.
// propose_questions.py writes this file straight into the bookhub repo, so
// it is already a public artifact the moment it is committed; only the
// ACCEPT/REJECT verdict needs write access, which is what /api/candidates
// /decide is for. A 404 is the normal state until a local run has produced
// one, same convention excluded.json/overrides.json already use.
let candidates = [];

function renderCandidates(){
  const host = document.getElementById("cqList");
  const pending = candidates.filter((c) => c.status === "pending");
  document.getElementById("cqCount").textContent = pending.length || "";
  if (!candidates.length){
    host.innerHTML = '<p class="status">Nothing yet. Run '
      + '<code>python scripts/akinator/propose_questions.py</code> locally — '
      + "it writes straight into this file.</p>";
    return;
  }
  if (!pending.length){
    host.innerHTML = '<p class="status">Nothing pending — '
      + (candidates.length) + " candidate(s) already decided.</p>";
    return;
  }

  host.innerHTML = pending.map((c) => {
    const measured = c.measured || {};
    const band = measured.passes_band
      ? '<span class="sg-new">clears the 5–60% floor</span>'
      : '<span class="sg-over">below the floor — would be dropped at build time</span>';
    const sampleNote = measured.measured_on === "sample"
      ? ' <span class="sg-none">(sample of ' + esc(measured.sample_size) + ', not the whole corpus — '
        + "prose candidates have no keyword shortcut to measure exactly)</span>"
      : ' <span class="sg-none">(exact, full shipped corpus, ' + esc(measured.sample_size) + " books)</span>";
    const freq = ((measured.frequency || 0) * 100).toFixed(1) + "%";

    const examples = (label, list) => (list && list.length)
      ? '<div class="sg-themes">' + esc(label) + ": "
        + list.map((t) => '<span class="badge">' + esc(t) + "</span>").join(" ") + "</div>"
      : "";

    const coll = c.collision || {};
    let warn = "";
    if (coll.exact_wording_collision){
      warn += '<p class="sg-dupe sg-dupe--sure">EXACT WORDING COLLISION with <code>'
        + esc(coll.exact_wording_collision) + "</code> — a player would be asked the same "
        + "question twice. Do not add this as written.</p>";
    }
    if (coll.near_duplicate && coll.near_duplicate.length){
      warn += '<p class="sg-dupe">Agrees with an existing question on most sampled books — '
        // SINGLE quotes, and the whole admin page depended on it. A
        // backslash-quote here is an escape the TEMPLATE LITERAL eats, so
        // the emitted client script carried a bare class="sg-near" inside
        // a double-quoted string — a syntax error, which in an inline
        // script kills the ENTIRE block. Every tab at once, since the
        // Mined questions tab shipped. Running node --check on this file
        // cannot see it: the client script is a string here and only
        // becomes JavaScript after page() runs, so the real check is to
        // extract the script body from page() and --check that.
        + 'it may tell the engine almost nothing new:</p><ul class="sg-near">'
        + coll.near_duplicate.map((n) => "<li><code>" + esc(n.id) + "</code> — "
          + Math.round(n.agreement * 100) + "% agreement over " + esc(n.n) + " books</li>").join("")
        + "</ul>";
    }

    const draft = c.draft_rule || c.draft_entry || "";

    return '<div class="sg" data-id="' + esc(c.id) + '">' +
      '<div class="sg-head"><span class="sg-reason">' + esc(c.type) + "</span>" +
        '<code>' + esc(c.key) + "</code>" +
        '<span class="sg-when">generated ' + esc(c.generated) + "</span></div>" +
      '<dl class="sg-cmp"><dt>Question</dt><dd>' + esc(c.question) + "</dd>" +
        "<dt>Frequency</dt><dd>" + freq + " — " + band + sampleNote + "</dd>" +
        "<dt>Cost</dt><dd>" + esc(c.byte_cost || "") + "</dd></dl>" +
      examples("matches", measured.example_present) +
      examples("does not match", measured.example_absent) +
      warn +
      (c.rationale ? '<p class="sg-none">' + esc(c.rationale) + "</p>" : "") +
      '<pre class="cq-draft">' + esc(draft) + "</pre>" +
      '<div class="row">' +
        '<button class="act ghost cqCopy" data-draft="' + esc(draft) + '">Copy rule</button>' +
        '<button class="act cqAct" data-id="' + esc(c.id) + '" data-status="accepted">Accept</button>' +
        '<button class="act ghost cqAct" data-id="' + esc(c.id) + '" data-status="rejected">Reject</button>' +
      "</div></div>";
  }).join("");
}

async function loadCandidates(){
  setStatus("cqStatus", "Loading…");
  try {
    const r = await fetch(DATA + "/question_candidates.json");
    candidates = r.ok ? await r.json() : [];
    if (!Array.isArray(candidates)) candidates = [];
    renderCandidates();
    const pending = candidates.filter((c) => c.status === "pending").length;
    setStatus("cqStatus", pending ? pending + " waiting" : "Nothing pending.", true);
  } catch (err) {
    setStatus("cqStatus", "Could not read question_candidates.json: " + String(err.message || err), false);
  }
}

document.getElementById("cqReload").addEventListener("click", loadCandidates);

document.getElementById("cqList").addEventListener("click", async (e)=>{
  if (e.target.classList.contains("cqCopy")){
    try { await navigator.clipboard.writeText(e.target.dataset.draft); } catch (err) { /* clipboard permission denied — nothing to fall back to on an admin-only page */ }
    return;
  }
  if (!e.target.classList.contains("cqAct")) return;
  const id = e.target.dataset.id, status = e.target.dataset.status;
  const card = e.target.closest(".sg");
  card.querySelectorAll("button").forEach(b => { b.disabled = true; });
  setStatus("cqStatus", status === "rejected" ? "Rejecting…" : "Accepting…");
  try {
    await post("/api/candidates/decide", {id, status});
    const c = candidates.find((x) => x.id === id);
    if (c) c.status = status;
    renderCandidates();
    setStatus("cqStatus", status === "rejected" ? "Rejected." :
      "Accepted — paste the rule into features.py/traits.py by hand, then rebuild when ready.", true);
  } catch (err) {
    setStatus("cqStatus", String(err.message || err), false);
    card.querySelectorAll("button").forEach(b => { b.disabled = false; });
  }
});

// ── Fandom: candidates the game proved, waiting to be trusted live ──────
//
// NOT LOADED WITH THE PAGE, same reasoning as the clamp audit: this only
// changes when the game's offline discovery pipeline finds something new,
// which is rare — polling it on every page load would spend a Render
// wake-up on an answer that is almost always identical to last time.
let fdCandidates = [];

function renderFandom(){
  const host = document.getElementById("fdList");
  document.getElementById("fdCount").textContent = fdCandidates.length || "";
  if (!fdCandidates.length){
    host.innerHTML = '<p class="status">Nothing waiting \\u2014 the summarizer\\u2019s live map already '
      + "covers everything the game has proven.</p>";
    return;
  }
  host.innerHTML = fdCandidates.map((c) => {
    const seed = c.seed_title ? esc(c.seed_title) : "";
    return '<div class="sg" data-sub="' + esc(c.subdomain) + '">'
      + '<div class="sg-head"><span class="sg-reason"><code>' + esc(c.subdomain) + "</code>"
      + (seed ? " \\u2014 " + seed : "") + "</span></div>"
      + (c.why ? '<p class="sg-none">' + esc(c.why) + "</p>" : "")
      // One per line, not comma-separated — see the Authors editor's aliases
      // field for why: a title that itself contains a comma (a subtitle, a
      // translated variant with punctuation) would otherwise be silently
      // torn in two on save.
      + '<div class="field"><label>Aliases, one per line (the titles/spellings a reader might type)</label>'
      + '<textarea class="fdAliases" rows="2">' + esc(seed) + '</textarea></div>'
      + '<div class="row">'
      + '<div class="field" style="flex:1"><label>Author (optional \\u2014 leave blank unless you verified it '
      + "on the wiki; a wrong author here poisons every book this resolver enriches for them, not just this "
      + 'one)</label><input type="text" class="fdAuthor"></div>'
      + '<div class="field" style="flex:1"><label>Cover URL (optional)</label>'
      + '<input type="text" class="fdCover"></div>'
      + "</div>"
      + '<div class="row" style="margin-top:8px">'
      + '<a class="act ghost" href="https://' + esc(c.subdomain) + '.fandom.com/" target="_blank" rel="noopener">Open the wiki</a>'
      + '<button class="act fdApprove">Approve \\u2014 write to wikis.json</button>'
      + '<button class="act ghost fdReject">Reject</button>'
      + "</div></div>";
  }).join("");
}

async function loadFandom(){
  setStatus("fdStatus", "Loading\\u2026");
  try {
    const r = await post("/api/fandom/candidates", {});
    fdCandidates = r.candidates || [];
    renderFandom();
    setStatus("fdStatus", fdCandidates.length
      ? fdCandidates.length + " waiting, " + r.live_count + " already live"
      : "Nothing waiting (" + r.live_count + " live).", true);
  } catch (err) {
    setStatus("fdStatus", String(err.message || err), false);
  }
}

document.getElementById("fdReload").addEventListener("click", loadFandom);

document.getElementById("fdList").addEventListener("click", async (e) => {
  const card = e.target.closest(".sg");
  if (!card) return;
  const sub = card.dataset.sub;

  if (e.target.classList.contains("fdReject")){
    if (!confirm("Reject " + sub + "? This drops it from the queue \\u2014 the game would have to "
        + "rediscover it to see it again.")) return;
    card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    setStatus("fdStatus", "Rejecting\\u2026");
    try {
      await post("/api/fandom/candidates/reject", {subdomain: sub});
      fdCandidates = fdCandidates.filter((c) => c.subdomain !== sub);
      renderFandom();
      setStatus("fdStatus", "Rejected " + sub + ".", true);
    } catch (err) {
      setStatus("fdStatus", String(err.message || err), false);
      card.querySelectorAll("button").forEach((b) => { b.disabled = false; });
    }
    return;
  }

  if (e.target.classList.contains("fdApprove")){
    const aliases = card.querySelector(".fdAliases").value.split("\\n")
      .map((s) => s.trim()).filter(Boolean);
    const author = card.querySelector(".fdAuthor").value.trim();
    const cover_url = card.querySelector(".fdCover").value.trim();
    if (!aliases.length && !confirm(sub + " has no aliases typed \\u2014 it will only ever match a "
        + "search for the bare subdomain. Approve anyway?")) return;
    card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    setStatus("fdStatus", "Approving " + sub + "\\u2026");
    try {
      const r = await post("/api/fandom/candidates/approve", {
        subdomain: sub, aliases, author: author || null, cover_url: cover_url || null,
      });
      fdCandidates = fdCandidates.filter((c) => c.subdomain !== sub);
      renderFandom();
      setStatus("fdStatus", "Approved " + sub + " \\u2014 " + r.effect, true);
    } catch (err) {
      setStatus("fdStatus", String(err.message || err), false);
      card.querySelectorAll("button").forEach((b) => { b.disabled = false; });
    }
  }
});

(async function init(){
  try {
    // authors.json and author_overrides.json join the first paint rather
    // than the deferred batch below: the Authors tab's duplicate count is a
    // nav badge, and a badge that appears a second late is a badge nobody
    // sees. A 404 on the overlay is the normal state until something has
    // been written, same convention as excluded.json/overrides.json.
    const [b, q, x, a, ao] = await Promise.all([
      fetch(DATA+"/books.json").then(r=>r.json()),
      fetch(DATA+"/questions.json").then(r=>r.json()),
      fetch(DATA+"/excluded.json").then(r=>r.ok?r.json():[]).catch(()=>[]),
      fetch(DATA+"/authors.json").then(r=>r.ok?r.json():{}).catch(()=>({})),
      fetch(DATA+"/author_overrides.json").then(r=>r.ok?r.json():{}).catch(()=>({})),
    ]);
    books = b; questions = q; excluded = new Set(x);
    authorsData = a && typeof a === "object" ? a : {};
    authorOverrides = ao && typeof ao === "object" ? ao : {};
    // The matrix and the override file, for the Edit tab only. 63 KB and a
    // small JSON on an admin page nobody loads casually — and without them
    // the editor would be setting cells blind, which is how you write an
    // override that changes nothing. A 404 on overrides.json is normal:
    // it does not exist until something has been written.
    Promise.all([
      fetch(DATA + "/matrix.bin").then(r => r.ok ? r.arrayBuffer() : null),
      fetch(DATA + "/meta.json").then(r => r.json()),
      fetch(DATA + "/overrides.json").then(r => r.ok ? r.json() : {}).catch(() => ({})),
      fetch(DATA + "/admin_corrections.json").then(r => r.ok ? r.json() : {}).catch(() => ({})),
    ]).then(([mb, mt, ov, co]) => {
      if (mb) matrix = new Uint8Array(mb);
      meta = mt; overrides = ov || {}; corrections = co || {};
    }).catch(() => {});
    computeDupFlags();
    renderBooks(""); renderQuestions();
    buildAuthorRows(); renderAuthors("");
  } catch (e) {
    setStatus("bookStatus", "failed to load live artifacts: "+e.message, false);
  }
  // Deliberately last, deliberately not awaited above, and deliberately not
  // deferred to the tab click: the count badge is the only way the owner
  // learns something is waiting without going looking. It also warms Render,
  // which sleeps after ~15 minutes and takes 30-60s to answer the first
  // request — better spent while the books table is being read than while
  // the owner stares at the Suggestions tab.
  loadSuggestions();
  loadTaught();
  loadCandidates();
})();
</script>
</body></html>`;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (!request.headers.get(ACCESS_HEADER)) {
      return new Response(
        "This page is served only behind Cloudflare Access.\n" +
        "No Access assertion was present on this request, so nothing is shown.\n",
        { status: 403, headers: { "Content-Type": "text/plain; charset=utf-8" } },
      );
    }

    if (url.pathname === "/" && request.method === "GET") {
      return new Response(page(), {
        headers: {
          "Content-Type": "text/html; charset=utf-8",
          "Cache-Control": "no-store",
          "X-Robots-Tag": "noindex, nofollow",
        },
      });
    }

    const handler = request.method === "POST" ? ROUTES[url.pathname] : null;
    if (handler) return handler(request, env);

    return new Response("not found", { status: 404 });
  },
};
