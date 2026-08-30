/*
 * devtools/launcher/server.js — a local control panel for two things a
 * session doing akinator work reaches for constantly: the bookhub-api
 * backend (for a live site) and games/play-bot.js (for a recorded take).
 *
 * NOT SHIPPED. Nothing here is read by production code, deployed, or
 * committed to a path either repo's build touches. It exists only to save
 * typing `python -m uvicorn ...` and a long `node games/play-bot.js ...`
 * command line by hand, with the process output visible in a browser tab
 * instead of scrolled past in a terminal.
 *
 *     node devtools/launcher/server.js
 *     (or double-click start.bat / the desktop shortcut)
 *
 * Talks to two sibling checkouts by relative path (../../.. from here is
 * the GitHub folder both repos share); if bookhub is not a sibling of
 * bookhub-api on this machine, edit BOOKHUB_DIR below.
 */
'use strict';

const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const PORT = 4590;
const HERE = __dirname;
const API_DIR = path.join(HERE, '..', '..');            // bookhub-api
const BOOKHUB_DIR = path.join(API_DIR, '..', 'bookhub'); // sibling checkout
const TAKES_DIR = path.join(API_DIR, 'takes');

// ── .env, the same way play-bot.js already reads it for itself ─────────────
// uvicorn is spawned directly (not through a shell that would `export` it),
// so main.py sees nothing from .env unless this launcher puts it in the
// child's environment itself. Same file, same format, same reasoning
// play-bot.js's own loadEnv() already documents: no python-dotenv anywhere
// in this repo, production reads Render's dashboard, local scripts parse
// the file themselves.
function loadEnvFile() {
  const file = path.join(API_DIR, '.env');
  const out = {};
  if (!fs.existsSync(file)) { return out; }
  for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const m = /^\s*([A-Z0-9_]+)\s*=\s*(.*)$/.exec(line);
    if (m) { out[m[1]] = m[2].trim().replace(/^["']|["']$/g, ''); }
  }
  return out;
}

// ── one tracked child process per job, so a second click reports status
// instead of spawning a duplicate uvicorn on the same port ─────────────────
function makeJob(name) {
  return { name, proc: null, status: 'idle', log: [], startedAt: null, exitCode: null };
}
const jobs = { backend: makeJob('backend'), record: makeJob('record') };

const MAX_LOG_LINES = 2000;
function append(job, text) {
  for (const line of String(text).split(/\r?\n/)) {
    if (line === '') { continue; }
    job.log.push(line);
  }
  if (job.log.length > MAX_LOG_LINES) { job.log.splice(0, job.log.length - MAX_LOG_LINES); }
}

function startJob(job, cmd, args, opts) {
  if (job.proc) { return false; }
  job.log = [];
  job.status = 'running';
  job.exitCode = null;
  job.startedAt = Date.now();
  append(job, '$ ' + cmd + ' ' + args.join(' '));
  const proc = spawn(cmd, args, { cwd: opts.cwd, env: opts.env, windowsHide: true });
  job.proc = proc;
  proc.stdout.on('data', d => append(job, d));
  proc.stderr.on('data', d => append(job, d));
  proc.on('error', e => { append(job, '[launcher] failed to start: ' + e.message); job.status = 'error'; job.proc = null; });
  proc.on('exit', code => {
    job.exitCode = code;
    job.status = code === 0 ? 'done' : (job.status === 'stopping' ? 'idle' : 'error');
    job.proc = null;
  });
  return true;
}

// taskkill /t so a --reload uvicorn's child watcher process dies too --
// proc.kill() alone leaves it running and the port stays bound.
function stopJob(job) {
  if (!job.proc) { return false; }
  job.status = 'stopping';
  if (process.platform === 'win32') {
    spawn('taskkill', ['/pid', String(job.proc.pid), '/t', '/f'], { windowsHide: true });
  } else {
    job.proc.kill('SIGTERM');
  }
  return true;
}

function jobView(job) {
  return {
    name: job.name, status: job.status, exitCode: job.exitCode,
    startedAt: job.startedAt, log: job.log.join('\n'),
  };
}

// ── routes ───────────────────────────────────────────────────────────────
function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', c => { data += c; });
    req.on('end', () => { try { resolve(data ? JSON.parse(data) : {}); } catch (e) { reject(e); } });
    req.on('error', reject);
  });
}

function sendJSON(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': Buffer.byteLength(body) });
  res.end(body);
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  try {
    if (req.method === 'GET' && url.pathname === '/') {
      const html = fs.readFileSync(path.join(HERE, 'index.html'));
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(html);
      return;
    }

    if (req.method === 'GET' && url.pathname === '/api/status') {
      sendJSON(res, 200, { backend: jobView(jobs.backend), record: jobView(jobs.record) });
      return;
    }

    if (req.method === 'POST' && url.pathname === '/api/backend/start') {
      if (!fs.existsSync(path.join(API_DIR, 'main.py'))) {
        sendJSON(res, 400, { ok: false, error: 'main.py not found at ' + API_DIR });
        return;
      }
      const env = Object.assign({}, process.env, loadEnvFile());
      const started = startJob(jobs.backend, 'python',
        ['-m', 'uvicorn', 'main:app', '--reload', '--port', '8000'],
        { cwd: API_DIR, env });
      sendJSON(res, 200, { ok: true, started, url: 'http://localhost:8000/health' });
      return;
    }
    if (req.method === 'POST' && url.pathname === '/api/backend/stop') {
      sendJSON(res, 200, { ok: true, stopped: stopJob(jobs.backend) });
      return;
    }

    if (req.method === 'POST' && url.pathname === '/api/record/start') {
      if (jobs.record.proc) { sendJSON(res, 409, { ok: false, error: 'a recording is already running' }); return; }
      const body = await readBody(req);
      const script = path.join(BOOKHUB_DIR, 'games', 'play-bot.js');
      if (!fs.existsSync(script)) {
        sendJSON(res, 400, { ok: false, error: 'games/play-bot.js not found under ' + BOOKHUB_DIR });
        return;
      }
      const args = [script];
      if (body.title) { args.push('--title', String(body.title)); }
      if (body.author) { args.push('--author', String(body.author)); }
      if (body.pick) { args.push('--pick'); }
      if (body.pickModel) { args.push('--pick-model'); }
      if (body.games && Number(body.games) > 1) { args.push('--games', String(Number(body.games) | 0)); }
      if (body.offline) { args.push('--offline'); }
      if (body.sheet) { args.push('--sheet'); }
      if (body.mode === 'record') { args.push('--record'); }
      else if (body.mode === 'video') { args.push('--video', 'takes'); }
      if (body.headed) { args.push('--headed'); }
      if (!body.title && !body.pick && !body.pickModel) {
        sendJSON(res, 400, { ok: false, error: 'give a title, or choose "pick from catalogue" / "let a model choose"' });
        return;
      }
      const started = startJob(jobs.record, 'node', args, { cwd: BOOKHUB_DIR, env: process.env });
      sendJSON(res, 200, { ok: true, started });
      return;
    }
    if (req.method === 'POST' && url.pathname === '/api/record/stop') {
      sendJSON(res, 200, { ok: true, stopped: stopJob(jobs.record) });
      return;
    }

    if (req.method === 'POST' && url.pathname === '/api/open-takes-folder') {
      fs.mkdirSync(TAKES_DIR, { recursive: true });
      if (process.platform === 'win32') { spawn('explorer.exe', [TAKES_DIR], { windowsHide: false }); }
      sendJSON(res, 200, { ok: true, dir: TAKES_DIR });
      return;
    }

    res.writeHead(404, { 'Content-Type': 'text/plain' });
    res.end('not found');
  } catch (e) {
    sendJSON(res, 500, { ok: false, error: e.message });
  }
});

server.listen(PORT, '127.0.0.1', () => {
  console.log('Litheca launcher -> http://localhost:' + PORT);
  console.log('  backend dir: ' + API_DIR);
  console.log('  bookhub dir: ' + BOOKHUB_DIR);
});

// A dev server left running with an uvicorn or play-bot.js child attached
// must not orphan them when this window is closed.
function shutdown() {
  stopJob(jobs.backend);
  stopJob(jobs.record);
  process.exit(0);
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
