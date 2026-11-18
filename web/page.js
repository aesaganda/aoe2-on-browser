// The page around the WebAssembly build (Phase 2, ADR-0005).
//
// The game's files are the owner's: they are copied once into the origin private
// file system (OPFS) under aoe2/, from a folder the owner picks (or, on a dev
// server, from the install tree it serves), and the module (build/web/aoe2.js)
// mounts OPFS and runs the game from /opfs/aoe2 (src/web/web_fs.c).
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

async function writeFile(base, path, data) {
  const [d, name] = await dirOf(base, path, true);
  const f = await d.getFileHandle(name, { create: true });
  const w = await f.createWritable();
  if (data instanceof ReadableStream) await data.pipeTo(w); else { await w.write(data); await w.close(); }
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

async function* walk(dir, prefix = '') {
  for await (const [name, h] of dir.entries()) {
    const path = prefix + name;
    if (h.kind === 'directory') yield* walk(h, path + '/');
    else yield [path, h];
  }
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

function progressTracker(total) {
  const start = performance.now();
  let done = 0;
  return {
    add(bytes) {
      done += bytes;
      $('bar').value = done / total;
      const elapsed = (performance.now() - start) / 1000;
      if (elapsed > 1) {
        const speed = done / elapsed;
        const left = (total - done) / speed;
        const mb = (speed / 1e6).toFixed(1);
        const eta = left < 60 ? `${Math.ceil(left)}s` : `${Math.floor(left / 60)}m ${Math.ceil(left % 60)}s`;
        $('bar-text').textContent = `${mb} MB/s — ${eta} left`;
      }
    },
    clear() { $('bar-text').textContent = ''; },
  };
}

async function importFolder() {
  if (!window.showDirectoryPicker) { log('This browser cannot open a folder here; use Chrome or Edge.'); return; }
  const src = await window.showDirectoryPicker({ mode: 'read' });
  const files = [];
  for await (const [path, h] of walk(src)) if (!skip(path)) files.push([path, h]);
  const exe = files.find(([p]) => p.toLowerCase() === EXE_PATH);
  if (!exe) {
    log('That folder has no age2_x1\\age2_x1.exe: pick the folder the game is installed in.');
    return;
  }
  const problem = await exeProblem(await exe[1].getFile());
  if (problem) { log(problem + ' Nothing was copied.'); return; }
  const base = await root();
  let total = 0;
  const sized = [];
  for (const [p, h] of files) { const f = await h.getFile(); sized.push([p, f]); total += f.size; }
  $('bar').hidden = false;
  const prog = progressTracker(total);
  for (const [p, f] of sized) {
    await writeFile(base, p, f);
    prog.add(f.size);
  }
  prog.clear();
  $('bar').hidden = true;
  await finish(base);
  log(`Copied ${sized.length} files (${(total / 1e6).toFixed(0)} MB). Ready to play.`);
}

async function importServer() {
  const list = await (await fetch('install/manifest.json')).json();
  const base = await root();
  const files = list.files.filter(([p]) => !skip(p));
  const total = files.reduce((s, [, n]) => s + n, 0);
  $('bar').hidden = false;
  const prog = progressTracker(total);
  for (const [p, n] of files) {
    const r = await fetch('install/' + p.split('/').map(encodeURIComponent).join('/'));
    if (!r.ok) throw new Error(`${p}: ${r.status}`);
    await writeFile(base, p, r.body);
    prog.add(n);
  }
  prog.clear();
  $('bar').hidden = true;
  await finish(base);
  log(`Copied ${files.length} files (${(total / 1e6).toFixed(0)} MB) from the server. Ready to play.`);
}

async function forget() {
  await (await navigator.storage.getDirectory()).removeEntry('aoe2', { recursive: true });
  log('Game files removed.'); await refresh();
}

async function refresh() {
  const ok = await isComplete();
  $('play').hidden = !ok;
  $('forget').hidden = !ok;
  $('pick').hidden = ok;
  $('online').hidden = false;
  $('host').disabled = !ok;
  $('intro').hidden = ok;
  $('eula-row').hidden = ok;
  if (ok) {
    $('eula').checked = true;
    $('server').hidden = true;
  } else {
    try { $('server').hidden = !(await fetch('install/manifest.json')).ok; } catch { $('server').hidden = true; }
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
    consecutiveFails = 0;
    const total = rooms.reduce((s, r) => s + r.players.length, 0);
    setStatus(true, total ? `${total} online · ${ping}ms` : `connected · ${ping}ms`);
  } catch {
    consecutiveFails++;
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
function startRoomPolling() {
  if (roomInterval) return;
  listRooms();
  roomInterval = setInterval(listRooms, 3000);
}

function stopRoomPolling() {
  if (roomInterval) { clearInterval(roomInterval); roomInterval = null; }
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) { stopRoomPolling(); }
  else if (!$('setup').hidden) { startRoomPolling(); }
});

function returnToLobby(msg) {
  $('loading').hidden = true;
  $('game-ui').hidden = true;
  $('canvas').hidden = true;
  $('screen').hidden = true;
  $('setup').hidden = false;
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  startRoomPolling();
  if (msg) log(msg);
}

async function play(launch) {
  if (!crossOriginIsolated) { log('This page must be served cross-origin isolated (tools/web/serve.py does).'); return; }
  if (!$('eula').checked) { log('Accept the license agreement first (the checkbox above).'); return; }
  const exe = await copiedExe().catch(() => null);
  const problem = exe ? await exeProblem(exe) : 'The copy in this browser has no age2_x1\\age2_x1.exe.';
  if (problem) { log(problem + ' Remove the copy and copy a matching install.'); return; }
  await videoStandIns(await root()).catch(() => {});
  stopRoomPolling();
  $('setup').hidden = true;
  $('loading').hidden = false;
  const loadBar = $('load-bar');
  const loadText = $('load-text');
  loadBar.value = 0;
  const canvas = $('canvas');
  canvas.hidden = false; canvas.focus();
  const s = document.createElement('script');
  s.src = 'aoe2.js';
  const config = {
    canvas,
    arguments: [],
    preRun: [(m) => {
      m.ENV.AOE2_ACCEPT_EULA = '1';
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
  s.onload = () => AoE2(config).then(
    () => returnToLobby('The game exited.'),
    (e) => returnToLobby('The game stopped: ' + e)
  );
  s.onerror = () => returnToLobby('Failed to load the game engine. Try refreshing the page.');
  document.body.appendChild(s);
}

const guard = (f) => async () => {
  for (const b of document.querySelectorAll('button')) b.disabled = true;
  try { await f(); } catch (e) { log('Error: ' + e.message); console.error(e); }
  for (const b of document.querySelectorAll('button')) b.disabled = false;
  await refresh();
};

async function init() {
  checkCompat();

  const complete = await isComplete();
  if (complete) {
    await refresh();
    const params = new URLSearchParams(location.search);
    const joinId = params.get('join');
    if (joinId) {
      history.replaceState(null, '', location.pathname);
      log('Joining game from invite link…');
      play({ lobby: 'join:' + joinId, room: params.get('room') || '' });
      return;
    }
    startRoomPolling();
    return;
  }

  let me = { owner: false, install: false };
  try { me = await (await fetch('api/me', { cache: 'no-store' })).json(); } catch {}

  if (me.owner && me.install) {
    $('eula').checked = true;
    $('eula-row').hidden = true;
    $('intro').textContent = 'Loading your game…';
    $('pick').hidden = true;
    try {
      await importServer();
      await refresh();
    } catch (e) {
      log('Could not load automatically: ' + e.message);
      $('intro').textContent = 'Pick your install folder to get started.';
      $('eula-row').hidden = false;
      await refresh();
    }
    startRoomPolling();
    return;
  }

  await refresh();
  startRoomPolling();
}

$('pick').onclick = guard(importFolder);
$('server').onclick = guard(importServer);
$('forget').onclick = guard(forget);
$('play').onclick = () => play();
$('host').onclick = () => play({ lobby: 'host', room: ($('roomname').value.trim() || `${playerName()}'s game`).slice(0, 30) });
$('fullscreen-btn').onclick = toggleFullscreen;
$('canvas').addEventListener('contextmenu', (e) => e.preventDefault());
$('help-link').onclick = (e) => { e.preventDefault(); $('help-overlay').hidden = false; };
$('help-close').onclick = () => { $('help-overlay').hidden = true; };
$('help-overlay').onclick = (e) => { if (e.target === $('help-overlay')) $('help-overlay').hidden = true; };
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('help-overlay').hidden) { $('help-overlay').hidden = true; return; }
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
