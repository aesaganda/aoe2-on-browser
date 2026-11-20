// The page around the WebAssembly build (Phase 2 & 3, ADR-0005 / ADR-0006).
//
// On first visit, the game's files are automatically downloaded from the server
// (/install/...) into the origin private file system (OPFS) under aoe2/, and the
// module (build/web/aoe2.js) mounts OPFS and runs the game from /opfs/aoe2
// (src/web/web_fs.c). On subsequent visits the cached OPFS copy boots directly.
'use strict';
const $ = (id) => document.getElementById(id);
const log = (s) => {
  const el = $('log');
  if (!el) return;
  el.textContent += s + '\n';
  el.scrollTop = 1e9;
};
const setLoadPhase = (text) => {
  const el = $('load-phase');
  if (el) el.textContent = text;
};
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

// The game bundle as one tar stream. Servers that list it in install/bundle.json
// serve it in chunks under a versioned URL, which a CDN can cache (Cloudflare's free
// plan does not cache the 500+ MB whole); those are fetched a few ahead, and a chunk
// that breaks off is fetched again from where it stopped. Older servers send
// install/bundle.tar in one piece.
async function openBundle() {
  let info = null;
  try {
    const r = await fetch('install/bundle.json', { cache: 'no-store' });
    if (r.ok) info = await r.json();
  } catch { /* no chunked bundle on this server */ }
  if (!info || !(info.count > 0)) {
    const r = await fetchRetry('install/bundle.tar', { cache: 'no-store' });
    if (!r.body) throw new Error('ReadableStream not supported for bundle.tar');
    return { total: parseInt(r.headers.get('content-length') || '0', 10), reader: r.body.getReader() };
  }

  const AHEAD = 3;
  const pending = new Map();
  const want = (k) => {
    if (k >= info.count || pending.has(k)) return;
    /* no-store: the files end up in OPFS, so a second copy in the HTTP cache only wastes disk */
    const p = fetchRetry(`install/bundle/${info.tag}/${k}.tar`, { cache: 'no-store' });
    p.catch(() => {});                  /* awaited (and reported) when its turn comes */
    pending.set(k, p);
  };
  const length = (k) => Math.min(info.chunk, info.size - k * info.chunk);
  let i = 0, body = null, sent = 0, read = 0, attempt = 0;
  const stream = new ReadableStream({
    async pull(ctl) {
      while (true) {
        if (i >= info.count) { ctl.close(); return; }
        try {
          if (!body) {
            for (let k = i; k < i + AHEAD; k++) want(k);
            const r = await pending.get(i);
            if (!r.body) throw new Error('ReadableStream not supported for the bundle');
            body = r.body.getReader();
            read = 0;
          }
          const { done, value } = await body.read();
          if (done) {
            if (read !== length(i)) throw new Error(`bundle chunk ${i}: ${read} of ${length(i)} bytes`);
            pending.delete(i);
            body = null, sent = 0, attempt = 0, i++;
            continue;
          }
          const skip = Math.max(0, sent - read);  /* bytes a broken-off try already gave */
          read += value.length;
          if (skip >= value.length) continue;
          const out = skip ? value.subarray(skip) : value;
          sent += out.length;
          ctl.enqueue(out);
          return;
        } catch (e) {
          body = null;
          pending.delete(i);
          if (++attempt > 3) throw e;
          await new Promise((res) => setTimeout(res, 1000 * attempt));
        }
      }
    },
  });
  return { total: info.size, reader: stream.getReader() };
}

async function importBundle(base, loadText, loadBar, loadSub) {
  setLoadPhase('Step 1/2 · Game Assets');
  const { total, reader } = await openBundle();
  if (total > 0) await checkStorage(total);

  loadText.textContent = 'Downloading & caching game files…';
  loadBar.value = 0;
  const start = performance.now();
  let doneBytes = 0;
  let fileCount = 0;
  const totalMB = total > 0 ? (total / 1e6).toFixed(0) : '?';

  const updateProgress = (n) => {
    doneBytes += n;
    if (total > 0) loadBar.value = Math.min(1, doneBytes / total);
    const elapsed = (performance.now() - start) / 1000;
    const doneMB = (doneBytes / 1e6).toFixed(0);
    if (elapsed > 0.15 && doneBytes > 0) {
      const speed = doneBytes / elapsed;
      const mb = (speed / 1e6).toFixed(1);
      if (total > 0) {
        const left = Math.max(0, (total - doneBytes) / speed);
        const pct = Math.floor(Math.min(100, (doneBytes / total) * 100));
        const eta = left < 60 ? `${Math.ceil(left)}s` : `${Math.floor(left / 60)}m ${Math.ceil(left % 60)}s`;
        loadSub.textContent = `${pct}% (${doneMB} / ${totalMB} MB) · ${mb} MB/s — ${eta} left`;
      } else {
        loadSub.textContent = `${doneMB} MB · ${mb} MB/s`;
      }
    }
  };

  const dirHandles = new Map([['', base]]);
  const ensureDir = async (dirPath) => {
    if (!dirPath) return base;
    let cached = dirHandles.get(dirPath);
    if (cached) return cached;
    const parts = dirPath.split('/');
    let cur = base;
    let acc = '';
    for (const part of parts) {
      acc = acc ? acc + '/' + part : part;
      let next = dirHandles.get(acc);
      if (!next) {
        next = await cur.getDirectoryHandle(part, { create: true });
        dirHandles.set(acc, next);
      }
      cur = next;
    }
    return cur;
  };

  const inFlight = new Set();
  let inFlightBytes = 0;
  const enqueueWrite = async (dir, name, data) => {
    while (inFlight.size >= 8 || (inFlight.size > 0 && inFlightBytes + data.byteLength > 32 * 1024 * 1024)) {
      await Promise.race(inFlight);
    }
    inFlightBytes += data.byteLength;
    const p = writeFileToDir(dir, name, data).finally(() => {
      inFlight.delete(p);
      inFlightBytes -= data.byteLength;
    });
    inFlight.add(p);
  };

  const dec = new TextDecoder();
  const readStr = (buf, s, e) => {
    let end = s;
    while (end < e && buf[end] !== 0) end++;
    return dec.decode(buf.subarray(s, end));
  };

  let state = 'header';
  const hdrBuf = new Uint8Array(512);
  let hdrLen = 0;
  let fileBuf = null;
  let filePos = 0;
  let fileSize = 0;
  let padRem = 0;
  let curDir = base;
  let curName = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    updateProgress(value.length);
    let off = 0;
    while (off < value.length && state !== 'done') {
      if (state === 'header') {
        const take = Math.min(512 - hdrLen, value.length - off);
        hdrBuf.set(value.subarray(off, off + take), hdrLen);
        hdrLen += take;
        off += take;
        if (hdrLen < 512) continue;
        hdrLen = 0;
        let allZero = true;
        for (let i = 0; i < 512; i++) {
          if (hdrBuf[i] !== 0) { allZero = false; break; }
        }
        if (allZero) { state = 'done'; break; }
        const name = readStr(hdrBuf, 0, 100);
        const prefix = readStr(hdrBuf, 345, 500);
        let rel = prefix ? `${prefix}/${name}` : name;
        if (rel.startsWith('./')) rel = rel.slice(2);
        const typeflag = hdrBuf[156];
        fileSize = parseInt(readStr(hdrBuf, 124, 136).trim(), 8) || 0;
        padRem = (512 - (fileSize % 512)) % 512;
        if (typeflag === 53 || rel.endsWith('/')) {
          await ensureDir(rel.replace(/\/+$/, ''));
          continue;
        }
        const slash = rel.lastIndexOf('/');
        curDir = await ensureDir(slash === -1 ? '' : rel.slice(0, slash));
        curName = slash === -1 ? rel : rel.slice(slash + 1);
        fileCount++;
        if (fileSize === 0) {
          await enqueueWrite(curDir, curName, new Uint8Array(0));
        } else {
          fileBuf = new Uint8Array(fileSize);
          filePos = 0;
          state = 'data';
        }
      } else if (state === 'data') {
        const take = Math.min(fileSize - filePos, value.length - off);
        fileBuf.set(value.subarray(off, off + take), filePos);
        filePos += take;
        off += take;
        if (filePos === fileSize) {
          const data = fileBuf;
          fileBuf = null;
          await enqueueWrite(curDir, curName, data);
          state = padRem > 0 ? 'pad' : 'header';
        }
      } else if (state === 'pad') {
        const take = Math.min(padRem, value.length - off);
        padRem -= take;
        off += take;
        if (padRem === 0) state = 'header';
      }
    }
  }

  if (state === 'data' || state === 'pad' || hdrLen > 0) {
    throw new Error('Incomplete game bundle stream');
  }
  await Promise.all(inFlight);

  loadText.textContent = 'Finalizing game files…';
  loadSub.textContent = '';
  await finish(base);
  log(`Cached ${fileCount} game files (${(doneBytes / 1e6).toFixed(0)} MB) in browser storage.`);
}

async function importManifest(base, loadText, loadBar, loadSub) {
  setLoadPhase('Step 1/2 · Game Assets');
  const manifestResp = await fetchRetry('install/manifest.json', { cache: 'no-store' });
  const list = await manifestResp.json();
  const files = list.files.filter(([p]) => !skip(p));
  const total = files.reduce((s, [, n]) => s + n, 0);
  await checkStorage(total);

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

  loadText.textContent = 'Downloading & caching game files…';
  loadBar.value = 0;
  const start = performance.now();
  let done = 0;
  const totalMB = (total / 1e6).toFixed(0);

  const updateProgress = (bytes) => {
    done += bytes;
    loadBar.value = total ? done / total : 1;
    const elapsed = (performance.now() - start) / 1000;
    if (elapsed > 0.15 && done > 0) {
      const speed = done / elapsed;
      const left = Math.max(0, (total - done) / speed);
      const mb = (speed / 1e6).toFixed(1);
      const pct = total ? Math.floor((done / total) * 100) : 100;
      const eta = left < 60 ? `${Math.ceil(left)}s` : `${Math.floor(left / 60)}m ${Math.ceil(left % 60)}s`;
      loadSub.textContent = `${pct}% (${(done / 1e6).toFixed(0)} / ${totalMB} MB) · ${mb} MB/s — ${eta} left`;
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
  log(`Cached ${files.length} game files (${totalMB} MB) in browser storage.`);
}

async function importServer() {
  const loadText = $('load-text');
  const loadBar = $('load-bar');
  const loadSub = $('load-sub');
  setLoadPhase('Step 1/2 · Game Assets');
  loadText.textContent = 'Checking game files…';
  loadBar.hidden = false;
  loadBar.removeAttribute('value');
  loadSub.textContent = '';

  const base = await root();
  try {
    await importBundle(base, loadText, loadBar, loadSub);
  } catch (e) {
    if (e.message && e.message.startsWith('Insufficient browser storage')) throw e;
    console.warn('[install] bundle.tar unavailable, falling back to manifest:', e);
    await importManifest(base, loadText, loadBar, loadSub);
  }
}

async function refresh() {
  const ok = await isComplete();
  $('play').hidden = !ok;
  $('play').textContent = gameRunning ? 'Resume Game (Esc)' : 'Play';
  $('saves-btn').hidden = !ok;
  $('import-rec').hidden = !ok;
  $('online').hidden = false;
  $('host').disabled = !ok;
  if (ok) {
    try {
      const files = await listSaves();
      $('save-count').textContent = files.length ? `${files.length} saved file${files.length > 1 ? 's' : ''}` : '';
    } catch { /* no save/scenario dir yet */ }
    try {
      const { usage } = await navigator.storage.estimate();
      const usedMB = (usage / 1e6).toFixed(0);
      $('storage-info').textContent = `${usedMB} MB cached`;
    } catch {}
  }
}

// --- FPS counter ---
let fpsFrames = 0, fpsLast = 0;
function tickFps() {
  fpsFrames++;
  const now = performance.now();
  if (now - fpsLast >= 1000) {
    $('fps').textContent = fpsFrames + ' fps';
    fpsFrames = 0;
    fpsLast = now;
  }
}

// --- Save game, replay & scenario management ---
const SAVE_EXT_INFO = {
  '.gax': { kind: 'Save', cls: '', folder: 'savegame' },
  '.gam': { kind: 'Save', cls: '', folder: 'savegame' },
  '.mgx': { kind: 'Replay', cls: 'replay', folder: 'savegame' },
  '.mgl': { kind: 'Replay', cls: 'replay', folder: 'savegame' },
  '.mgz': { kind: 'Replay', cls: 'replay', folder: 'savegame' },
  '.msx': { kind: 'MP Save', cls: 'mp', folder: 'savegame/multi' },
  '.mgs': { kind: 'MP Save', cls: 'mp', folder: 'savegame/multi' },
  '.scx': { kind: 'Scenario', cls: 'scenario', folder: 'scenario' },
  '.scn': { kind: 'Scenario', cls: 'scenario', folder: 'scenario' },
};

function saveFileMeta(name) {
  const idx = name.lastIndexOf('.');
  return idx >= 0 ? (SAVE_EXT_INFO[name.slice(idx).toLowerCase()] || null) : null;
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return '0 KB';
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1e6).toFixed(1)} MB`;
}

function notifyFsChanged() {
  try { activeModule?._web_fs_notify_change?.(); } catch {}
}

async function findDirsCI(parent, lowerName) {
  const out = [];
  try {
    for await (const [n, h] of parent.entries()) {
      if (h.kind === 'directory' && n.toLowerCase() === lowerName) out.push(h);
    }
  } catch {}
  return out;
}

async function getTargetDirForExt(folderPath, create) {
  let cur = await root();
  for (const seg of folderPath.split('/')) {
    let next = null;
    try { next = await cur.getDirectoryHandle(seg); } catch {}
    if (!next) {
      const matches = await findDirsCI(cur, seg.toLowerCase());
      if (matches.length) next = matches[0];
    }
    if (!next && create) next = await cur.getDirectoryHandle(seg, { create: true });
    if (!next) return null;
    cur = next;
  }
  return cur;
}

async function collectSaveFilesFromDir(dirHandle, files) {
  for await (const [name, h] of dirHandle.entries()) {
    if (h.kind !== 'file') continue;
    const meta = saveFileMeta(name);
    if (!meta) continue;
    const f = await h.getFile();
    files.push({ name, size: f.size, modified: f.lastModified, handle: h, dirHandle, meta });
  }
}

async function listSaves() {
  const r = await root();
  const files = [];
  for (const dir of await findDirsCI(r, 'savegame')) {
    await collectSaveFilesFromDir(dir, files);
    for (const multi of await findDirsCI(dir, 'multi')) {
      await collectSaveFilesFromDir(multi, files);
    }
  }
  for (const dir of await findDirsCI(r, 'scenario')) {
    await collectSaveFilesFromDir(dir, files);
  }
  files.sort((a, b) => b.modified - a.modified);
  return files;
}

async function showSaves(forceOpen = false) {
  const list = $('saves-list');
  if (!forceOpen && !list.hidden) { list.hidden = true; return; }
  const files = await listSaves();
  list.replaceChildren();
  if (!files.length) {
    list.textContent = 'No saved games, replays, or scenarios yet.';
    list.hidden = false;
    return;
  }
  for (const f of files) {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:8px;padding:5px 0;border-bottom:1px solid #252730;font-size:13px';
    const info = document.createElement('span');
    info.style.cssText = 'min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--dim)';
    const badge = document.createElement('span');
    badge.className = 'save-badge' + (f.meta.cls ? ' ' + f.meta.cls : '');
    badge.textContent = f.meta.kind;
    const dateStr = new Date(f.modified).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    info.append(badge, document.createTextNode(`${f.name}  (${formatBytes(f.size)} · ${dateStr})`));
    const actions = document.createElement('span');
    actions.style.cssText = 'display:flex;gap:4px;flex-shrink:0';
    const dl = document.createElement('button');
    dl.textContent = 'Download';
    dl.style.cssText = 'margin:0;padding:3px 9px;font-size:12px';
    dl.onclick = async () => {
      const file = await f.handle.getFile();
      const url = URL.createObjectURL(file);
      const a = document.createElement('a');
      a.href = url; a.download = f.name;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    };
    const del = document.createElement('button');
    del.textContent = '×';
    del.title = 'Delete ' + f.name;
    del.style.cssText = 'margin:0;padding:3px 8px;font-size:12px;color:var(--err);border-color:transparent;background:transparent';
    del.onclick = async () => {
      if (!confirm(`Delete "${f.name}"?`)) return;
      await f.dirHandle.removeEntry(f.name);
      notifyFsChanged();
      row.remove();
      const remaining = list.querySelectorAll('div').length;
      if (!remaining) list.textContent = 'No saved games, replays, or scenarios yet.';
      $('save-count').textContent = remaining ? `${remaining} saved file${remaining > 1 ? 's' : ''}` : '';
    };
    actions.append(dl, del);
    row.append(info, actions);
    list.append(row);
  }
  list.hidden = false;
}

async function importFiles(fileList) {
  const list = Array.from(fileList || []);
  if (!list.length) return;
  let imported = 0, skipped = 0, firstName = '';
  for (const file of list) {
    const meta = saveFileMeta(file.name);
    if (!meta) { skipped++; continue; }
    const dir = await getTargetDirForExt(meta.folder, true);
    await writeFileToDir(dir, file.name, file);
    if (!firstName) firstName = file.name;
    log(`Imported ${meta.kind.toLowerCase()}: ${file.name} (${meta.folder}/)`);
    imported++;
  }
  if (imported) {
    notifyFsChanged();
    await refresh();
    if (!$('saves-list').hidden) await showSaves(true);
    toast(imported === 1 ? `Imported ${firstName}` : `Imported ${imported} files`);
  } else if (skipped) {
    toast('Unsupported file type (use .gax, .gam, .mgx, .mgl, .mgz, .msx, .scx, .scn)', 3500);
  }
}

async function importRecording() {
  return new Promise((resolve) => {
    const input = $('rec-input');
    input.onchange = async () => {
      try { await importFiles(input.files); } finally { input.value = ''; resolve(); }
    };
    input.click();
  });
}

function initWebGL(canvas) {
  const opts = { alpha: false, depth: false, stencil: false, antialias: false, preserveDrawingBuffer: false };
  const gl = canvas.getContext('webgl2', opts) || canvas.getContext('webgl', opts);
  if (!gl) return null;
  const compile = (type, src) => {
    const sh = gl.createShader(type);
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) { gl.deleteShader(sh); return null; }
    return sh;
  };
  const vs = compile(gl.VERTEX_SHADER,
    'attribute vec2 aPos; varying vec2 vTex; void main(){ vTex = vec2(aPos.x * 0.5 + 0.5, 0.5 - aPos.y * 0.5); gl_Position = vec4(aPos, 0.0, 1.0); }');
  const fs = compile(gl.FRAGMENT_SHADER,
    'precision mediump float; varying vec2 vTex; uniform sampler2D uTex; void main(){ gl_FragColor = texture2D(uTex, vTex); }');
  if (!vs || !fs) return null;
  const prog = gl.createProgram();
  gl.attachShader(prog, vs);
  gl.attachShader(prog, fs);
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return null;
  gl.useProgram(prog);
  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(prog, 'aPos');
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  const tex = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, tex);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.pixelStorei(gl.UNPACK_ALIGNMENT, 2);
  let curW = 0, curH = 0, curSmooth = null, scratch = null;
  return (src, w, h, smooth) => {
    if (curW !== w || curH !== h) {
      canvas.width = w; canvas.height = h;
      gl.viewport(0, 0, w, h);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, w, h, 0, gl.RGB, gl.UNSIGNED_SHORT_5_6_5, null);
      scratch = new Uint16Array(w * h);
      curW = w; curH = h;
    }
    const wantSmooth = Boolean(smooth);
    if (curSmooth !== wantSmooth) {
      const filt = wantSmooth ? gl.LINEAR : gl.NEAREST;
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filt);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filt);
      curSmooth = wantSmooth;
    }
    scratch.set(src);
    gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, w, h, gl.RGB, gl.UNSIGNED_SHORT_5_6_5, scratch);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  };
}

const VALID_RES = ['800', '1024', '1280', '1280x720', '1366x768', '1600x900', '1920x1080'];
const WIDE_RES = [[1920, 1080], [1600, 900], [1366, 768], [1280, 720]];
// 'auto' picks the largest 16:9 mode that fits the physical screen, so a
// 1080p monitor renders 1:1 instead of upscaling 1366x768 (and a 4K one gets a clean 2x).
function resolveAutoRes() {
  const dpr = window.devicePixelRatio || 1;
  const pw = Math.round((screen.width || window.innerWidth) * dpr);
  const ph = Math.round((screen.height || window.innerHeight) * dpr);
  const sw = Math.max(pw, ph), sh = Math.min(pw, ph);
  if (sw / sh < 1.5) return sw >= 1280 && sh >= 1024 ? '1280' : (sw >= 1024 && sh >= 768 ? '1024' : '800');
  for (const [w, h] of WIDE_RES) if (w <= sw && h <= sh) return `${w}x${h}`;
  return '1280x720';
}
function resolveRes(pref) { return pref === 'auto' ? resolveAutoRes() : pref; }
function resLabel(res) {
  if (res === '800') return '800×600';
  if (res === '1024') return '1024×768';
  if (res === '1280') return '1280×1024';
  return res.replace('x', '×');
}
// Stored under a new key so everyone starts on 'auto' once; the old key was
// overwritten with the in-game frame size, so it can't tell a real choice apart.
let selectedRes = (() => {
  try {
    const v = localStorage.getItem('aoe2.res2');
    return v === 'auto' || VALID_RES.includes(v) ? v : 'auto';
  } catch { return 'auto'; }
})();
let launchedRes = resolveRes(selectedRes);
if ($('res-auto-opt')) $('res-auto-opt').textContent = `Auto — match screen (${resLabel(resolveAutoRes())})`;
if ($('res-select')) $('res-select').value = selectedRes;

const SCALE_MODES = ['smooth', 'sharp', 'crt'];
const SCALE_LABELS = { smooth: 'Smooth', sharp: 'Sharp', crt: 'CRT' };
let scaleMode = (() => {
  try {
    const v = localStorage.getItem('aoe2.scale');
    return SCALE_MODES.includes(v) ? v : 'smooth';
  } catch { return 'smooth'; }
})();

function applyScaleMode(nextMode, showToast) {
  scaleMode = SCALE_MODES.includes(nextMode) ? nextMode : 'smooth';
  try { localStorage.setItem('aoe2.scale', scaleMode); } catch {}
  const scr = $('screen'), scan = $('crt-scanlines');
  if (scr) {
    scr.classList.toggle('smooth', scaleMode === 'smooth');
    scr.classList.toggle('crt', scaleMode === 'crt');
  }
  if (scan) scan.hidden = (scaleMode !== 'crt') || !scr || scr.hidden;
  if ($('scale-select')) $('scale-select').value = scaleMode;
  if ($('scale-btn')) $('scale-btn').textContent = SCALE_LABELS[scaleMode] || 'Smooth';
  if (showToast) toast(`Scaling: ${SCALE_LABELS[scaleMode] || scaleMode}`, 1400);
}
applyScaleMode(scaleMode, false);

// --- Top HUD bar: an overlay that never takes space from the game picture ---
// It slides away a few seconds after the game starts and comes back when the
// mouse rests on the top edge (or with Alt+H, which pins it open).
let hudPinned = false, hudHideTimer = 0, hudEdgeTimer = 0, hudHintShown = false;
const HUD_EDGE_PX = 3, HUD_EDGE_DWELL_MS = 450;
function hudVisible() { return !document.body.classList.contains('hud-hidden'); }
function showHud(pin) {
  clearTimeout(hudHideTimer);
  clearTimeout(hudEdgeTimer);
  hudEdgeTimer = 0;
  if (pin) hudPinned = true;
  document.body.classList.remove('hud-hidden');
}
function hideHud() {
  hudPinned = false;
  clearTimeout(hudHideTimer);
  document.body.classList.add('hud-hidden');
  const ui = $('game-ui');
  if (ui && ui.contains(document.activeElement) && gameRunning && $('setup').hidden) $('canvas').focus();
}
function scheduleHudHide(ms) {
  if (hudPinned) return;
  clearTimeout(hudHideTimer);
  hudHideTimer = setTimeout(() => {
    const ui = $('game-ui');
    if (hudPinned || !gameRunning || (ui && ui.matches(':hover'))) return;
    hideHud();
  }, ms);
}
function toggleHud() {
  if (hudVisible() && hudPinned) hideHud();
  else showHud(true);
}
window.addEventListener('mousemove', (e) => {
  if (!gameRunning || !$('setup').hidden || $('game-ui').hidden) return;
  if (!hudVisible()) {
    if (e.clientY <= HUD_EDGE_PX) {
      if (!hudEdgeTimer) hudEdgeTimer = setTimeout(() => { hudEdgeTimer = 0; showHud(false); }, HUD_EDGE_DWELL_MS);
    } else if (hudEdgeTimer) {
      clearTimeout(hudEdgeTimer);
      hudEdgeTimer = 0;
    }
  } else if (!hudPinned) {
    const barBottom = $('game-ui').getBoundingClientRect().bottom;
    if (e.clientY > barBottom + 24) scheduleHudHide(600);
    else clearTimeout(hudHideTimer);
  }
});

let lastBox = null;
let currentFrameW = 0, currentFrameH = 0;
function syncScreenRect() {
  const sdl = $('canvas'), scr = $('screen'), scan = $('crt-scanlines');
  if (!lastBox || !sdl || !scr) return;
  const { dx, dy, dw, dh, cw, ch } = lastBox;
  if (cw <= 0 || ch <= 0) return;
  let r = sdl.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) {
    const ar = (currentFrameW > 0 && currentFrameH > 0) ? (currentFrameW / currentFrameH) : (4 / 3);
    const vw = window.innerWidth, vh = Math.max(100, window.innerHeight);
    const w = Math.min(vw, vh * ar);
    const h = Math.min(vh, vw / ar);
    r = { left: (vw - w) / 2, top: (vh - h) / 2, width: w, height: h };
  }
  const sx = r.width / cw, sy = r.height / ch;
  const box = {
    left: `${r.left + dx * sx}px`,
    top: `${r.top + dy * sy}px`,
    width: `${dw * sx}px`,
    height: `${dh * sy}px`,
  };
  Object.assign(scr.style, box);
  if (scan) Object.assign(scan.style, box);
}
window.addEventListener('resize', syncScreenRect);
document.addEventListener('fullscreenchange', () => {
  syncScreenRect();
  requestAnimationFrame(syncScreenRect);
});

function presenter(module) {
  const sdl = $('canvas'), scr = $('screen'), scan = $('crt-scanlines');
  const glDraw = initWebGL(scr);
  const ctx = glDraw ? null : scr.getContext('2d');
  let img = null, firstFrame = true, lastFrameW = 0, lastFrameH = 0, lastSdlMode = null;
  return {
    ra2Present(fb, w, h, dx, dy, dw, dh, mode, cw, ch) {
      if (document.hidden && !firstFrame) return;
      if (sdl.hidden) sdl.hidden = false;
      if (lastSdlMode !== null && mode !== lastSdlMode) {
        const mapped = mode === 2 ? 'crt' : (mode >= 3 ? 'sharp' : 'smooth');
        applyScaleMode(mapped, true);
      }
      lastSdlMode = mode;
      const src = module.HEAPU16.subarray(fb >> 1, (fb >> 1) + w * h);
      if (glDraw) {
        glDraw(src, w, h, scaleMode === 'smooth');
      } else {
        if (!img || img.width !== w || img.height !== h) { scr.width = w; scr.height = h; img = ctx.createImageData(w, h); }
        const out = new Uint32Array(img.data.buffer);
        for (let i = 0; i < src.length; i++) {
          const v = src[i], r = (v >> 11) & 31, g = (v >> 5) & 63, b = v & 31;
          out[i] = 0xFF000000 | (((b << 3) | (b >> 2)) << 16) | (((g << 2) | (g >> 4)) << 8) | ((r << 3) | (r >> 2));
        }
        ctx.putImageData(img, 0, 0);
      }
      if (lastFrameW !== w || lastFrameH !== h) {
        lastFrameW = w;
        lastFrameH = h;
        currentFrameW = w;
        currentFrameH = h;
        document.documentElement.style.setProperty('--game-ar', String(w / h));
        sdl.style.width = '';
        sdl.style.height = '';
        if ($('res-badge')) $('res-badge').textContent = `${w}×${h}`;
        // AoE2 always uses 800x600 on menus and 640x480 on videos; only sync high-res / widescreen match modes.
        const modeKey = (w === 1024 && h === 768) ? '1024'
                      : (w === 1280 && h === 1024) ? '1280'
                      : VALID_RES.includes(`${w}x${h}`) ? `${w}x${h}` : '';
        if (modeKey) {
          launchedRes = modeKey;
          // Keep 'auto' while the game runs at the auto-picked mode; only a different
          // mode (changed from the in-game options) becomes an explicit choice.
          if (!(selectedRes === 'auto' && modeKey === resolveAutoRes())) {
            selectedRes = modeKey;
            try { localStorage.setItem('aoe2.res2', selectedRes); } catch {}
            if ($('res-select')) $('res-select').value = selectedRes;
          }
          if ($('apply-res')) $('apply-res').hidden = true;
        }
      }
      lastBox = { dx, dy, dw, dh, cw, ch };
      syncScreenRect();
      scr.hidden = false;
      if (scan) scan.hidden = (scaleMode !== 'crt');
      tickFps();
      if (firstFrame) {
        $('loading').hidden = true;
        $('game-ui').hidden = false;
        showHud(false);
        scheduleHudHide(4000);
        if (!hudHintShown) {
          hudHintShown = true;
          setTimeout(() => toast('Top bar auto-hides — rest the mouse on the top edge or press Alt+H', 4000), 4200);
        }
        syncScreenRect();
        requestAnimationFrame(syncScreenRect);
        fpsLast = performance.now();
        firstFrame = false;
        try { activeModule?._web_set_hidden?.(document.hidden ? 1 : 0); } catch {}
        if (activeRoomName || hostedRoom) setTimeout(measurePing, 2500);
        if ($('setup').hidden) sdl.focus();
      }
    },
    ra2Menu(open) {
      scr.hidden = !!open;
      if (scan) scan.hidden = !!open || (scaleMode !== 'crt');
    },
  };
}

// --- Audio context resume & mute keep-alive (keeps Chromium active-audio tab exemption) ---
function startSilentKeepAlive(ctx) {
  if (ctx._keepAliveStarted || ctx.state !== 'running') return;
  ctx._keepAliveStarted = true;
  try {
    const osc = ctx.createOscillator();
    const g = ctx.createGain();
    g.gain.value = 0;
    osc.connect(g);
    g.connect(ctx._realDestination || ctx.destination);
    osc.start();
  } catch {}
}

function resumeAudioContexts() {
  const all = [];
  if (Array.isArray(window._audioContexts)) all.push(...window._audioContexts);
  if (typeof SDL === 'object' && SDL.audioContext && !all.includes(SDL.audioContext)) all.push(SDL.audioContext);
  for (const ctx of all) {
    if (ctx.state === 'suspended') {
      ctx.resume().then(() => startSilentKeepAlive(ctx)).catch(() => {});
    } else {
      startSilentKeepAlive(ctx);
    }
  }
}
for (const evt of ['click', 'keydown', 'touchstart']) {
  document.addEventListener(evt, resumeAudioContexts, { capture: true, passive: true });
}
let muted = false;
let masterVolume = (() => {
  try {
    const v = parseFloat(localStorage.getItem('aoe2.volume'));
    return Number.isFinite(v) ? Math.min(1, Math.max(0, v)) : 0.8;
  } catch { return 0.8; }
})();
if ($('volume-slider')) $('volume-slider').value = String(Math.round(masterVolume * 100));

function applyAudioGain() {
  const all = [];
  if (Array.isArray(window._audioContexts)) all.push(...window._audioContexts);
  if (typeof SDL === 'object' && SDL.audioContext && !all.includes(SDL.audioContext)) all.push(SDL.audioContext);
  const effectiveMuted = muted || masterVolume <= 0;
  for (const ctx of all) {
    if (ctx._masterGain) {
      ctx._masterGain.gain.value = muted ? 0 : masterVolume;
    } else {
      if (effectiveMuted) ctx.suspend().catch(() => {});
      else ctx.resume().catch(() => {});
    }
    if (!effectiveMuted && ctx.state === 'suspended') ctx.resume().catch(() => {});
  }
  if ($('mute-btn')) {
    $('mute-btn').textContent = effectiveMuted ? '🔇' : '🔊';
    $('mute-btn').title = effectiveMuted ? 'Unmute audio (Alt+M)' : 'Mute audio (Alt+M)';
  }
}

function setVolume(val) {
  masterVolume = Math.min(1, Math.max(0, Number(val) || 0));
  if (masterVolume > 0 && muted) muted = false;
  try { localStorage.setItem('aoe2.volume', String(masterVolume)); } catch {}
  applyAudioGain();
}

const OrigAudioContext = window.AudioContext || window.webkitAudioContext;
if (OrigAudioContext) {
  window._audioContexts = [];
  const OrigCtor = OrigAudioContext;
  window.AudioContext = function (...args) {
    const ctx = new OrigCtor(...args);
    try {
      const realDest = ctx.destination;
      const master = ctx.createGain();
      master.gain.value = muted ? 0 : masterVolume;
      master.connect(realDest);
      ctx._realDestination = realDest;
      ctx._masterGain = master;
      Object.defineProperty(ctx, 'destination', { get: () => master, configurable: true });
    } catch {}
    window._audioContexts.push(ctx);
    return ctx;
  };
  window.AudioContext.prototype = OrigCtor.prototype;
  if (window.webkitAudioContext) window.webkitAudioContext = window.AudioContext;
}

// --- Mute ---
function toggleMute() {
  muted = !muted;
  if (!muted && masterVolume <= 0) {
    masterVolume = 0.8;
    if ($('volume-slider')) $('volume-slider').value = '80';
    try { localStorage.setItem('aoe2.volume', String(masterVolume)); } catch {}
  }
  applyAudioGain();
  toast(muted ? 'Audio muted' : 'Audio unmuted', 1500);
}

// --- Fullscreen ---
function toggleFullscreen() {
  if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
  } else {
    document.documentElement.requestFullscreen().catch(() => {});
  }
}

let serverConfig = { install: true, relay_same_origin: null, relay_ws_url: '' };
let serverWasmSize = 43 * 1e6;

function resolveRelayWsUrl() {
  if (serverConfig.relay_ws_url) return serverConfig.relay_ws_url;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  if (serverConfig.relay_same_origin === true || !location.port || location.port === '80' || location.port === '443') {
    return `${proto}//${location.host}/relay`;
  }
  return `${proto}//${location.hostname}:8643/relay`;
}

async function fetchRelay(subpath, timeoutMs = 5000) {
  try {
    const r = await fetch('relay' + subpath, { cache: 'no-store', signal: AbortSignal.timeout(timeoutMs) });
    if (r.ok || r.status < 500) return r;
  } catch {}
  if (serverConfig.relay_same_origin !== true && location.port === '8642') {
    return fetch(`${location.protocol}//${location.hostname}:8643/relay${subpath}`, {
      cache: 'no-store',
      signal: AbortSignal.timeout(timeoutMs),
    });
  }
  throw new Error('Relay unavailable');
}

async function copyToClipboard(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {}
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;left:-9999px;top:-9999px;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch {
    return false;
  }
}

// --- Connection status ---
let relayOk = false;
let consecutiveFails = 0;

function setStatus(ok, text, onlinePlayers = 0, openRooms = 0) {
  const el = $('status');
  el.textContent = text;
  el.className = ok ? 'ok' : 'err';
  relayOk = ok;
  $('rooms').classList.toggle('dimmed', !ok);

  const dot = $('hud-online-dot');
  const label = $('hud-online-label');
  if (dot) dot.className = ok ? 'ok' : 'err';
  if (label) {
    const count = onlinePlayers || openRooms;
    label.textContent = ok && count > 0 ? `Online (${count})` : 'Online';
  }
}

const playerName = () => ($('player').value.trim() || 'Player').slice(0, 15);
let currentInviteUrl = '';

function buildInviteUrl(room) {
  const u = new URL(location.href);
  u.search = `?join=${room.id}&room=${encodeURIComponent(room.name)}${room.password ? '&locked=1' : ''}`;
  return u.toString();
}

function formatRoomAge(ageS) {
  const s = Math.max(0, Math.floor(Number(ageS) || 0));
  if (s < 60) return '<1m';
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function updateHudRoomPill(rooms) {
  const pill = $('hud-room-pill');
  if (!pill) return;
  if (!gameRunning || (!hostedRoom && !activeRoomId && !activeRoomName)) {
    pill.hidden = true;
    pill.textContent = '';
    return;
  }
  const list = Array.isArray(rooms) ? rooms : [];
  const rm = list.find((r) => (activeRoomId && r.id === activeRoomId) || r.name === (hostedRoom || activeRoomName)) || null;
  if (rm) {
    const maxPlayers = Math.min(8, rm.max || 8);
    const roster = rm.players.length ? rm.players.join(', ') : 'waiting';
    pill.textContent = `👥 ${rm.players.length}/${maxPlayers} · ${rm.name}`;
    pill.title = `Room: ${rm.name} (${rm.open ? 'Lobby' : 'In Progress'}) — Players: ${roster}`;
    pill.hidden = false;
  } else if (activeRoomName || hostedRoom) {
    const name = hostedRoom || activeRoomName;
    pill.textContent = `👥 ${name}`;
    pill.title = `Room: ${name}`;
    pill.hidden = false;
  } else {
    pill.hidden = true;
  }
}

function updateInviteBanner(rooms) {
  const banner = $('invite-banner');
  const urlEl = $('invite-url');
  let targetRoom = null;
  if (hostedRoom && Array.isArray(rooms)) {
    targetRoom = rooms.find((r) => r.name === hostedRoom);
  }
  if (!targetRoom && Array.isArray(rooms) && rooms.length === 1 && rooms[0].open) {
    targetRoom = rooms[0];
  }
  currentInviteUrl = targetRoom ? buildInviteUrl(targetRoom) : '';
  if (banner && urlEl) {
    if (targetRoom) {
      urlEl.textContent = currentInviteUrl;
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
  }
  if ($('hud-invite-btn')) {
    $('hud-invite-btn').hidden = !(gameRunning && hostedRoom && targetRoom && targetRoom.open && currentInviteUrl);
  }
}

function checkHostedRoomJoins(rooms) {
  if (!hostedRoom || !Array.isArray(rooms)) return;
  const mine = rooms.find((r) => r.name === hostedRoom);
  if (mine) {
    for (const p of mine.players) {
      if (!knownPlayers.has(p)) notifyJoin(p);
    }
    knownPlayers = new Set(mine.players);
  }
}

function renderChat(chat) {
  const box = $('chat-messages');
  const key = JSON.stringify(chat);
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.replaceChildren();
  if (!chat.length) {
    const empty = document.createElement('div');
    empty.style.cssText = 'color:var(--dim);padding:4px 0';
    empty.textContent = 'Lobby chat is ready — say hi or coordinate a match.';
    box.append(empty);
    return;
  }
  for (const m of chat) {
    const line = document.createElement('div');
    line.style.padding = '2px 0';
    const timeStr = new Date((m.ts || 0) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const ts = document.createElement('span');
    ts.style.color = '#6e6c66';
    ts.textContent = `[${timeStr}] `;
    const who = document.createElement('strong');
    who.style.color = 'var(--accent)';
    who.textContent = m.from;
    line.append(ts, who, document.createTextNode(`: ${m.text}`));
    box.append(line);
  }
  box.scrollTop = box.scrollHeight;
}

async function sendChat() {
  const input = $('chat-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  try {
    const qs = `?from=${encodeURIComponent(playerName())}&text=${encodeURIComponent(text)}`;
    const r = await fetchRelay('/chat' + qs, 5000);
    if (r.ok) {
      const data = await r.json();
      if (data.chat) renderChat(data.chat);
    } else {
      throw new Error('HTTP ' + r.status);
    }
  } catch (e) { log('Chat send failed: ' + (e.message || e)); }
}

async function listRooms() {
  if ($('setup').hidden) return;
  let rooms = [];
  try {
    const t0 = performance.now();
    const r = await fetchRelay('/rooms', 5000);
    if (!r.ok) throw new Error(r.status);
    const data = await r.json();
    rooms = data.rooms || [];
    if (data.chat) renderChat(data.chat);
    const ping = Math.round(performance.now() - t0);
    const wasOffline = consecutiveFails >= 2;
    consecutiveFails = 0;
    pollBackoff = POLL_MIN;
    const totalPlayers = rooms.reduce((s, rm) => s + rm.players.length, 0);
    const onlineCount = typeof data.online === 'number' ? Math.max(data.online, totalPlayers) : totalPlayers;
    const statusParts = [];
    if (onlineCount > 0) statusParts.push(`${onlineCount} online`);
    if (rooms.length > 0) statusParts.push(`${rooms.length} ${rooms.length === 1 ? 'room' : 'rooms'}`);
    setStatus(true, statusParts.length ? `${statusParts.join(' · ')} · ${ping}ms` : `connected · ${ping}ms`, onlineCount, rooms.length);
    if (wasOffline) log('Reconnected to the game server.');
  } catch {
    consecutiveFails++;
    pollBackoff = Math.min(pollBackoff * 1.5, POLL_MAX);
    if (consecutiveFails >= 2) setStatus(false, 'offline');
    $('rooms').innerHTML = '<div class="rooms-empty">Cannot reach the multiplayer relay server.</div>';
    return;
  }

  checkHostedRoomJoins(rooms);
  updateInviteBanner(rooms);
  updateHudRoomPill(rooms);

  const box = $('rooms'), key = JSON.stringify(rooms) + $('play').hidden;
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.replaceChildren();
  if (!rooms.length) {
    const empty = document.createElement('div');
    empty.className = 'rooms-empty';
    empty.textContent = 'No open games right now — enter a room name above and click "Host a game".';
    box.append(empty);
    return;
  }
  for (const r of rooms) {
    const row = document.createElement('div');
    row.className = 'room';
    row.setAttribute('role', 'listitem');

    const main = document.createElement('div');
    main.className = 'room-main';

    const title = document.createElement('span');
    title.className = 'room-title';
    title.textContent = (r.password ? '🔒 ' : '') + r.name;

    const maxPlayers = Math.min(8, r.max || 8);
    const countBadge = document.createElement('span');
    countBadge.className = 'room-badge ' + (r.open ? 'open' : 'playing');
    countBadge.textContent = `${r.players.length}/${maxPlayers} · ${r.open ? 'Waiting' : 'In Progress'}`;

    const mainChildren = [title, countBadge];
    if (typeof r.age_s === 'number') {
      const age = document.createElement('span');
      age.className = 'room-age';
      age.textContent = formatRoomAge(r.age_s);
      age.title = 'Room age';
      mainChildren.push(age);
    }
    if (r.players.length) {
      const players = document.createElement('span');
      players.className = 'room-players';
      for (const p of r.players) {
        const chip = document.createElement('span');
        const isHost = Boolean(r.host) && p === r.host;
        chip.className = 'player-chip' + (isHost ? ' host' : '');
        chip.textContent = isHost ? `👑 ${p}` : p;
        players.append(chip);
      }
      mainChildren.push(players);
    }

    main.append(...mainChildren);

    const actions = document.createElement('span');
    actions.style.display = 'flex';
    actions.style.gap = '6px';

    const b = document.createElement('button');
    b.className = r.open ? 'primary' : '';
    b.textContent = 'Join';
    b.style.padding = '5px 12px';
    b.disabled = !r.open || r.players.length >= maxPlayers || $('play').hidden;
    b.onclick = () => {
      let password = '';
      if (r.password) {
        const entered = window.prompt(`Enter password for "${r.name}":`);
        if (entered === null) return;
        password = entered;
      }
      play({ lobby: 'join:' + r.id, room: r.name, password });
    };

    const link = document.createElement('button');
    link.textContent = 'Copy Link';
    link.style.padding = '5px 10px';
    link.title = 'Copy direct invite link to clipboard';
    link.onclick = async () => {
      if (await copyToClipboard(buildInviteUrl(r))) {
        link.textContent = 'Copied!';
        toast('Invite link copied to clipboard');
        setTimeout(() => { link.textContent = 'Copy Link'; }, 1500);
      } else {
        toast('Copy failed — copy the URL manually', 3000);
      }
    };

    actions.append(b, link);
    row.append(main, actions);
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
let activeModule = null;
let relayWsState = 'idle';
let hostedRoom = null;
let activeRoomId = null;
let activeRoomName = null;
let knownPlayers = new Set();
let pingInterval = null;
let buildVersion = '';

async function measurePing() {
  try {
    const t0 = performance.now();
    const r = await fetchRelay('/rooms', 3000);
    if (!r.ok) return;
    const data = await r.json();
    const ms = Math.round(performance.now() - t0);
    const el = $('ping');
    if (relayWsState !== 'reconnecting' && relayWsState !== 'lost') {
      el.textContent = ms + ' ms';
      el.style.color = ms < 100 ? '#74e888' : ms < 250 ? '#f0c25b' : '#f26d6d';
    }
    const rooms = data.rooms || [];
    const totalPlayers = rooms.reduce((s, rm) => s + rm.players.length, 0);
    const onlineCount = typeof data.online === 'number' ? Math.max(data.online, totalPlayers) : totalPlayers;
    const statusParts = [];
    if (onlineCount > 0) statusParts.push(`${onlineCount} online`);
    if (rooms.length > 0) statusParts.push(`${rooms.length} ${rooms.length === 1 ? 'room' : 'rooms'}`);
    if (relayWsState !== 'reconnecting' && relayWsState !== 'lost') {
      setStatus(true, statusParts.length ? `${statusParts.join(' · ')} · ${ms}ms` : `connected · ${ms}ms`, onlineCount, rooms.length);
    }
    checkHostedRoomJoins(rooms);
    updateInviteBanner(rooms);
    updateHudRoomPill(rooms);
  } catch {
    if (relayWsState !== 'reconnecting' && relayWsState !== 'lost') {
      $('ping').textContent = '';
    }
  }
}

function startPingPolling() {
  if (pingInterval) return;
  measurePing();
  pingInterval = setInterval(measurePing, 10000);
}
function stopPingPolling() {
  if (pingInterval) { clearInterval(pingInterval); pingInterval = null; }
  $('ping').textContent = '';
}

// --- Toast notifications ---
function toast(msg, duration) {
  let el = $('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    el.style.cssText = 'position:fixed;bottom:18px;left:50%;transform:translateX(-50%);z-index:40;background:rgba(16,17,22,0.92);border:1px solid rgba(212,173,83,0.4);color:#ece8de;padding:8px 18px;border-radius:999px;font-size:13px;box-shadow:0 8px 24px rgba(0,0,0,0.6);pointer-events:none;transition:opacity 0.25s;opacity:0';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.style.opacity = '1';
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.style.opacity = '0'; }, duration || 2800);
}

// --- Online/offline events during gameplay ---
window.addEventListener('online', () => { if (gameRunning) toast('Connection restored'); });
window.addEventListener('offline', () => { if (gameRunning) toast('Connection lost', 5000); });

// --- Wake lock (keep screen on during gameplay) ---
let wakeLock = null;
async function requestWakeLock() {
  if (!('wakeLock' in navigator)) return;
  try { wakeLock = await navigator.wakeLock.request('screen'); } catch {}
}
function releaseWakeLock() {
  if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; }
}
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && gameRunning) requestWakeLock();
});

// --- Web Lock (signals active session to resist aggressive background throttling) ---
let releaseSessionLockFn = null;
function acquireSessionLock() {
  if (releaseSessionLockFn || !navigator.locks?.request) return;
  const name = 'aoe2-session-' + Math.random().toString(36).slice(2);
  navigator.locks.request(name, () => new Promise((resolve) => {
    releaseSessionLockFn = resolve;
  })).catch(() => {});
}
function releaseSessionLock() {
  if (releaseSessionLockFn) {
    const fn = releaseSessionLockFn;
    releaseSessionLockFn = null;
    try { fn(); } catch {}
  }
}

// --- Tab visibility warning ---
function updateVisibilityWarning() {
  try { activeModule?._web_set_hidden?.(document.hidden ? 1 : 0); } catch {}
  let el = $('tab-warn');
  if (document.hidden && gameRunning) {
    if (!el) {
      el = document.createElement('div');
      el.id = 'tab-warn';
      el.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:30;background:#661100;color:#ffcc66;text-align:center;padding:8px 16px;font-size:14px';
      el.textContent = 'The game is paused — keep this tab visible to avoid stalling multiplayer.';
      document.body.appendChild(el);
    }
  } else if (el) {
    el.remove();
  }
}
document.addEventListener('visibilitychange', updateVisibilityWarning);

// --- Game timer ---
let gameStartTime = 0;
let timerInterval = null;
function startGameTimer() {
  gameStartTime = Date.now();
  if (timerInterval) clearInterval(timerInterval);
  timerInterval = setInterval(() => {
    const s = Math.floor((Date.now() - gameStartTime) / 1000);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    $('game-timer').textContent = h ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}` : `${m}:${String(sec).padStart(2, '0')}`;
  }, 1000);
}
function stopGameTimer() {
  if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
  $('game-timer').textContent = '';
}

function playJoinTone() {
  if (muted || !OrigAudioContext) return;
  try {
    const existing = (window._audioContexts || []).find((c) => c.state === 'running');
    const ctx = existing || new OrigAudioContext();
    const dest = ctx._realDestination || ctx.destination;
    const playTone = () => {
      const t = ctx.currentTime;
      const g = ctx.createGain();
      g.gain.setValueAtTime(0.08, t);
      g.gain.exponentialRampToValueAtTime(0.001, t + 0.3);
      g.connect(dest);
      const o = ctx.createOscillator();
      o.type = 'sine';
      o.frequency.setValueAtTime(523.25, t);
      o.frequency.setValueAtTime(659.25, t + 0.12);
      o.connect(g);
      o.start(t);
      o.stop(t + 0.3);
      if (!existing) setTimeout(() => ctx.close().catch(() => {}), 450);
    };
    if (ctx.state === 'suspended') ctx.resume().then(playTone).catch(() => {});
    else playTone();
  } catch {}
}

function notifyJoin(name) {
  playJoinTone();
  toast(`${name} joined your game!`);
  if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
    try { new Notification('Player joined', { body: name + ' joined your game', icon: 'icon-192.png', tag: 'join-' + name }); } catch {}
  }
}

let pointerLocked = false;
let plFracX = 0, plFracY = 0;
const lockedButtonsDown = new Set();

function exitPointerLockIfHeld() {
  if (document.pointerLockElement === $('canvas')) {
    try { document.exitPointerLock?.(); } catch {}
  }
}

function togglePointerLock() {
  const sdl = $('canvas');
  if (!sdl) return;
  if (document.pointerLockElement === sdl) {
    exitPointerLockIfHeld();
    return;
  }
  if (!gameRunning || !$('setup').hidden || !$('help-overlay').hidden) return;
  try {
    const res = sdl.requestPointerLock?.();
    if (res && typeof res.catch === 'function') {
      res.catch(() => toast('Click the game screen or press Alt+L to lock mouse', 2200));
    }
  } catch {
    toast('Could not lock mouse pointer', 2000);
  }
}

document.addEventListener('pointerlockchange', () => {
  const locked = document.pointerLockElement === $('canvas');
  pointerLocked = locked;
  plFracX = 0;
  plFracY = 0;
  if (!locked && lockedButtonsDown.size) {
    for (const b of lockedButtonsDown) {
      try { activeModule?._web_input_button?.(b, 0); } catch {}
    }
    lockedButtonsDown.clear();
  }
  if ($('lock-btn')) {
    $('lock-btn').textContent = locked ? '🔒 Locked' : '🔓 Lock';
    $('lock-btn').classList.toggle('active', locked);
  }
  if (locked) {
    toast('Mouse locked — move to screen edges to scroll (Esc to release)', 2400);
  }
});

document.addEventListener('pointerlockerror', () => {
  toast('Could not lock mouse pointer', 2000);
});

window.addEventListener('mousemove', (e) => {
  if (!pointerLocked || document.pointerLockElement !== $('canvas') || !activeModule?._web_input_motion) return;
  e.stopImmediatePropagation();
  const mx = e.movementX || 0, my = e.movementY || 0;
  if (Math.abs(mx) > 400 || Math.abs(my) > 400) return;
  const sdl = $('canvas');
  const rect = sdl.getBoundingClientRect();
  const fw = currentFrameW || 800, fh = currentFrameH || 600;
  const picCssW = lastBox && lastBox.cw > 0 ? rect.width * (lastBox.dw / lastBox.cw) : rect.width;
  const picCssH = lastBox && lastBox.ch > 0 ? rect.height * (lastBox.dh / lastBox.ch) : rect.height;
  if (picCssW <= 0 || picCssH <= 0) return;
  plFracX += (mx * fw) / picCssW;
  plFracY += (my * fh) / picCssH;
  const dx = Math.trunc(plFracX), dy = Math.trunc(plFracY);
  plFracX -= dx;
  plFracY -= dy;
  if (dx || dy) activeModule._web_input_motion(dx, dy);
}, true);

for (const evtName of ['mousedown', 'mouseup']) {
  window.addEventListener(evtName, (e) => {
    if (!pointerLocked || document.pointerLockElement !== $('canvas') || !activeModule?._web_input_button) return;
    e.stopImmediatePropagation();
    if (e.button !== 0) e.preventDefault();
    const down = evtName === 'mousedown';
    if (down) lockedButtonsDown.add(e.button);
    else lockedButtonsDown.delete(e.button);
    activeModule._web_input_button(e.button, down ? 1 : 0);
  }, true);
}

function openOnline() {
  if (!$('setup').hidden) return;
  exitPointerLockIfHeld();
  $('play').textContent = gameRunning ? 'Resume Game (Esc)' : 'Play';
  $('setup').hidden = false;
  refresh();
  startRoomPolling();
}

function closeOnline() {
  if ($('setup').hidden) return;
  $('setup').hidden = true;
  stopRoomPolling();
  if (gameRunning) {
    $('canvas').hidden = false;
    syncScreenRect();
    $('canvas').focus();
  }
}

function returnToLobby(msg) {
  exitPointerLockIfHeld();
  try { activeModule?.closeRelay?.(); } catch {}
  gameRunning = false;
  activeModule = null;
  relayWsState = 'idle';
  hostedRoom = null;
  activeRoomId = null;
  activeRoomName = null;
  currentInviteUrl = '';
  stopPingPolling();
  releaseWakeLock();
  releaseSessionLock();
  stopGameTimer();
  updateVisibilityWarning();
  hudPinned = false;
  clearTimeout(hudHideTimer);
  document.body.classList.remove('hud-hidden');
  $('loading').hidden = true;
  $('game-ui').hidden = true;
  if ($('hud-invite-btn')) $('hud-invite-btn').hidden = true;
  if ($('hud-room-pill')) { $('hud-room-pill').hidden = true; $('hud-room-pill').textContent = ''; }
  $('canvas').hidden = true;
  $('screen').hidden = true;
  if ($('crt-scanlines')) $('crt-scanlines').hidden = true;
  if ($('res-badge')) $('res-badge').textContent = '';
  if ($('apply-res')) $('apply-res').hidden = true;
  currentFrameW = 0;
  currentFrameH = 0;
  document.documentElement.style.setProperty('--game-ar', '1.3333333');
  $('fps').textContent = '';
  $('saves-list').hidden = true;
  $('play').textContent = 'Play';
  $('setup').hidden = false;
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  refresh();
  startRoomPolling();
  if (msg) log(msg);
}

async function play(launch) {
  if (gameRunning) {
    if (!launch || !launch.lobby) {
      closeOnline();
      return;
    }
    try {
      if (launch.password) sessionStorage.setItem('aoe2.pendingPass', launch.password);
      else sessionStorage.removeItem('aoe2.pendingPass');
    } catch {}
    const u = new URL(location.href);
    if (launch.lobby === 'host') {
      u.search = `?host=1&room=${encodeURIComponent(launch.room || '')}`;
    } else if (launch.lobby.startsWith('join:')) {
      u.search = `?join=${encodeURIComponent(launch.lobby.slice(5))}&room=${encodeURIComponent(launch.room || '')}`;
    } else {
      u.search = '';
    }
    location.href = u.toString();
    return;
  }
  launchedRes = resolveRes(selectedRes);
  if ($('apply-res')) $('apply-res').hidden = true;
  if (launch && launch.lobby) {
    if (launch.lobby === 'host') {
      activeRoomId = null;
      activeRoomName = launch.room || null;
    } else if (launch.lobby.startsWith('join:')) {
      activeRoomId = launch.lobby.slice(5) || null;
      activeRoomName = launch.room || null;
    }
  } else {
    activeRoomId = null;
    activeRoomName = null;
  }
  updateHudRoomPill([]);
  if (!crossOriginIsolated) { returnToLobby('This page must be served cross-origin isolated (tools/web/serve.py does).'); return; }
  const exe = await copiedExe().catch(() => null);
  const problem = exe ? await exeProblem(exe) : 'The browser cache has no age2_x1\\age2_x1.exe.';
  if (problem) { returnToLobby(problem); return; }
  await videoStandIns(await root()).catch(() => {});
  gameRunning = true;
  stopRoomPolling();
  requestWakeLock();
  acquireSessionLock();
  startGameTimer();
  startPingPolling();
  $('setup').hidden = true;
  $('loading').hidden = false;
  const loadBar = $('load-bar');
  const loadText = $('load-text');
  const loadSub = $('load-sub');
  setLoadPhase('Step 2/2 · Game Engine');
  loadBar.hidden = false;
  loadBar.value = 0;
  loadText.textContent = 'Starting the game…';
  loadSub.textContent = '';
  $('retry-btn').hidden = true;
  const canvas = $('canvas');
  canvas.hidden = false;
  canvas.focus();
  const verSuffix = buildVersion ? `?v=${encodeURIComponent(buildVersion)}` : '';
  const wasmUrl = 'aoe2.wasm' + verSuffix;
  const jsUrl = 'aoe2.js' + verSuffix;
  const wasmSize = serverWasmSize || (43 * 1e6);
  const debugTrace = new URLSearchParams(location.search).has('debug');
  const config = {
    canvas,
    arguments: [],
    instantiateWasm(imports, callback) {
      loadText.textContent = 'Downloading WebAssembly engine…';
      loadSub.textContent = '';
      fetch(wasmUrl)
        .then(async (r) => {
          if (!r.ok) throw new Error('WASM fetch failed: ' + r.status);
          const rawLen = parseInt(r.headers.get('x-raw-length') || '0', 10);
          const contentLen = parseInt(r.headers.get('content-length') || '0', 10);
          const isEncoded = Boolean(r.headers.get('content-encoding'));
          const totalBytes = rawLen || (!isEncoded && contentLen ? contentLen : 0) || wasmSize;
          const totalMB = (totalBytes / 1e6).toFixed(1);
          let received = 0;

          if (WebAssembly.instantiateStreaming && r.body) {
            const reader = r.body.getReader();
            const progressStream = new ReadableStream({
              async pull(controller) {
                const { done, value } = await reader.read();
                if (done) {
                  loadBar.removeAttribute('value');
                  loadText.textContent = 'Compiling WebAssembly…';
                  loadSub.textContent = 'Preparing game runtime';
                  controller.close();
                  return;
                }
                received += value.length;
                loadBar.value = Math.min(1, received / Math.max(totalBytes, received));
                loadSub.textContent = `${(received / 1e6).toFixed(1)} / ${totalMB} MB`;
                controller.enqueue(value);
              },
              cancel(reason) { return reader.cancel(reason); },
            });
            const streamedResp = new Response(progressStream, {
              status: r.status,
              statusText: r.statusText,
              headers: { 'Content-Type': 'application/wasm' },
            });
            try {
              return await WebAssembly.instantiateStreaming(streamedResp, imports);
            } catch (streamErr) {
              console.warn('[wasm] instantiateStreaming failed, falling back:', streamErr);
              const fallback = await fetch(wasmUrl);
              if (!fallback.ok) throw new Error('WASM fetch failed: ' + fallback.status);
              const buf = await fallback.arrayBuffer();
              loadBar.removeAttribute('value');
              loadText.textContent = 'Compiling WebAssembly…';
              loadSub.textContent = '';
              return WebAssembly.instantiate(buf, imports);
            }
          }

          const buf = await r.arrayBuffer();
          loadBar.removeAttribute('value');
          loadText.textContent = 'Compiling WebAssembly…';
          loadSub.textContent = '';
          return WebAssembly.instantiate(buf, imports);
        })
        .then((result) => {
          loadBar.removeAttribute('value');
          loadText.textContent = 'Launching Age of Empires II…';
          loadSub.textContent = 'Mounting game files & starting engine…';
          callback(result.instance, result.module);
        })
        .catch((e) => { returnToLobby('Failed to load the game engine: ' + e.message); });
      return {};
    },
    preRun: [(m) => {
      activeModule = m;
      m.ENV.AOE2_ACCEPT_EULA = '1';
      m.ENV.AOE2_RESOLUTION = launchedRes;
      if (debugTrace) m.ENV.AOE2_TRACE_VIDEO = '1';
      if (launch && launch.lobby) {
        m.ENV.AOE2_ARGS = 'LOBBY';
        m.ENV.AOE2_LOBBY = launch.lobby;
        m.ENV.AOE2_ROOM = launch.room || '';
        m.ENV.AOE2_PLAYER = playerName();
        if (launch.password) m.ENV.AOE2_PASSWORD = launch.password;
      }
    }],
    setStatus(text) {
      if (!text) return;
      const m = text.match(/([^(]+)\((\d+(\.\d+)?)\/(\d+)\)/);
      if (m) {
        loadBar.value = 1 - parseInt(m[2], 10) / parseInt(m[4], 10);
        loadText.textContent = m[1].trim() || 'Loading…';
      } else {
        loadBar.removeAttribute('value');
        loadText.textContent = text;
      }
    },
    monitorRunDependencies(left) {
      if (left === 0) {
        loadBar.removeAttribute('value');
        loadText.textContent = 'Launching Age of Empires II…';
        loadSub.textContent = 'Initializing DirectDraw & audio…';
      }
    },
    onExit(code) {
      returnToLobby('The game exited' + (code ? ` (code ${code}).` : '.'));
    },
    onAbort(what) {
      returnToLobby('The game stopped: ' + what);
    },
    onRelayState(state, attempt) {
      const prev = relayWsState;
      relayWsState = state;
      const dot = $('hud-online-dot');
      const pingEl = $('ping');
      if (state === 'connected') {
        if (dot) dot.className = 'ok';
        if (prev === 'reconnecting' || prev === 'lost') {
          toast('Reconnected to multiplayer relay');
          log('Reconnected to multiplayer relay.');
        }
        measurePing();
      } else if (state === 'reconnecting') {
        if (dot) dot.className = 'err';
        if (pingEl) {
          pingEl.textContent = `Reconnecting (${attempt}/5)…`;
          pingEl.style.color = '#f0c25b';
        }
        toast(`Relay disconnected — reconnecting (${attempt}/5)…`, 2500);
      } else if (state === 'lost') {
        if (dot) dot.className = 'err';
        if (pingEl) {
          pingEl.textContent = 'Disconnected';
          pingEl.style.color = '#f26d6d';
        }
        log('Multiplayer relay connection lost.');
        toast('Multiplayer connection lost — check your network', 6000);
      }
    },
    relayUrl: resolveRelayWsUrl(),
    print: (t) => console.log(t),
    printErr: (t) => console.warn(t),
  };
  Object.assign(config, presenter(config));
  const startWasm = () => AoE2(config).catch(
    (e) => returnToLobby('The game stopped: ' + (e?.message || e))
  );
  if (typeof AoE2 === 'function') {
    startWasm();
  } else {
    const s = document.createElement('script');
    s.src = jsUrl;
    s.onload = startWasm;
    s.onerror = () => returnToLobby('Failed to load the game engine. Try refreshing the page.');
    document.body.appendChild(s);
  }
}

async function syncBuildVersion() {
  try {
    const [verResp, meResp] = await Promise.all([
      fetch('api/version', { cache: 'no-store' }),
      fetch('api/me', { cache: 'no-store' }).catch(() => null),
    ]);
    if (meResp && meResp.ok) {
      try {
        const me = await meResp.json();
        serverConfig = {
          install: me.install !== false,
          relay_same_origin: typeof me.relay_same_origin === 'boolean' ? me.relay_same_origin : null,
          relay_ws_url: typeof me.relay_ws_url === 'string' ? me.relay_ws_url : '',
        };
      } catch {}
    }
    if (!verResp.ok) return;
    const j = await verResp.json();
    if (typeof j.wasm_size === 'number' && j.wasm_size > 0) {
      serverWasmSize = j.wasm_size;
    }
    if (!j.version) return;
    buildVersion = j.version;
    $('version').textContent = 'v' + buildVersion;
    const prev = localStorage.getItem('aoe2.version');
    if (prev !== buildVersion) {
      if ('caches' in window) {
        const keys = await caches.keys();
        await Promise.all(keys.map((k) => caches.delete(k)));
      }
      if ('serviceWorker' in navigator) {
        const reg = await navigator.serviceWorker.getRegistration();
        reg?.active?.postMessage('update-cache');
      }
      localStorage.setItem('aoe2.version', buildVersion);
    }
  } catch {}
}

async function init() {
  checkCompat();
  if (!hasRuntimeAPIs()) {
    $('load-text').textContent = 'Browser not supported';
    $('load-bar').hidden = true;
    return;
  }

  await syncBuildVersion();

  const params = new URLSearchParams(location.search);
  const joinId = params.get('join');
  const hostParam = params.get('host');
  const roomParam = params.get('room') || '';
  let pendingPass = '';
  try {
    pendingPass = sessionStorage.getItem('aoe2.pendingPass') || '';
    sessionStorage.removeItem('aoe2.pendingPass');
  } catch {}
  let launch;
  let openLobbyOnBoot = false;
  if (joinId) {
    history.replaceState(null, '', location.pathname);
    let target = null;
    let roomsKnown = false;
    try {
      const r = await fetchRelay('/rooms', 4000);
      if (r.ok) {
        const data = await r.json();
        roomsKnown = true;
        target = (data.rooms || []).find((rm) => String(rm.id) === String(joinId)) || null;
      }
    } catch {}
    if (roomsKnown && !target) {
      toast('Invite room no longer exists — opened multiplayer lobby.', 4000);
      log('Invite room is no longer available.');
      openLobbyOnBoot = true;
    } else if (target && !target.open) {
      toast('That game has already started — opened multiplayer lobby.', 4000);
      log(`Room "${target.name}" has already started.`);
      openLobbyOnBoot = true;
    } else {
      const needsPass = Boolean((target && target.password) || params.get('locked') === '1');
      if (needsPass && !pendingPass) {
        const label = (target && target.name) || roomParam || 'this room';
        const entered = window.prompt(`Enter password for "${label}":`);
        if (entered === null) {
          openLobbyOnBoot = true;
        } else {
          pendingPass = entered;
        }
      }
      if (!openLobbyOnBoot) {
        launch = { lobby: 'join:' + joinId, room: (target && target.name) || roomParam, password: pendingPass };
        log('Joining game from invite link…');
      }
    }
  } else if (hostParam) {
    history.replaceState(null, '', location.pathname);
    const room = (roomParam || `${playerName()}'s game`).slice(0, 30);
    launch = { lobby: 'host', room, password: pendingPass };
    hostedRoom = room;
    knownPlayers = new Set([playerName()]);
    if (typeof Notification !== 'undefined' && Notification.permission === 'default') Notification.requestPermission();
  }

  const exe = await copiedExe().catch(() => null);
  const complete = (await isComplete()) && Boolean(exe) && !(await exeProblem(exe));

  if (!complete) {
    if (serverConfig.install === false) {
      setLoadPhase('Setup Required');
      $('load-text').textContent = 'Server has no preloaded game files configured';
      $('load-sub').textContent = 'Configure AOE2_INSTALL_DIR to serve the Age of Empires II install files.';
      $('load-bar').hidden = true;
      $('retry-btn').hidden = false;
      log('Server install disabled and no cached files in OPFS.');
      return;
    }
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
  link.rel = 'preload';
  link.href = 'aoe2.wasm' + (buildVersion ? `?v=${encodeURIComponent(buildVersion)}` : '');
  link.as = 'fetch';
  link.crossOrigin = '';
  document.head.appendChild(link);
  await refresh();
  if (openLobbyOnBoot) {
    $('loading').hidden = true;
    openOnline();
    return;
  }
  await play(launch);
}

// --- UI Event Listeners ---
$('online-btn').onclick = () => { if ($('setup').hidden) openOnline(); else closeOnline(); };
if ($('hud-room-pill')) $('hud-room-pill').onclick = () => { if ($('setup').hidden) openOnline(); else closeOnline(); };
$('play').onclick = () => { if (gameRunning) closeOnline(); else play(); };
$('host').onclick = () => {
  const room = ($('roomname').value.trim() || `${playerName()}'s game`).slice(0, 30);
  const password = $('roompass') ? $('roompass').value.trim() : '';
  if ($('roompass')) $('roompass').value = '';
  hostedRoom = room;
  knownPlayers = new Set([playerName()]);
  if (typeof Notification !== 'undefined' && Notification.permission === 'default') Notification.requestPermission();
  play({ lobby: 'host', room, password });
};
if ($('res-select')) {
  $('res-select').onchange = () => {
    const val = $('res-select').value;
    selectedRes = val === 'auto' || VALID_RES.includes(val) ? val : 'auto';
    try { localStorage.setItem('aoe2.res2', selectedRes); } catch {}
    if ($('apply-res')) $('apply-res').hidden = !gameRunning || (resolveRes(selectedRes) === launchedRes);
  };
}
if ($('apply-res')) $('apply-res').onclick = () => location.reload();
if ($('scale-select')) $('scale-select').onchange = () => applyScaleMode($('scale-select').value, true);
if ($('scale-btn')) {
  $('scale-btn').onclick = () => {
    const next = SCALE_MODES[(SCALE_MODES.indexOf(scaleMode) + 1) % SCALE_MODES.length];
    applyScaleMode(next, true);
  };
}
if ($('volume-slider')) {
  $('volume-slider').oninput = () => setVolume(Number($('volume-slider').value) / 100);
}
if ($('hud-collapse-btn')) $('hud-collapse-btn').onclick = hideHud;
$('refresh-rooms').onclick = () => { listRooms(); };
$('copy-invite').onclick = async () => {
  const u = $('invite-url').textContent;
  if (!u) return;
  if (await copyToClipboard(u)) {
    $('copy-invite').textContent = 'Copied!';
    toast('Invite link copied to clipboard');
    setTimeout(() => { $('copy-invite').textContent = 'Copy Invite Link'; }, 1500);
  } else {
    toast('Copy failed — copy the URL manually', 3000);
  }
};
$('retry-btn').onclick = () => location.reload();
$('chat-send').onclick = sendChat;
$('saves-btn').onclick = () => showSaves();
$('import-rec').onclick = importRecording;
if ($('hud-saves-btn')) $('hud-saves-btn').onclick = () => { openOnline(); showSaves(true); };
if ($('hud-invite-btn')) {
  $('hud-invite-btn').onclick = async () => {
    if (!currentInviteUrl) return;
    if (await copyToClipboard(currentInviteUrl)) {
      toast('Invite link copied to clipboard');
    } else {
      toast('Copy failed — open Online lobby to copy link', 3000);
    }
  };
}
if ($('lock-btn')) $('lock-btn').onclick = togglePointerLock;
const setupPanel = $('setup-panel');
if (setupPanel) {
  setupPanel.addEventListener('dragover', (e) => {
    if ($('setup').hidden || $('import-rec').hidden) return;
    if (!e.dataTransfer?.types?.includes('Files')) return;
    e.preventDefault();
    setupPanel.classList.add('drop-active');
  });
  setupPanel.addEventListener('dragleave', (e) => {
    if (!setupPanel.contains(e.relatedTarget)) setupPanel.classList.remove('drop-active');
  });
  setupPanel.addEventListener('drop', (e) => {
    setupPanel.classList.remove('drop-active');
    if ($('setup').hidden || $('import-rec').hidden) return;
    if (!e.dataTransfer?.files?.length) return;
    e.preventDefault();
    importFiles(e.dataTransfer.files);
  });
}
$('mute-btn').onclick = toggleMute;
$('fullscreen-btn').onclick = toggleFullscreen;
$('hud-help-btn').onclick = () => { exitPointerLockIfHeld(); $('help-overlay').hidden = false; $('help-close').focus(); };
$('canvas').addEventListener('contextmenu', (e) => e.preventDefault());
$('setup').onclick = (e) => { if (gameRunning && e.target === $('setup')) closeOnline(); };
$('help-link').onclick = (e) => { e.preventDefault(); $('help-overlay').hidden = false; $('help-close').focus(); };
$('clear-cache').onclick = async (e) => {
  e.preventDefault();
  if (!confirm('Delete all cached game files? The game will re-download on next visit.')) return;
  try {
    const r = await navigator.storage.getDirectory();
    for await (const [name] of r.entries()) {
      await r.removeEntry(name, { recursive: true });
    }
    if ('serviceWorker' in navigator) {
      const keys = await caches.keys();
      await Promise.all(keys.map((k) => caches.delete(k)));
    }
    log('Cache cleared. Reloading…');
    setTimeout(() => location.reload(), 500);
  } catch (err) { log('Failed to clear cache: ' + err.message); }
};
$('help-close').onclick = () => {
  $('help-overlay').hidden = true;
  if ($('setup').hidden && gameRunning) $('canvas').focus(); else $('help-link').focus();
};
$('help-overlay').onclick = (e) => {
  if (e.target === $('help-overlay')) {
    $('help-overlay').hidden = true;
    if ($('setup').hidden && gameRunning) $('canvas').focus(); else $('help-link').focus();
  }
};

// Capture-phase keyboard handler registered BEFORE aoe2.js loads so SDL2's
// window capture listener never steals keystrokes when #setup or #help-overlay is open.
window.addEventListener('keydown', (e) => {
  if (!$('help-overlay').hidden) {
    if (e.key === 'Escape') {
      e.preventDefault();
      $('help-overlay').hidden = true;
      if ($('setup').hidden && gameRunning) $('canvas').focus();
    }
    e.stopImmediatePropagation();
    return;
  }
  if (!$('setup').hidden) {
    if (e.key === 'Escape' && gameRunning) {
      e.preventDefault();
      closeOnline();
      e.stopImmediatePropagation();
      return;
    }
    if (e.key === 'Enter' && e.target === $('chat-input')) {
      e.preventDefault();
      sendChat();
      e.stopImmediatePropagation();
      return;
    }
    e.stopImmediatePropagation();
    return;
  }
  // In-game shortcuts (when overlays are hidden)
  const tag = e.target?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
  if (!e.altKey && !e.ctrlKey && !e.metaKey && (e.key === 'F1' || e.key === 'F2' || e.key === 'F3' || e.key === 'F4' || e.key === 'F10')) {
    e.preventDefault();
    return;
  }
  if (e.key === 'F11') {
    e.preventDefault();
    toggleFullscreen();
    e.stopImmediatePropagation();
    return;
  }
  if (e.altKey && (e.key === 'm' || e.key === 'M') && !e.ctrlKey && !e.metaKey) {
    e.preventDefault();
    toggleMute();
    e.stopImmediatePropagation();
    return;
  }
  if (e.altKey && (e.key === 'h' || e.key === 'H') && !e.ctrlKey && !e.metaKey && gameRunning) {
    e.preventDefault();
    toggleHud();
    e.stopImmediatePropagation();
    return;
  }
  if (e.altKey && (e.key === 'l' || e.key === 'L') && !e.ctrlKey && !e.metaKey) {
    e.preventDefault();
    togglePointerLock();
    e.stopImmediatePropagation();
  }
}, true);

for (const evt of ['keyup', 'keypress']) {
  window.addEventListener(evt, (e) => {
    if (!$('help-overlay').hidden || !$('setup').hidden) {
      e.stopImmediatePropagation();
    }
  }, true);
}

try { $('player').value = localStorage.getItem('aoe2.player') || ''; } catch {}
$('player').oninput = () => { try { localStorage.setItem('aoe2.player', $('player').value); } catch {} };
try { $('roomname').value = localStorage.getItem('aoe2.room') || ''; } catch {}
$('roomname').oninput = () => { try { localStorage.setItem('aoe2.room', $('roomname').value); } catch {} };

window.addEventListener('pagehide', () => {
  try { activeModule?.closeRelay?.(); } catch {}
  releaseSessionLock();
});

if ('serviceWorker' in navigator) {
  const hadController = Boolean(navigator.serviceWorker.controller);
  navigator.serviceWorker.register('sw.js').then((reg) => {
    reg.addEventListener('updatefound', () => {
      const nw = reg.installing;
      if (!nw) return;
      nw.addEventListener('statechange', () => {
        if (nw.state === 'activated' && hadController) {
          log('Update available — refresh the page for the latest version.');
        }
      });
    });
  }).catch(() => {});
}
navigator.storage?.persist?.();
init();
