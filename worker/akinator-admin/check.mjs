/**
 * worker/akinator-admin/check.mjs — what `node --check index.js` cannot see.
 *
 *     node worker/akinator-admin/check.mjs
 *
 * THE BUG THIS EXISTS FOR, found 2026-08-24 and live at the time. The admin
 * page's whole client is a STRING inside `page()`'s template literal, so
 * `node --check index.js` only proves the string is well-formed — never that
 * what it emits is valid JavaScript. A `\"` written inside that literal is an
 * escape the template eats, and one of them shipped with the Mined questions
 * tab:
 *
 *     + "…almost nothing new:</p><ul class=\"sg-near\">"
 *
 * emitted `class="sg-near"` inside a double-quoted string. A syntax error in
 * an inline script kills the ENTIRE block, so every tab on the page — books,
 * questions, suggestions, all of it — had been dead since that deploy, and
 * nothing in the repo or in CI could tell. The deploy workflow's own guard
 * could not either: it checks that the page REFUSES an unauthenticated
 * request, which a broken page does perfectly well.
 *
 * THREE CHECKS, and each one is a failure this file has actually had:
 *
 *   1. the emitted client script parses          (the bug above)
 *   2. every /api/… the client calls is in ROUTES
 *      — `display` shipped calling a relay that did not exist, so Rename
 *        posted into a 404 silently for weeks
 *   3. every tab button has a section to show
 *
 * The deploy workflow also derives its guard list FROM `ROUTES` rather than
 * repeating it, so the fourth failure mode — a relay nothing proves is
 * guarded — cannot come back either. Pass --relays to print that list.
 */
import fs from "node:fs";
import vm from "node:vm";

const here = new URL(".", import.meta.url);
const src = fs.readFileSync(new URL("index.js", here), "utf8");

// ROUTES and page() are module-private, so read them out of a copy that
// exports them rather than making the real file export test hooks.
const tmp = new URL("_check_tmp.mjs", here);
fs.writeFileSync(tmp, src + "\nexport { page as __page, ROUTES as __routes };\n");
let page, ROUTES;
try {
  ({ __page: page, __routes: ROUTES } = await import(tmp.href + "?t=" + Date.now()));
} finally {
  fs.rmSync(tmp, { force: true });
}

const relays = Object.keys(ROUTES);
if (process.argv.includes("--relays")) {
  // Consumed by .github/workflows/deploy-akinator-admin-worker.yml, so the
  // guard loop and ROUTES are one list and cannot drift apart.
  console.log(relays.map((p) => p.replace(/^\/api\//, "")).join(" "));
  process.exit(0);
}

const fail = [];
const html = page();

// 1. the emitted client script must be real JavaScript
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) {
  fail.push("no <script> block in the emitted page");
} else {
  try {
    new vm.Script(m[1], { filename: "emitted-client.js" });
    console.log(`ok  emitted client script parses (${m[1].length} chars)`);
  } catch (e) {
    fail.push(`emitted client script does NOT parse: ${e.message}`);
  }
}

// 2. every relay the client calls must exist in ROUTES
const called = [...html.matchAll(/["'](\/api\/[a-z/]+)["']/g)].map((x) => x[1]);
const missing = [...new Set(called)].filter((p) => !relays.includes(p));
if (missing.length) fail.push(`client calls relays absent from ROUTES: ${missing.join(", ")}`);
else console.log(`ok  ${new Set(called).size} relay(s) called, all present in ROUTES`);

const unused = relays.filter((p) => !called.includes(p));
if (unused.length) console.log(`    note: in ROUTES but never called: ${unused.join(", ")}`);

// 3. every tab must have a section
const tabs = [...html.matchAll(/data-tab="([a-z]+)"/g)].map((x) => x[1]);
const secs = [...html.matchAll(/<section id="([a-z]+)"/g)].map((x) => x[1]);
const orphan = tabs.filter((t) => !secs.includes(t));
if (orphan.length) fail.push(`tab buttons with no section: ${orphan.join(", ")}`);
else console.log(`ok  ${tabs.length} tab(s), each with a section`);

if (fail.length) {
  fail.forEach((f) => console.error(`FAIL  ${f}`));
  process.exit(1);
}
console.log("all checks passed");
