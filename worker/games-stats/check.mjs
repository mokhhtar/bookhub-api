import fs from "node:fs";

const here = new URL(".", import.meta.url);
const src = fs.readFileSync(new URL("index.js", here), "utf8");
const tmp = new URL("_check_tmp.mjs", here);
fs.writeFileSync(tmp, src);
let worker;
try {
  ({ default: worker } = await import(tmp.href + "?t=" + Date.now()));
} finally {
  fs.rmSync(tmp, { force: true });
}

globalThis.fetch = async () => Response.json({ success: true });

let writes = 0;
let reads = 0;
const env = {
  TURNSTILE_SECRET: "test-secret",
  DB: {
    prepare(sql) {
      return {
        bind() {
          if (sql.startsWith("INSERT")) {
            return { async run() { writes += 1; return { success: true }; } };
          }
          return {
            async all() {
              reads += 1;
              return { results: [{ guesses: 3, n: 20 }] };
            },
          };
        },
      };
    },
  },
};

const day = new Date().toISOString().slice(0, 10);
const request = new Request("https://games-stats.example/solved", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    game: "guess-the-book", day, player: "contract-player-0001",
    guesses: 3, token: "valid-test-token",
  }),
});
const response = await worker.fetch(request, env);
const body = await response.json();
if (response.status !== 200 || body.ok !== true || body.stats?.enough !== true) {
  throw new Error(`combined response contract failed: ${response.status} ${JSON.stringify(body)}`);
}
if (body.stats.players !== 20 || body.stats.dist?.[2] !== 20) {
  throw new Error(`unexpected aggregate: ${JSON.stringify(body.stats)}`);
}
if (writes !== 1 || reads !== 1) {
  throw new Error(`expected one D1 write and one D1 read, got ${writes}/${reads}`);
}

console.log("ok  /solved returns the current aggregate in one Worker request");
