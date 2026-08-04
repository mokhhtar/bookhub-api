/**
 * litheca-games-stats — how many people played today's puzzle, and how many
 * of them solved it.
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
 * 2. It will not report a small number. Below MIN_PLAYERS the stats endpoint
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

// The valid score range per game, because they do not share one. Guess the
// Book counts guesses used, 1-6. Spot the Slop counts pairs spotted, 0-5 —
// and ZERO is a real result there, not an error. A single 1..6 rule would
// have rejected every player who got none of them right, and rejected it
// server-side where nobody would see the failure.
const GAMES = {
  "guess-the-book": { min: 1, max: 6, buckets: 6 },
  "spot-the-slop": { min: 0, max: 5, buckets: 6 },
};

// A loss. Stored as a score outside every game's own range so it can never
// collide with a real one, and counted separately.
//
// Recording it changes what the counter can honestly say. Wins alone give
// "57 solved" — a number with no denominator, which quietly flatters us:
// the people who tried and failed are simply absent. With losses we can say
// "57 of 80", and the rate is the more interesting fact anyway. It also
// means a player who loses is not told, by silence, that their day did not
// count.
const LOSS = -1;

// Below this many PLAYERS the day's numbers are withheld entirely. A new game
// reads as empty either way; showing "4 played" advertises that nobody is
// here, while showing nothing is simply quiet.
const MIN_PLAYERS = 20;

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
  const rules = GAMES[game];
  if (!rules) return json({ error: "unknown game" }, 400);
  if (!plausibleDay(day)) return json({ error: "implausible day" }, 400);
  if (typeof player !== "string" || player.length < 8 || player.length > 64) {
    return json({ error: "bad player id" }, 400);
  }
  // LOSS is deliberately outside every game's range, so "in range OR the
  // loss sentinel" is one check that cannot be satisfied by accident.
  const inRange = Number.isInteger(guesses) && guesses >= rules.min && guesses <= rules.max;
  if (!inRange && guesses !== LOSS) {
    return json({ error: "score out of range" }, 400);
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
  const rules = GAMES[game];
  if (!rules) return json({ error: "unknown game" }, 400);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return json({ error: "bad day" }, 400);

  // One query gives the total and the distribution together.
  const { results } = await env.DB.prepare(
    "SELECT guesses, COUNT(*) AS n FROM solves WHERE game = ? AND day = ? GROUP BY guesses"
  ).bind(game, day).all();

  // The bucket index is the score minus the game's own minimum, so both
  // games produce a dense array the page can render without knowing which
  // game it is: guesses 1-6 and pairs 0-5 both land in slots 0-5. Losses are
  // NOT a bucket — they have no position on an axis of "how well" — so they
  // are counted apart and reported as their own number.
  const dist = new Array(rules.buckets).fill(0);
  let solvers = 0;
  let players = 0;
  for (const row of results || []) {
    const g = Number(row.guesses);
    const n = Number(row.n);
    players += n;
    if (g >= rules.min && g <= rules.max) {
      dist[g - rules.min] = n;
      solvers += n;
    }
  }

  // The floor counts PLAYERS, not solvers. A day where 30 people played and
  // 3 solved is a hard puzzle worth reporting; a day where 3 people played
  // is an empty room, and the two must not look alike.
  if (players < MIN_PLAYERS) {
    // No counts leave the building. The caller learns only that there is not
    // enough yet, which is all it needs to render nothing.
    return json({ enough: false, min: MIN_PLAYERS, source: "players" });
  }
  return json({ enough: true, players, solvers, dist, source: "players" });
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
