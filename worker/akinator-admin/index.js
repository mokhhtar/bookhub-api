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
</style></head><body><div class="wrap">
<h1>Book Mind Reader — admin</h1>
<p class="sub">Every action here commits directly to the live game. Nothing is automatic — nothing applies without you clicking it.</p>

<nav>
  <button data-tab="books" class="on">Books</button>
  <button data-tab="questions">Questions</button>
  <button data-tab="add">Add a book</button>
  <button data-tab="suggestions">Suggestions <span class="pill" id="sgCount"></span></button>
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

<section id="suggestions">
  <p class="sub" style="margin-bottom:14px">Reported by readers on the give-up screen, and verified against
  Google Books / Open Library before reaching this queue — a book that could not be found never arrives here.
  Nothing a reader typed is stored: the title below is the catalogue's own, looked up from what they typed.
  Approving runs the same action as doing it by hand on the other tabs.</p>
  <div class="row" style="margin-bottom:12px">
    <button class="act ghost" id="sgReload">Refresh</button>
    <span class="status" id="sgStatus" style="margin:0"></span>
  </div>
  <div id="sgList"></div>
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

async function loadSuggestions(){
  setStatus("sgStatus", "Loading…");
  try {
    const r = await post("/api/suggestions", {});
    suggestions = r.pending || [];
    renderSuggestions();
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

(async function init(){
  try {
    const [b, q, x] = await Promise.all([
      fetch(DATA+"/books.json").then(r=>r.json()),
      fetch(DATA+"/questions.json").then(r=>r.json()),
      fetch(DATA+"/excluded.json").then(r=>r.ok?r.json():[]).catch(()=>[]),
    ]);
    books = b; questions = q; excluded = new Set(x);
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
