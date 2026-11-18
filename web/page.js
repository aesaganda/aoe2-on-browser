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
const log = (s) => { $('log').textContent += s + '\n'; $('log').scrollTop = 1e9; };
const DISC_LABEL = 'AGE2_X1';
const MARK = '.complete';

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

async function writeFile(base, path, data) {           // data: a Blob, a File or a ReadableStream
  const [d, name] = await dirOf(base, path, true);
  const f = await d.getFileHandle(name, { create: true });
  const w = await f.createWritable();
  if (data instanceof ReadableStream) await data.pipeTo(w); else { await w.write(data); await w.close(); }
}

async function videoStandIns(base) {                   // an empty x.avi for each x.mpg, in any folder
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

// From a folder the owner picks: the File System Access API where there is one
async function* walk(dir, prefix = '') {
  for await (const [name, h] of dir.entries()) {
    const path = prefix + name;
    if (h.kind === 'directory') yield* walk(h, path + '/');
    else yield [path, h];
  }
}

// The code on this page was lifted from one exact age2_x1.exe (build/web/aoe2.json,
// written by make web): the same game, version 1.0. Another version's executable (a
// patch such as 1.0c) does not match it and would crash, so it is refused up front.
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
async function copiedExe() {                            // the copy's age2_x1.exe, whatever its case
  for await (const [n, d] of (await root()).entries())
    if (d.kind === 'directory' && n.toLowerCase() === 'age2_x1')
      for await (const [m, f] of d.entries()) if (f.kind === 'file' && m.toLowerCase() === 'age2_x1.exe') return f.getFile();
  return null;
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
  let total = 0, done = 0;
  const sized = [];
  for (const [p, h] of files) { const f = await h.getFile(); sized.push([p, f]); total += f.size; }
  $('bar').hidden = false;
  for (const [p, f] of sized) {
    await writeFile(base, p, f);
    done += f.size; $('bar').value = done / total;
  }
  await finish(base);
  log(`copied ${sized.length} files (${(total / 1e6).toFixed(0)} MB)`);
}

// From the dev server (tools/web/serve.py --install): its manifest, then each file
async function importServer() {
  const list = await (await fetch('install/manifest.json')).json();
  const base = await root();
  const files = list.files.filter(([p]) => !skip(p));
  const total = files.reduce((s, [, n]) => s + n, 0);
  let done = 0;
  $('bar').hidden = false;
  for (const [p, n] of files) {
    const r = await fetch('install/' + p.split('/').map(encodeURIComponent).join('/'));
    if (!r.ok) throw new Error(`${p}: ${r.status}`);
    await writeFile(base, p, r.body);
    done += n; $('bar').value = done / total;
  }
  await finish(base);
  log(`copied ${files.length} files (${(total / 1e6).toFixed(0)} MB) from the server`);
}

async function forget() {
  await (await navigator.storage.getDirectory()).removeEntry('aoe2', { recursive: true });
  log('removed'); await refresh();
}

async function refresh() {
  const ok = await isComplete();
  $('play').hidden = !ok;
  $('forget').hidden = !ok;
  $('pick').hidden = ok;
  $('online').hidden = !ok;
  if (ok) {
    $('eula').checked = true;
    $('eula-row').hidden = true;
    $('intro').hidden = true;
    $('server').hidden = true;
  } else {
    try { $('server').hidden = !(await fetch('install/manifest.json')).ok; } catch { $('server').hidden = true; }
  }
}

// win32hle's screen.c hands each frame to the page (its __EMSCRIPTEN__ path): an
// RGB565 picture of w x h in wasm memory, to be shown at (dx, dy, dw, dh) of SDL's
// cw x ch window. It is drawn on #screen, laid over that rectangle of SDL's canvas.
function presenter(module) {
  const sdl = $('canvas'), scr = $('screen'), ctx = scr.getContext('2d');
  let img = null;
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
    },
    ra2Menu(open) { scr.hidden = !!open; },          // SDL draws the host's own menu (F10) itself
  };
}

// The multiplayer relay (tools/net/relay.py): beside the page behind a proxy (/relay),
// or its own port when the page comes straight from tools/web/serve.py
const RELAY = location.port === '8642' ? `//${location.hostname}:8643/relay` : `//${location.host}/relay`;
const relayUrl = (location.protocol === 'https:' ? 'wss:' : 'ws:') + RELAY;
const roomsUrl = location.protocol + RELAY + '/rooms';

// Online: the games on the relay, refreshed while this page shows. Joining or hosting
// starts the game through DirectPlay lobby launch (its LOBBY switch,
// src/net/dplay_net.c): straight into that game's setup screen.
const playerName = () => ($('player').value.trim() || 'Player').slice(0, 15);
async function listRooms() {
  if ($('setup').hidden) return;
  let rooms = [];
  try { rooms = (await (await fetch(roomsUrl, { cache: 'no-store' })).json()).rooms; } catch { $('rooms').textContent = 'The game server cannot be reached.'; return; }
  const box = $('rooms'), key = JSON.stringify(rooms) + $('play').hidden;
  if (box.dataset.key === key) return;                  // unchanged: keep the buttons under the mouse
  box.dataset.key = key;
  box.replaceChildren();
  if (!rooms.length) { box.textContent = 'No games yet: host one.'; return; }
  for (const r of rooms) {
    const row = document.createElement('div');
    row.className = 'room';
    const info = document.createElement('span');
    info.textContent = `${r.name} — ${r.players.length}/${r.max} (${r.players.join(', ')})${r.open ? '' : ' · playing'}`;
    const b = document.createElement('button');
    b.textContent = 'Join';
    b.disabled = !r.open || r.players.length >= r.max || $('play').hidden;
    b.onclick = () => play({ lobby: 'join:' + r.id, room: r.name });
    row.append(info, b);
    box.append(row);
  }
}
setInterval(listRooms, 3000);

async function play(launch) {
  if (!crossOriginIsolated) { log('This page must be served cross-origin isolated (tools/web/serve.py does).'); return; }
  if (!$('eula').checked) { log('The game runs only once its license agreement is accepted (the checkbox above).'); return; }
  const exe = await copiedExe().catch(() => null);
  const problem = exe ? await exeProblem(exe) : 'The copy in this browser has no age2_x1\\age2_x1.exe.';
  if (problem) { log(problem + ' Remove the copy and copy a matching install.'); return; }
  await videoStandIns(await root()).catch(() => {});    // copies made before the stand-ins existed
  $('setup').hidden = true;
  const canvas = $('canvas');
  canvas.hidden = false; canvas.focus();
  const s = document.createElement('script');
  s.src = 'aoe2.js';
  const config = {
    canvas,
    arguments: [],
    preRun: [(m) => {
      m.ENV.AOE2_ACCEPT_EULA = '1';                     // the owner's answer (src/linux/aoc_shims.c)
      if (launch && launch.lobby) {                     // a game from the list, or a new one
        m.ENV.AOE2_ARGS = 'LOBBY';
        m.ENV.AOE2_LOBBY = launch.lobby;
        m.ENV.AOE2_ROOM = launch.room || '';
        m.ENV.AOE2_PLAYER = playerName();
      }
    }],
    relayUrl,
    print: (t) => console.log(t),
    printErr: (t) => console.warn(t),
  };
  Object.assign(config, presenter(config));             // the module object is this config
  s.onload = () => AoE2(config).catch((e) => { $('setup').hidden = false; log('the game stopped: ' + e); });
  document.body.appendChild(s);
}

const guard = (f) => async () => {
  for (const b of document.querySelectorAll('button')) b.disabled = true;
  try { await f(); } catch (e) { log('error: ' + e.message); console.error(e); }
  for (const b of document.querySelectorAll('button')) b.disabled = false;
  await refresh();
};

async function init() {
  const complete = await isComplete();
  if (complete) { await refresh(); return; }

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
    return;
  }

  await refresh();
}

$('pick').onclick = guard(importFolder);
$('server').onclick = guard(importServer);
$('forget').onclick = guard(forget);
$('play').onclick = () => play();
$('host').onclick = () => play({ lobby: 'host', room: ($('roomname').value.trim() || `${playerName()}'s game`).slice(0, 30) });
try { $('player').value = localStorage.getItem('aoe2.player') || ''; } catch {}
$('player').oninput = () => { try { localStorage.setItem('aoe2.player', $('player').value); } catch {} };
listRooms();
navigator.storage.persist?.();
init();
