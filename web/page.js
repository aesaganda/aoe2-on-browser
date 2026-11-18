// The page around the WebAssembly build (Phase 2, ADR-0005).
//
// On first visit, the game's files are automatically downloaded from the server
// (/install/...) into the origin private file system (OPFS) under aoe2/, and the
// module (build/web/aoe2.js) mounts OPFS and runs the game from /opfs/aoe2
// (src/web/web_fs.c). On subsequent visits the cached OPFS copy boots directly.
//
// Not copied: the disc tree (the game only checks that a CD with the right label is
// there: an empty cdrom/ and cdrom.label stand for it), the AVIs and .cab archives.
// The MPEG-1 copies beside the AVIs are what plays (ADR-0004), but the game plays a
// video only if its .avi exists, so each copy gets an empty .avi beside it
// (videoStandIns). An install without the copies plays no videos.
'use strict';
const $ = (id) => document.getElementById(id);
const log = (s) => { const el = $('log'); el.textContent += s + '\n'; el.scrollTop = 1e9; };
const DISC_LABEL = 'AGE2_X1';
const MARK = '.complete';

window.onerror = (msg) => { log('Error: ' + msg); };
window.onunhandledrejection = (e) => { log('Error: ' + (e.reason?.message || e.reason || 'unknown')); };

// --- Compatibility checks ---
function checkCompat() {
  const issues = [];
  if (!window.crossOriginIsolated) issues.push('Cross-origin isolation is not active (SharedArrayBuffer unavailable).');
  if (!('storage' in navigator && 'getDirectory' in navigator.storage)) issues.push('Origin Private File System (OPFS) is not supported.');
  if (typeof WebAssembly === 'undefined') issues.push('WebAssembly is not supported.');
  if (!window.SharedArrayBuffer) issues.push('SharedArrayBuffer is not available.');
  if (/iPhone|iPad|iPod|Android/i.test(navigator.userAgent)) issues.push('This game needs a desktop browser with keyboard and mouse.');
  if (issues.length) {
    const el = $('compat-warn');
    el.textContent = issues.join(' ');
    el.hidden = false;
  }
  return issues.length === 0;
}

function hasRuntimeAPIs() {
  return Boolean(
    window.crossOriginIsolated &&
    'storage' in navigator &&
    'getDirectory' in navigator.storage &&
    typeof WebAssembly !== 'undefined' &&
    window.SharedArrayBuffer
  );
}

function skip(path) {
  const p = path.toLowerCase();
  return p.startsWith('cdrom/') || p.endsWith('.avi') || p.endsWith('.cab') || p === 'cdrom.label' || p.split('/').pop() === MARK;
}

async function root() { return (await navigator.storage.getDirectory()).getDirectoryHandle('aoe2', { create: true }); }

async function dirOf(base, path, create) {
  let d = base;
  const parts = path.split('/');
  for (const part of parts.slice(0, -1)) d = await d.getDirectoryHandle(part, { create });
  return [d, parts[parts.length - 1]];
}

async function writeFileToDir(dir, name, data) {
  const f = await dir.getFileHandle(name, { create: true });
  const w = await f.createWritable();
  if (data instanceof ReadableStream) await data.pipeTo(w); else { await w.write(data); await w.close(); }
}

async function writeFile(base, path, data) {
  const [d, name] = await dirOf(base, path, true);
  await writeFileToDir(d, name, data);
}

async function videoStandIns(base) {
  for await (const [, d] of base.entries()) {
    if (d.kind !== 'directory') continue;
    const names = [];
    for await (const [n] of d.entries()) names.push(n);
    const lower = new Set(names.map((n) => n.toLowerCase()));
    for (const n of names)
      if (n.toLowerCase().endsWith('.mpg') && !lower.has(n.slice(0, -4).toLowerCase() + '.avi'))
        await (await (await d.getFileHandle(n.slice(0, -4) + '.avi', { create: true })).createWritable()).close();
  }
}

async function finish(base) {
  await videoStandIns(base);
  await base.getDirectoryHandle('cdrom', { create: true });
  await writeFile(base, 'cdrom.label', new Blob([DISC_LABEL + '\n']));
  await writeFile(base, MARK, new Blob([new Date().toISOString()]));
}

async function isComplete() {
  try { await (await root()).getFileHandle(MARK); return true; } catch { return false; }
}

const EXE_PATH = 'age2_x1/age2_x1.exe';
async function exeProblem(file) {
  let want;
  try { want = (await (await fetch('aoe2.json', { cache: 'no-store' })).json()).exe; } catch { return null; }
  if (!want) return null;
  if (file.size === want.size) {
    const got = [...new Uint8Array(await crypto.subtle.digest('SHA-256', await file.arrayBuffer()))]
      .map((b) => b.toString(16).padStart(2, '0')).join('');
    if (got === want.sha256) return null;
  }
  return `Your age2_x1.exe (${file.size} bytes) is not the one this build runs: it needs The Conquerors 1.0 as ` +
         `installed from the disc, without patches (${want.size} bytes, SHA-256 ${want.sha256.slice(0, 12)}…).`;
}

async function copiedExe() {
  for await (const [n, d] of (await root()).entries())
    if (d.kind === 'directory' && n.toLowerCase() === 'age2_x1')
      for await (const [m, f] of d.entries()) if (f.kind === 'file' && m.toLowerCase() === 'age2_x1.exe') return f.getFile();
  return null;
}

async function checkStorage(needed) {
  if (!navigator.storage.estimate) return;
  const { quota, usage } = await navigator.storage.estimate();
  const free = quota - usage;
  if (free < needed * 1.1) {
    const freeMB = (free / 1e6).toFixed(0);
    const needMB = (needed / 1e6).toFixed(0);
    log(`Not enough storage: ${needMB} MB needed, only ${freeMB} MB available. Free up space or clear site data.`);
    throw new Error(`Insufficient browser storage (${needMB} MB needed, ${freeMB} MB available).`);
  }
}

async function fetchRetry(url, opts, retries = 3) {
  for (let i = 0; i <= retries; i++) {
    try {
      const r = await fetch(url, opts);
      if (r.ok) return r;
      if (r.status >= 400 && r.status < 500) throw new Error(`${url}: ${r.status}`);
      if (i === retries) throw new Error(`${url}: ${r.status} after ${retries + 1} attempts`);
    } catch (e) {
      if (e.name === 'AbortError') throw e;
      if (i === retries) throw e;
    }
    await new Promise((r) => setTimeout(r, 1000 * (i + 1)));
  }
}

async function importServer() {
  const loadText = $('load-text');
  const loadBar = $('load-bar');
  const loadSub = $('load-sub');
  loadText.textContent = 'Checking game files…';
  loadBar.hidden = false;
  loadBar.removeAttribute('value');
  loadSub.textContent = '';

  const manifestResp = await fetchRetry('install/manifest.json', { cache: 'no-store' });
  const list = await manifestResp.json();
  const base = await root();
  const files = list.files.filter(([p]) => !skip(p));
  const total = files.reduce((s, [, n]) => s + n, 0);
  await checkStorage(total);

  // Pre-create all directories sequentially so parallel workers never race on getDirectoryHandle(..., { create: true }).
  const dirs = new Set();
  for (const [p] of files) {
    const parts = p.split('/');
    for (let i = 1; i < parts.length; i++) dirs.add(parts.slice(0, i).join('/'));
  }
  const dirHandles = new Map([['', base]]);
  for (const d of [...dirs].sort((a, b) => a.length - b.length)) {
    const slash = d.lastIndexOf('/');
    const parent = dirHandles.get(slash === -1 ? '' : d.slice(0, slash));
    dirHandles.set(d, await parent.getDirectoryHandle(slash === -1 ? d : d.slice(slash + 1), { create: true }));
  }

  loadText.textContent = 'Loading game files…';
  loadBar.value = 0;
  const start = performance.now();
  let done = 0;

  const updateProgress = (bytes) => {
    done += bytes;
    loadBar.value = total ? done / total : 1;
    const elapsed = (performance.now() - start) / 1000;
    if (elapsed > 0.5 && done > 0) {
      const speed = done / elapsed;
      const left = Math.max(0, (total - done) / speed);
      const mb = (speed / 1e6).toFixed(1);
      const pct = total ? Math.floor((done / total) * 100) : 100;
      const eta = left < 60 ? `${Math.ceil(left)}s` : `${Math.floor(left / 60)}m ${Math.ceil(left % 60)}s`;
      loadSub.textContent = `${pct}% · ${mb} MB/s — ${eta} left`;
    }
  };

  let idx = 0;
  const concurrency = Math.min(6, files.length);
  const workers = Array.from({ length: concurrency }, async () => {
    while (idx < files.length) {
      const [p, n] = files[idx++];
      const r = await fetchRetry('install/' + p.split('/').map(encodeURIComponent).join('/'));
      const slash = p.lastIndexOf('/');
      const dir = dirHandles.get(slash === -1 ? '' : p.slice(0, slash));
      const name = slash === -1 ? p : p.slice(slash + 1);
      await writeFileToDir(dir, name, r.body);
      updateProgress(n);
    }
  });
  await Promise.all(workers);

  loadText.textContent = 'Finalizing game files…';
  loadSub.textContent = '';
  await finish(base);
  log(`Loaded ${files.length} files (${(total / 1e6).toFixed(0)} MB) from the server.`);
}

async function refresh() {
  const ok = await isComplete();
  $('play').hidden = !ok;
  $('play').textContent = gameRunning ? 'Resume game' : 'Play';
  $('online').hidden = false;
  $('host').disabled = !ok;
  if (ok) {
    try {
      let saves = 0;
      const r = await root();
      for await (const [n, h] of r.entries()) {
        if (h.kind !== 'directory' || n.toLowerCase() !== 'savegame') continue;
        for await (const [sn] of h.entries()) if (sn.toLowerCase().endsWith('.mgl') || sn.toLowerCase().endsWith('.mgx')) saves++;
      }
      $('save-count').textContent = saves ? `${saves} saved game${saves > 1 ? 's' : ''}` : '';
    } catch { /* no SaveGame dir yet */ }
    try {
      const { usage } = await navigator.storage.estimate();
      const usedMB = (usage / 1e6).toFixed(0);
      $('storage-info').textContent = `${usedMB} MB used`;
    } catch {}
  }
}

function presenter(module) {
  const sdl = $('canvas'), scr = $('screen'), ctx = scr.getContext('2d');
  let img = null, firstFrame = true;
  return {
    ra2Present(fb, w, h, dx, dy, dw, dh, mode, cw, ch) {
      if (!img || img.width !== w || img.height !== h) { scr.width = w; scr.height = h; img = ctx.createImageData(w, h); }
      const src = module.HEAPU16.subarray(fb >> 1, (fb >> 1) + w * h), out = new Uint32Array(img.data.buffer);
      for (let i = 0; i < src.length; i++) {
        const v = src[i], r = (v >> 11) & 31, g = (v >> 5) & 63, b = v & 31;
        out[i] = 0xFF000000 | (((b << 3) | (b >> 2)) << 16) | (((g << 2) | (g >> 4)) << 8) | ((r << 3) | (r >> 2));
      }
      ctx.putImageData(img, 0, 0);
      const r = sdl.getBoundingClientRect(), sx = r.width / cw, sy = r.height / ch;
      Object.assign(scr.style, { left: `${r.left + dx * sx}px`, top: `${r.top + dy * sy}px`, width: `${dw * sx}px`, height: `${dh * sy}px` });
      scr.hidden = false;
      if (firstFrame) { $('loading').hidden = true; $('game-ui').hidden = false; firstFrame = false; }
    },
    ra2Menu(open) { scr.hidden = !!open; },
  };
}

// --- Fullscreen ---
function toggleFullscreen() {
  if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
  } else {
    document.documentElement.requestFullscreen().catch(() => {});
  }
}

const RELAY = location.port === '8642' ? `//${location.hostname}:8643/relay` : `//${location.host}/relay`;
const relayUrl = (location.protocol === 'https:' ? 'wss:' : 'ws:') + RELAY;
const roomsUrl = location.protocol + RELAY + '/rooms';

// --- Connection status ---
let relayOk = false;
let consecutiveFails = 0;

function setStatus(ok, text) {
  const el = $('status');
  el.textContent = text;
  el.className = ok ? 'ok' : 'err';
  relayOk = ok;
  $('rooms').classList.toggle('dimmed', !ok);
}

const playerName = () => ($('player').value.trim() || 'Player').slice(0, 15);

async function listRooms() {
  if ($('setup').hidden) return;
  let rooms = [];
  try {
    const t0 = performance.now();
    const r = await fetch(roomsUrl, { cache: 'no-store', signal: AbortSignal.timeout(5000) });
    if (!r.ok) throw new Error(r.status);
    rooms = (await r.json()).rooms;
    const ping = Math.round(performance.now() - t0);
    const wasOffline = consecutiveFails >= 2;
    consecutiveFails = 0;
    pollBackoff = POLL_MIN;
    const total = rooms.reduce((s, r) => s + r.players.length, 0);
    setStatus(true, total ? `${total} online · ${ping}ms` : `connected · ${ping}ms`);
    if (wasOffline) log('Reconnected to the game server.');
  } catch {
    consecutiveFails++;
    pollBackoff = Math.min(pollBackoff * 1.5, POLL_MAX);
    if (consecutiveFails >= 2) setStatus(false, 'offline');
    $('rooms').textContent = 'Cannot reach the game server.';
    return;
  }
  const box = $('rooms'), key = JSON.stringify(rooms) + $('play').hidden;
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.replaceChildren();
  if (!rooms.length) { box.textContent = 'No games yet — host one.'; return; }
  for (const r of rooms) {
    const row = document.createElement('div');
    row.className = 'room';
    row.setAttribute('role', 'listitem');
    const info = document.createElement('span');
    info.textContent = `${r.name} — ${r.players.length}/${r.max} (${r.players.join(', ')})${r.open ? '' : ' · playing'}`;
    const actions = document.createElement('span');
    actions.style.display = 'flex';
    actions.style.gap = '4px';
    const b = document.createElement('button');
    b.textContent = 'Join';
    b.disabled = !r.open || r.players.length >= r.max || $('play').hidden;
    b.onclick = () => play({ lobby: 'join:' + r.id, room: r.name });
    const link = document.createElement('button');
    link.textContent = 'Link';
    link.title = 'Copy invite link';
    link.onclick = () => {
      const u = new URL(location.href);
      u.search = `?join=${r.id}&room=${encodeURIComponent(r.name)}`;
      navigator.clipboard.writeText(u.toString()).then(() => { link.textContent = 'Copied!'; setTimeout(() => { link.textContent = 'Link'; }, 1500); });
    };
    actions.append(b, link);
    row.append(info, actions);
    box.append(row);
  }
}

let roomInterval = null;
let pollBackoff = 3000;
const POLL_MIN = 3000, POLL_MAX = 30000;

function startRoomPolling() {
  if (roomInterval) return;
  pollBackoff = POLL_MIN;
  listRooms();
  scheduleNextPoll();
}

function scheduleNextPoll() {
  if (roomInterval) clearTimeout(roomInterval);
  roomInterval = setTimeout(() => {
    listRooms().then(() => {
      if (roomInterval) scheduleNextPoll();
    });
  }, pollBackoff);
}

function stopRoomPolling() {
  if (roomInterval) { clearTimeout(roomInterval); roomInterval = null; }
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) { stopRoomPolling(); }
  else if (!$('setup').hidden) { startRoomPolling(); }
});

window.addEventListener('online', () => {
  if (!$('setup').hidden) { pollBackoff = POLL_MIN; startRoomPolling(); }
});
window.addEventListener('offline', () => {
  setStatus(false, 'no network');
});

let gameRunning = false;

function openOnline() {
  if (!$('setup').hidden) return;
  $('play').textContent = gameRunning ? 'Resume game' : 'Play';
  $('setup').hidden = false;
  refresh();
  startRoomPolling();
}

function closeOnline() {
  if ($('setup').hidden) return;
  $('setup').hidden = true;
  stopRoomPolling();
  if (gameRunning) $('canvas').focus();
}

function returnToLobby(msg) {
  gameRunning = false;
  $('loading').hidden = true;
  $('game-ui').hidden = true;
  $('canvas').hidden = true;
  $('screen').hidden = true;
  $('play').textContent = 'Play';
  $('setup').hidden = false;
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  refresh();
  startRoomPolling();
  if (msg) log(msg);
}

async function play(launch) {
  if (gameRunning) {
    const u = new URL(location.href);
    if (launch && launch.lobby === 'host') {
      u.search = `?host=1&room=${encodeURIComponent(launch.room || '')}`;
    } else if (launch && launch.lobby && launch.lobby.startsWith('join:')) {
      u.search = `?join=${encodeURIComponent(launch.lobby.slice(5))}&room=${encodeURIComponent(launch.room || '')}`;
    } else {
      u.search = '';
    }
    location.href = u.toString();
    return;
  }
  if (!crossOriginIsolated) { returnToLobby('This page must be served cross-origin isolated (tools/web/serve.py does).'); return; }
  const exe = await copiedExe().catch(() => null);
  const problem = exe ? await exeProblem(exe) : 'The browser cache has no age2_x1\\age2_x1.exe.';
  if (problem) { returnToLobby(problem); return; }
  await videoStandIns(await root()).catch(() => {});
  gameRunning = true;
  stopRoomPolling();
  $('setup').hidden = true;
  $('loading').hidden = false;
  const loadBar = $('load-bar');
  const loadText = $('load-text');
  const loadSub = $('load-sub');
  loadBar.hidden = false;
  loadBar.value = 0;
  loadText.textContent = 'Starting the game…';
  loadSub.textContent = '';
  $('retry-btn').hidden = true;
  const canvas = $('canvas');
  canvas.hidden = false; canvas.focus();
  const config = {
    canvas,
    arguments: [],
    preRun: [(m) => {
      m.ENV.AOE2_ACCEPT_EULA = '1';
      m.ENV.AOE2_TRACE_VIDEO = '1';
      if (launch && launch.lobby) {
        m.ENV.AOE2_ARGS = 'LOBBY';
        m.ENV.AOE2_LOBBY = launch.lobby;
        m.ENV.AOE2_ROOM = launch.room || '';
        m.ENV.AOE2_PLAYER = playerName();
      }
    }],
    setStatus(text) {
      if (!text) return;
      const m = text.match(/([^(]+)\((\d+(\.\d+)?)\/(\d+)\)/);
      if (m) {
        loadBar.value = 1 - parseInt(m[2]) / parseInt(m[4]);
        loadText.textContent = m[1].trim() || 'Loading…';
      } else {
        loadBar.removeAttribute('value');
        loadText.textContent = text;
      }
    },
    monitorRunDependencies(left) {
      if (left === 0) { loadBar.value = 1; loadText.textContent = 'Starting…'; }
    },
    relayUrl,
    print: (t) => console.log(t),
    printErr: (t) => console.warn(t),
  };
  Object.assign(config, presenter(config));
  const startWasm = () => AoE2(config).then(
    () => returnToLobby('The game exited.'),
    (e) => returnToLobby('The game stopped: ' + e)
  );
  if (typeof AoE2 === 'function') {
    startWasm();
  } else {
    const s = document.createElement('script');
    s.src = 'aoe2.js';
    s.onload = startWasm;
    s.onerror = () => returnToLobby('Failed to load the game engine. Try refreshing the page.');
    document.body.appendChild(s);
  }
}

async function init() {
  checkCompat();
  if (!hasRuntimeAPIs()) {
    $('load-text').textContent = 'Browser not supported';
    $('load-bar').hidden = true;
    return;
  }

  const params = new URLSearchParams(location.search);
  const joinId = params.get('join');
  const hostParam = params.get('host');
  const roomParam = params.get('room') || '';
  let launch;
  if (joinId) {
    history.replaceState(null, '', location.pathname);
    launch = { lobby: 'join:' + joinId, room: roomParam };
    log('Joining game from invite link…');
  } else if (hostParam) {
    history.replaceState(null, '', location.pathname);
    launch = { lobby: 'host', room: (roomParam || `${playerName()}'s game`).slice(0, 30) };
  }

  const exe = await copiedExe().catch(() => null);
  const complete = (await isComplete()) && Boolean(exe) && !(await exeProblem(exe));

  if (!complete) {
    try {
      await importServer();
    } catch (e) {
      $('load-text').textContent = 'Failed to load game files';
      $('load-sub').textContent = e.message || String(e);
      $('load-bar').hidden = true;
      $('retry-btn').hidden = false;
      log('Could not load automatically: ' + e.message);
      return;
    }
  }

  const link = document.createElement('link');
  link.rel = 'preload'; link.href = 'aoe2.wasm'; link.as = 'fetch'; link.crossOrigin = '';
  document.head.appendChild(link);
  await refresh();
  await play(launch);
}

$('online-btn').onclick = () => { if ($('setup').hidden) openOnline(); else closeOnline(); };
$('play').onclick = () => { if (gameRunning) closeOnline(); else play(); };
$('host').onclick = () => play({ lobby: 'host', room: ($('roomname').value.trim() || `${playerName()}'s game`).slice(0, 30) });
$('retry-btn').onclick = () => location.reload();
$('fullscreen-btn').onclick = toggleFullscreen;
$('canvas').addEventListener('contextmenu', (e) => e.preventDefault());
$('setup').onclick = (e) => { if (gameRunning && e.target === $('setup')) closeOnline(); };
for (const evt of ['keydown', 'keyup', 'keypress']) {
  $('setup').addEventListener(evt, (e) => {
    if (!gameRunning) return;
    if (evt === 'keydown' && e.key === 'Escape' && $('help-overlay').hidden) {
      e.preventDefault();
      closeOnline();
    }
    e.stopPropagation();
  });
}
$('help-link').onclick = (e) => { e.preventDefault(); $('help-overlay').hidden = false; $('help-close').focus(); };
$('help-close').onclick = () => { $('help-overlay').hidden = true; $('help-link').focus(); };
$('help-overlay').onclick = (e) => { if (e.target === $('help-overlay')) { $('help-overlay').hidden = true; $('help-link').focus(); } };
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('help-overlay').hidden) { $('help-overlay').hidden = true; return; }
  if (e.key === 'Escape' && gameRunning && !$('setup').hidden) { closeOnline(); return; }
  if (e.key === 'F11' && $('setup').hidden) { e.preventDefault(); toggleFullscreen(); }
});
try { $('player').value = localStorage.getItem('aoe2.player') || ''; } catch {}
$('player').oninput = () => { try { localStorage.setItem('aoe2.player', $('player').value); } catch {} };
try { $('roomname').value = localStorage.getItem('aoe2.room') || ''; } catch {}
$('roomname').oninput = () => { try { localStorage.setItem('aoe2.room', $('roomname').value); } catch {} };
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('sw.js').then((reg) => {
    reg.addEventListener('updatefound', () => {
      const nw = reg.installing;
      if (!nw) return;
      nw.addEventListener('statechange', () => {
        if (nw.state === 'activated' && navigator.serviceWorker.controller) log('Update available — refresh the page for the latest version.');
      });
    });
  }).catch(() => {});
}
navigator.storage.persist?.();
fetch('api/version', { cache: 'no-store' }).then((r) => r.json()).then((j) => {
  if (j.version) $('version').textContent = j.version;
}).catch(() => {});
init();
