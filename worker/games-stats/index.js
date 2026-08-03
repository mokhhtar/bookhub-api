/**
 * litheca-games-stats — how many people solved today's puzzle.
 *
 * WHY A WORKER. The games are static JSON played entirely in the browser, and
 * that is the whole reason they cost nothing and work while Render sleeps.
 * A counter on Render would undo it: every solve would hit an instance that
 * may be asleep, at the exact moment someone has just won. This is one row
 * written and one aggregate read — the shape Workers exist for, and it runs
 * inside the 10ms CPU the free plan allows because both are I/O.
 *
 * WHAT IT REFUSES TO DO
 *
 * 1. It will not accept a write it cannot verify. If TURNSTILE_SECRET is
 *    missing the endpoint answers 503, rather than quietly recording
 *    unverified solves. A guard that switches itself off when unconfigured is
 *    not a guard — that exact mistake cost this project a green build that
 *    had stored nothing, twice in one day.
 *
 * 2. It will not report a small number. Below MIN_SOLVERS the stats endpoint
 *    returns `{ enough: false }` and no counts at all, so nothing downstream
 *    can render "3 people solved today" — which is honest and reads as
 *    abandonment. The threshold lives here, not in the page, because a number
 *    the client never receives is one it cannot leak.
 *
 * 3. It will not claim the numbers are verified. They are self-reported: we
 *    cannot prove someone solved in two. One row per player per day per game
 *    is structural (the primary key), and Turnstile keeps scripts out, but
 *    the honest label is "as reported by players" and the response says so.
 */

const GAMES = new Set(["guess-the-book", "spot-the-slop"]);
const MAX_GUESSES = 6;
// Below this many solvers the day's numbers are withheld entirely. A new game
// reads as empty either way; showing "4 solved" advertises that nobody is
// here, while showing nothing is simply quiet.
const MIN_SOLVERS = 20;

const CORS = {
  "Access-Control-Allow-Origin": "https://litheca.com",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Access-Control-Max-Age": "86400",
};

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });

/** A YYYY-MM-DD that is today or yesterday in UTC. */
function plausibleDay(day) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return false;
  const asked = Date.parse(day + "T00:00:00Z");
  if (Number.isNaN(asked)) return false;
  // Players' dates are LOCAL, so "today" spans about two UTC dates at once —
  // someone in UTC+13 is a day ahead of someone in UTC-11. A one-day window
  // either side covers every timezone without letting anyone backfill a week.
  const now = Date.now();
  const day_ms = 86400000;
  return asked >= now - 2 * day_ms && asked <= now + day_ms;
}

async function verifyTurnstile(token, secret, ip) {
  const form = new FormData();
  form.append("secret", secret);
  form.append("response", token);
  if (ip) form.append("remoteip", ip);
  const res = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
    method: "POST",
    body: form,
  });
  if (!res.ok) return false;
  const data = await res.json();
  return data.success === true;
}

async function recordSolve(request, env) {
  if (!env.TURNSTILE_SECRET) {
    // Deliberately not a soft pass. See the header.
    return json({ error: "verification unavailable" }, 503);
  }

  let body;
  try {
    body = await request.json();
  } catch (e) {
    return json({ error: "bad json" }, 400);
  }

  const { game, day, player, guesses, token } = body || {};
  if (!GAMES.has(game)) return json({ error: "unknown game" }, 400);
  if (!plausibleDay(day)) return json({ error: "implausible day" }, 400);
  if (typeof player !== "string" || player.length < 8 || player.length > 64) {
    return json({ error: "bad player id" }, 400);
  }
  if (!Number.isInteger(guesses) || guesses < 1 || guesses > MAX_GUESSES) {
    return json({ error: "guesses out of range" }, 400);
  }
  if (typeof token !== "string" || !token) return json({ error: "missing token" }, 400);

  const ip = request.headers.get("CF-Connecting-IP") || "";
  if (!(await verifyTurnstile(token, env.TURNSTILE_SECRET, ip))) {
    return json({ error: "verification failed" }, 403);
  }

  // OR IGNORE, not OR REPLACE: a second submission for the same day is a
  // reload or a retry, and the first answer is the honest one. The primary
  // key is what makes that a property of the table rather than of this code.
  await env.DB.prepare(
    "INSERT OR IGNORE INTO solves (game, day, player, guesses, created_at) VALUES (?, ?, ?, ?, ?)"
  ).bind(game, day, player, guesses, Date.now()).run();

  return json({ ok: true });
}

async function readStats(url, env) {
  const game = url.searchParams.get("game") || "";
  const day = url.searchParams.get("day") || "";
  if (!GAMES.has(game)) return json({ error: "unknown game" }, 400);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return json({ error: "bad day" }, 400);

  // One query gives the total and the distribution together.
  const { results } = await env.DB.prepare(
    "SELECT guesses, COUNT(*) AS n FROM solves WHERE game = ? AND day = ? GROUP BY guesses"
  ).bind(game, day).all();

  const dist = new Array(MAX_GUESSES).fill(0);
  let solvers = 0;
  for (const row of results || []) {
    const g = Number(row.guesses);
    const n = Number(row.n);
    if (g >= 1 && g <= MAX_GUESSES) dist[g - 1] = n;
    solvers += n;
  }

  if (solvers < MIN_SOLVERS) {
    // No counts leave the building. The caller learns only that there is not
    // enough yet, which is all it needs to render nothing.
    return json({ enough: false, min: MIN_SOLVERS, source: "players" });
  }
  return json({ enough: true, solvers, dist, source: "players" });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }
    if (url.pathname === "/solved" && request.method === "POST") {
      return recordSolve(request, env);
    }
    if (url.pathname === "/stats" && request.method === "GET") {
      return readStats(url, env);
    }
    return json({ error: "not found" }, 404);
  },
};
