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

const ROUTES = {
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
  "/api/suggestions": relay("/akinator/admin/suggestions"),
  "/api/suggestions/resolve": relay("/akinator/admin/suggestions/resolve"),
  "/api/suggestions/theme": relay("/akinator/admin/suggestions/theme"),
  "/api/taught": relay("/akinator/admin/taught"),
  "/api/taught/apply": relay("/akinator/admin/taught/apply"),
  // Verdicts on scripts/akinator/propose_questions.py's mined candidates —
  // the candidates themselves are read straight off question_candidates.json
  // by the client, same as books.json/questions.json; only the decision
  // needs a write, hence one relay rather than two.
  "/api/candidates/decide": relay("/akinator/admin/candidates/decide"),
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
.badge{font-size:11px;font-weight:600;padding:1px 7px;border-radius:3px;background:var(--line)}
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
  <button data-tab="edit">Edit a book</button>
</nav>

<section id="books" class="on">
  <input type="search" id="bookSearch" placeholder="Search by title or author…">
  <div class="toolbar">
    <label><input type="checkbox" id="dupOnly"> Only duplicate candidates (<span id="dupCount">0</span>)</label>
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

<section id="add">
  <div class="card">
    <div class="field"><label>Title</label><input type="text" id="addTitle"></div>
    <div class="field"><label>Author</label><input type="text" id="addAuthor"></div>
    <div class="field"><label>First published (optional — leave blank and Open Library's work-level year is used;
      six era questions read this, and a wrong year is worse than none)</label>
      <input type="text" id="addYear" inputmode="numeric" style="width:110px"></div>
    <div class="field"><label>Summary (grounds the theme labels — the richer this is, the more the book can answer)</label>
      <textarea id="addSummary"></textarea></div>
    <div class="field"><label>Themes, comma-separated (optional)</label><input type="text" id="addThemes"></div>
    <button class="act" id="addSubmit">Add book</button>
    <p class="status" id="addStatus"></p>
  </div>
</section>

<section id="edit">
  <p class="sub" style="margin-bottom:14px">Everything about one book on one screen. Title and author change what the
  reveal PRINTS and take effect as soon as GitHub Pages redeploys. Each question below writes the same verified bound a
  catalogued fact gets (0.90 / 0.15) straight into overrides.json, and is then held against the nightly drain until you
  clear it. The year is the exception and says so: it feeds a matrix bit only a full rebuild recomputes.</p>
  <input type="search" id="edSearch" placeholder="Find a book by title or author…">
  <div id="edPick"></div>
  <div id="edPanel"></div>
  <p class="status" id="edStatus"></p>
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
</section>

<section id="suggestions">
  <p class="sub" style="margin-bottom:14px">Reported by readers on the give-up screen, and verified against
  Google Books / Open Library before reaching this queue — a book that could not be found never arrives here.
  Nothing a reader typed is stored: the title below is the catalogue's own, looked up from what they typed.
  Approving runs the same action as doing it by hand on the other tabs.</p>
  <div class="row" style="margin-bottom:12px">
    <button class="act ghost" id="sgReload">Refresh</button>
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

<footer>Verified against Google Books / Open Library before a row is added — this page cannot invent a book.
Excluding, adding and rewording reach players as soon as GitHub Pages redeploys the commit (about a minute) — no rebuild needed.
A fact correction (year) reaches nobody until the next full <code>build_matrix.py</code> run: it feeds a matrix bit only that recomputes.</footer>
</div>
<script>
${ESC_FN}
const DATA = "https://litheca.com/games/data/akinator";
let books = [], questions = [], excluded = new Set(), dupFlag = [];

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
  const matching = books
    .map((b,i)=>({b,i}))
    .filter(({b,i}) => (!dupOnly || dupFlag[i])
      && (!f || (b.t||"").toLowerCase().includes(f) || (b.a||"").toLowerCase().includes(f)));
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
}

document.getElementById("bookSearch").addEventListener("input", (e)=>renderBooks(e.target.value));
document.getElementById("dupOnly").addEventListener("change", ()=>
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

document.getElementById("addSubmit").addEventListener("click", async ()=>{
  const title = document.getElementById("addTitle").value.trim();
  const author = document.getElementById("addAuthor").value.trim();
  const summary = document.getElementById("addSummary").value.trim();
  const themes = document.getElementById("addThemes").value.split(",").map(s=>s.trim()).filter(Boolean);
  const rawYear = document.getElementById("addYear").value.trim();
  if (!title || !author) { setStatus("addStatus", "title and author are required", false); return; }
  if (rawYear && !/^\\d{1,4}$/.test(rawYear)) {
    setStatus("addStatus", "year must be digits only, or blank", false); return;
  }
  const btn = document.getElementById("addSubmit");
  btn.disabled = true;
  setStatus("addStatus", "Verifying and adding…");
  try {
    const r = await post("/api/book",
      {title, author, summary, themes, year: rawYear ? parseInt(rawYear, 10) : null});
    setStatus("addStatus", \`Added "\${r.title}" by \${r.author}\${r.year?(" ("+r.year+")"):" (no year — era questions answer \\u201cunknown\\u201d)"} — effect: \${r.effect}\`, true);
    document.getElementById("addTitle").value = "";
    document.getElementById("addAuthor").value = "";
    document.getElementById("addYear").value = "";
    document.getElementById("addSummary").value = "";
    document.getElementById("addThemes").value = "";
  } catch (err) { setStatus("addStatus", String(err.message||err), false); }
  finally { btn.disabled = false; }
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
};

// Which resolutions each reason offers. Mirrors _ALLOWED in
// tools/akinator_suggest.py — the server refuses anything else with a 400,
// so this list decides what is OFFERED, never what is permitted.
const REASON_ACTIONS = {
  missing: [["book", "Add this book", "act"]],
  wrong_year: [["correction", "Apply the year fix", "act"]],
  unreadable: [["display", "Rename for display", "act"],
               ["exclude", "Exclude the row instead", "act danger"]],
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
  if (!suggestions.length){
    host.innerHTML = '<p class="status">Nothing pending. Reports arrive from the ' +
      'give-up screen when a reader taps one of the three buttons there.</p>';
    return;
  }
  const byKey = {};
  books.forEach((b) => { byKey[b.k] = b; });

  host.innerHTML = suggestions.map((s) => {
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
      // it was invisible until it was already committed.
      if (s.reason === "missing"){
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

    // Duplicate hints, for a "missing" claim only — the other two reasons
    // already name a row.
    let dupes = "";
    if (s.reason === "missing"){
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
      ? '<span class="badge">reported ' + esc(s.votes) + " times</span>" : "";
    const buttons = (REASON_ACTIONS[s.reason] || []).map(([action, label, cls]) =>
      '<button class="' + cls + ' sgAct" data-id="' + esc(s.id) + '" data-action="' +
      action + '">' + label + "</button>").join("");

    return '<div class="sg" data-id="' + esc(s.id) + '">' +
      '<div class="sg-head"><span class="sg-reason">' +
        esc(REASON_LABEL[s.reason] || s.reason) + "</span>" + votes +
        '<span class="sg-when">first seen ' + esc(when(s.first_seen)) +
        ((s.votes || 1) > 1 ? ", last " + esc(when(s.last_seen)) : "") + "</span></div>" +
      '<dl class="sg-cmp"><dt>Current</dt><dd>' + current + "</dd>" +
        "<dt>Reader</dt><dd>" + proposed + "</dd></dl>" + dupes +
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

function cellState(bookIndex, qIndex){
  if (!matrix || !meta) return null;
  const off = bookIndex * meta.bytes_per_row;
  return (matrix[off + (qIndex >> 2)] >> ((qIndex & 3) * 2)) & 3;
}

function renderEditPanel(i){
  const b = books[i], host = document.getElementById("edPanel");
  const ov = overrides[b.k] || {};
  const rows = questions.map((q, qi) => {
    const st = cellState(i, qi);
    // Three states, and "unknown" is NOT "no". The table asserting nothing
    // is the case most worth editing, so it must not read as a denial.
    const table = st === 1 ? '<span class="sg-new">yes</span>'
      : st === 0 ? "no"
      : '<span class="sg-none">no record</span>';
    // A PROBABILITY IS NOT AN ANSWER. This read "set by hand: 0.9", which
    // tells the owner what got written and not what the game now believes —
    // and next time they open the book they cannot tell whether they had
    // said yes or no. 0.90 and 0.15 are the two clamp bounds a hand verdict
    // writes, so they map back to yes and no exactly; anything else came
    // from the drain and IS a probability, so it is labelled as learned
    // rather than dressed up as a verdict.
    const o = ov[q.id];
    let cur = "";
    if (o !== undefined) {
      cur = o >= 0.9 ? '<span class="badge off">set by hand: YES</span>'
          : o <= 0.15 ? '<span class="badge off">set by hand: NO</span>'
          : '<span class="badge">learned from play: ' + esc(o) + "</span>";
      cur += ' <span class="sg-none">(' + esc(o) + ")</span>";
    }
    return "<tr><td>" + esc(q.text) + '<br><span class="sg-none">' + esc(q.id) + "</span></td>"
      + "<td>" + table + "</td><td>" + cur + "</td>"
      + '<td class="row">'
      + '<button class="act ghost edSet" data-q="' + esc(q.id) + '" data-v="yes">Yes</button>'
      + '<button class="act ghost edSet" data-q="' + esc(q.id) + '" data-v="no">No</button>'
      + (o === undefined ? ""
         : '<button class="act ghost edSet" data-q="' + esc(q.id) + '" data-v="clear">Clear</button>')
      + "</td></tr>";
  }).join("");

  host.innerHTML =
    '<div class="card" style="max-width:none;margin-bottom:14px" data-key="' + esc(b.k) + '">'
    + '<div class="field"><label>Title shown to the player</label>'
      + '<input type="text" id="edTitle" value="' + esc(b.t) + '"></div>'
    + '<div class="field"><label>Author shown to the player</label>'
      + '<input type="text" id="edAuthor" value="' + esc(b.a || "") + '"></div>'
    + '<button class="act" id="edSaveName">Save title and author</button>'
    + '<p class="effect">Changes only what the reveal prints. No question and no '
    + "matrix bit reads these.</p>"
    + '<div class="field" style="margin-top:16px"><label>First published '
      + "(feeds six era questions)</label>"
      + '<input type="text" id="edYear" inputmode="numeric" style="width:120px" value="'
      + esc(b.y == null ? "" : b.y) + '"></div>'
    + '<button class="act ghost" id="edSaveYear">Queue the year</button>'
    + '<p class="effect">NOT instant. A year feeds a matrix bit that only a local '
    + "build_matrix.py run recomputes — everything else on this page is live.</p></div>"
    + '<div class="scroll"><table><thead><tr><th>Question</th><th>Table says</th>'
    + "<th>Override</th><th></th></tr></thead><tbody>" + rows + "</tbody></table></div>";

  document.getElementById("edSaveName").addEventListener("click", async () => {
    const title = document.getElementById("edTitle").value.trim();
    const author = document.getElementById("edAuthor").value.trim();
    if (!title) { setStatus("edStatus", "a title cannot be blank", false); return; }
    try {
      const r = await post("/api/display", {work_key: b.k, title, author});
      books[i].t = title; books[i].a = author;
      setStatus("edStatus", "Renamed for display — " + r.effect, true);
    } catch (err) { setStatus("edStatus", String(err.message || err), false); }
  });

  document.getElementById("edSaveYear").addEventListener("click", async () => {
    const raw = document.getElementById("edYear").value.trim();
    if (!/^\\d{1,4}$/.test(raw)) { setStatus("edStatus", "a year, digits only", false); return; }
    try {
      const r = await post("/api/correction",
        {work_key: b.k, field: "first_publish_year", value: parseInt(raw, 10)});
      setStatus("edStatus", "Year queued — " + r.effect, true);
    } catch (err) { setStatus("edStatus", String(err.message || err), false); }
  });

  host.querySelector(".scroll").addEventListener("click", async (e) => {
    if (!e.target.classList.contains("edSet")) return;
    const q = e.target.dataset.q, v = e.target.dataset.v;
    e.target.closest("tr").querySelectorAll("button").forEach(x => { x.disabled = true; });
    setStatus("edStatus", "Writing\\u2026");
    try {
      const r = await post("/api/taught/apply", {work_key: b.k, question_id: q, verdict: v});
      if (r.value == null) { delete (overrides[b.k] || {})[q]; }
      else { (overrides[b.k] = overrides[b.k] || {})[q] = r.value; }
      setStatus("edStatus", (r.value == null ? "Cleared \\u2014 " : "Set to " + r.value + " \\u2014 ")
        + r.note, true);
      renderEditPanel(i);
    } catch (err) {
      setStatus("edStatus", String(err.message || err), false);
      renderEditPanel(i);
    }
  });
}

document.getElementById("edSearch").addEventListener("input", (e) => {
  const f = (e.target.value || "").toLowerCase();
  const host = document.getElementById("edPick");
  if (f.length < 2) { host.innerHTML = ""; return; }
  const hits = books.map((b, i) => ({b, i}))
    .filter(({b}) => (b.t || "").toLowerCase().includes(f)
                  || (b.a || "").toLowerCase().includes(f)).slice(0, 12);
  host.innerHTML = hits.length
    ? '<div class="scroll"><table><tbody>' + hits.map(({b, i}) =>
        '<tr><td class="title">' + esc(b.t) + "</td><td>" + esc(b.a || "\\u2014")
        + "</td><td>" + (b.y ?? "\\u2014") + '</td><td><button class="act ghost edPickBtn" '
        + 'data-i="' + i + '">Edit</button></td></tr>').join("") + "</tbody></table></div>"
    : '<p class="status">No match.</p>';
});

document.getElementById("edPick").addEventListener("click", (e) => {
  if (!e.target.classList.contains("edPickBtn")) return;
  document.getElementById("edPick").innerHTML = "";
  renderEditPanel(+e.target.dataset.i);
});

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

(async function init(){
  try {
    const [b, q, x] = await Promise.all([
      fetch(DATA+"/books.json").then(r=>r.json()),
      fetch(DATA+"/questions.json").then(r=>r.json()),
      fetch(DATA+"/excluded.json").then(r=>r.ok?r.json():[]).catch(()=>[]),
    ]);
    books = b; questions = q; excluded = new Set(x);
    // The matrix and the override file, for the Edit tab only. 63 KB and a
    // small JSON on an admin page nobody loads casually — and without them
    // the editor would be setting cells blind, which is how you write an
    // override that changes nothing. A 404 on overrides.json is normal:
    // it does not exist until something has been written.
    Promise.all([
      fetch(DATA + "/matrix.bin").then(r => r.ok ? r.arrayBuffer() : null),
      fetch(DATA + "/meta.json").then(r => r.json()),
      fetch(DATA + "/overrides.json").then(r => r.ok ? r.json() : {}).catch(() => ({})),
    ]).then(([mb, mt, ov]) => {
      if (mb) matrix = new Uint8Array(mb);
      meta = mt; overrides = ov || {};
    }).catch(() => {});
    computeDupFlags();
    renderBooks(""); renderQuestions();
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
