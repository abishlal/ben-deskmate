// Desktop shell for the pet. It holds no logic of its own: it shows the page that the Docker
// backend serves at http://127.0.0.1:<port>/pet/ in a transparent, click-through, always-on-top
// strip just above the taskbar, and adds a tray menu, a global hotkey and desktop notifications.
const { app, BrowserWindow, Tray, Menu, Notification, clipboard, globalShortcut, ipcMain, nativeImage, powerMonitor, screen, session, shell } = require('electron');
const { spawn, execFile } = require('child_process');
const fs = require('fs');
const path = require('path');

const STRIP_HEIGHT = 300;
const SELFTEST = process.argv.includes('--selftest');
// A transparent always-on-top window drawn by the GPU often stops painting after the laptop sleeps and wakes a
// number of times (the window is there, but empty). The pet is a few small shapes, so draw it in software.
app.disableHardwareAcceleration();

// Reads one plain setting (never the token) from the project's .env.
function readSetting(name, pattern, fallback) {
  if (process.env[name]) return process.env[name];
  try {
    const env = fs.readFileSync(path.join(__dirname, '..', '.env'), 'utf8');
    const m = env.match(new RegExp(`^${name}=\\s*(${pattern})\\s*$`, 'm'));
    if (m) return m[1];
  } catch {}
  return fallback;
}
const ORIGIN = `http://127.0.0.1:${readSetting('PET_PORT', '\\d+', '47821')}`;
const PET_NAME = readSetting('PET_NAME', '[\\w -]{1,30}', 'Jarvis');
// What you say to wake it; the wake-word model decides this, so it can differ from the name.
const WAKE_PHRASE = readSetting('PET_WAKE_PHRASE', '[\\w ,-]{1,40}', 'Hey Jarvis');
// Global shortcuts. Each can be changed in .env (e.g. PET_HOTKEY_TALK=Control+Alt+B). A shortcut another app
// already owns (Ctrl+Alt+Space belongs to the Claude app's quick entry) cannot be taken; the tray menu says so.
const KEY = '[\\w+]{3,40}';
const HOTKEY = readSetting('PET_HOTKEY_TALK', KEY, 'Control+Alt+B');
// Stops whatever the pet is doing (talking, thinking, listening, dictating) and hides it at once.
const DISMISS_HOTKEY = readSetting('PET_HOTKEY_DISMISS', KEY, 'Control+Alt+X');
const PAUSE_HOTKEY = readSetting('PET_HOTKEY_PAUSE', KEY, 'Control+Alt+P');
const DICTATE_HOTKEY = readSetting('PET_HOTKEY_DICTATE', KEY, 'Control+Alt+D');
const CAPTURE_HOTKEY = readSetting('PET_HOTKEY_SUMMARISE', KEY, 'Control+Alt+S');
const REPHRASE_HOTKEY = readSetting('PET_HOTKEY_REPHRASE', KEY, 'Control+Alt+R');
// Silent mode on or off: messages in the speech bubble instead of spoken (also on the settings page, or by voice).
const SILENT_HOTKEY = readSetting('PET_HOTKEY_SILENT', KEY, 'Control+Alt+M');
const hotkeyOk = {};
const keyLabel = (k) => hotkeyOk[k] === false ? 'shortcut taken by another app'
  : k.replace('Control', 'Ctrl').replace('CommandOrControl', 'Ctrl').replaceAll('+', ' + ');

let win = null, tray = null;

// ---- a small log of starts, stops and crashes, so "the pet is gone" can be explained afterwards ----
// %APPDATA%\desktop-pet-shell\shell.log, kept under 1 MB. It never contains what you say or your data.
let quitReason = 'unknown (Windows shutdown or sign-out, or the process was ended)';
function logLine(...parts) {
  try {
    const file = path.join(app.getPath('userData'), 'shell.log');
    if (fs.existsSync(file) && fs.statSync(file).size > 1_000_000) fs.renameSync(file, file + '.old');
    fs.appendFileSync(file, `${new Date().toISOString()} ${parts.map(String).join(' ')}\n`);
  } catch {}
}
process.on('uncaughtException', (e) => logLine('uncaught exception:', e && e.stack || e));
process.on('unhandledRejection', (e) => logLine('unhandled rejection:', e && e.stack || e));
let state = { handsfree: true, muted: false, stay: false, paused: false, autoPause: true, inCall: false, silent: false };
// A pause is meant for a meeting or a call, so it lifts itself when you come back from a break
// (twenty minutes without keyboard or mouse, or the PC asleep that long), and after three hours at most.
const AWAY_RESUMES_S = 20 * 60, PAUSE_MAX_MS = 3 * 3600 * 1000;
let pausedAt = 0, awayDuringPause = false, suspendedAt = 0;

// ---- desktop bridge: Microsoft To Do (classic Outlook task folders) and OneNote only ----
// The server may ask for these operations and nothing else. Each one runs bridge.ps1 once;
// the operation name is checked here and again inside the script.
const BRIDGE_OPS = new Set(['status', 'tasks_list', 'tasks_done', 'task_add', 'task_update', 'task_delete',
  'notes_search', 'note_read', 'note_create', 'note_append', 'note_delete', 'note_sections', 'note_section_ensure',
  'calendar_list', 'calendar_add']);
let bridgeQueue = Promise.resolve();
// Which OneNote notebook new pages and sections go into: its name, or part of its OneDrive path when two
// notebooks share a name (e.g. "Notebooks/My Notebook"). Empty: OneNote's own Quick Notes.
const BRIDGE_ENV = {
  PET_ONENOTE_NOTEBOOK: readSetting('PET_ONENOTE_NOTEBOOK', '[^\\r\\n]{1,200}?', '').trim(),
  PET_ONENOTE_SECTION: readSetting('PET_ONENOTE_SECTION', '[^\\r\\n\\\\/:*?"<>|]{1,100}?', 'Quick Notes').trim(),
};

// One bridge process is kept running and answers one JSON line per request, so each call skips starting
// PowerShell and loading the Office interop again (about a second, often more). It is restarted after a
// crash or a timeout, and now and then to keep its memory small.
const BRIDGE_MAX_CALLS = 200;
let bridge = null;   // { child, buf, err, waiting, calls }

function bridgeProc() {
  if (bridge && bridge.child.exitCode === null && !bridge.child.killed && bridge.calls < BRIDGE_MAX_CALLS) return bridge;
  if (bridge) stopBridge();
  const child = spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-File', path.join(__dirname, 'bridge.ps1'), '-Op', 'serve'],
    { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, ...BRIDGE_ENV } });
  const b = { child, buf: '', err: '', waiting: null, calls: 0 };
  const fail = (error) => { const w = b.waiting; b.waiting = null; if (w) w({ ok: false, error }); };
  child.stdout.setEncoding('utf8').on('data', (d) => {
    b.buf += d;
    let i;
    while ((i = b.buf.indexOf('\n')) >= 0) {
      const line = b.buf.slice(0, i).replace(/^﻿/, '').trim();
      b.buf = b.buf.slice(i + 1);
      if (!line) continue;
      const w = b.waiting; b.waiting = null;
      if (!w) continue;
      try { w(JSON.parse(line)); } catch { w({ ok: false, error: line.slice(0, 300) }); }
    }
  });
  child.stderr.setEncoding('utf8').on('data', (d) => { b.err = (b.err + d).slice(-2000); });
  child.on('error', (e) => fail(String(e.message || e)));
  child.on('close', () => { fail((b.err || 'The desktop bridge stopped.').trim().slice(0, 300)); if (bridge === b) bridge = null; });
  child.stdin.on('error', () => {});
  bridge = b;
  return b;
}

function stopBridge() {
  const b = bridge; bridge = null;
  if (b) { try { b.child.stdin.end(); } catch {} setTimeout(() => { try { b.child.kill(); } catch {} }, 2000); }
}

function runBridge(op, args) {
  return new Promise((resolve) => {
    const b = bridgeProc();
    b.calls++; b.err = '';
    const timer = setTimeout(() => {
      b.waiting = null;
      if (bridge === b) bridge = null;
      try { b.child.kill(); } catch {}
      resolve({ ok: false, error: 'The desktop app took too long to answer.' });
    }, 45000);
    b.waiting = (res) => { clearTimeout(timer); resolve(res); };
    b.child.stdin.write(JSON.stringify({ op, args: args || {} }) + '\n');
  });
}

function hostCall(op, args) {
  if (!BRIDGE_OPS.has(op)) return Promise.resolve({ ok: false, error: `Operation ${op} is not allowed.` });
  const run = bridgeQueue.then(() => runBridge(op, args));   // one at a time
  bridgeQueue = run.catch(() => {});
  return run;
}

// ---- dictation: type recognised speech into whichever app has the keyboard focus ----
// The text is put on the clipboard and pasted with Ctrl+V; the previous clipboard text or image is put back.
let typeQueue = Promise.resolve();

function typeText(text) {
  text = String(text || '').slice(0, 5000);
  if (!text.trim()) return;
  typeQueue = typeQueue.then(() => new Promise((resolve) => {
    const savedImage = clipboard.readImage(), savedText = clipboard.readText();
    clipboard.writeText(text);
    execFile('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
      "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait('^v')"],
    { windowsHide: true, timeout: 10000 }, () => {
      setTimeout(() => {
        if (!savedImage.isEmpty()) clipboard.writeImage(savedImage); else clipboard.writeText(savedText);
        resolve();
      }, 500);
    });
  }));
}

// ---- rephrase: Ctrl+Alt+R rewrites the selected text in your tone and pastes it over the selection ----
// keys.ps1 runs the whole time (starting PowerShell for each use would add a second); it presses Ctrl+C and
// Ctrl+V and reports which window is active. The clipboard is put back as it was after both.
let keysProc = null, keysBuf = '';
const keysWaiting = [];
let keysQueue = Promise.resolve();

function startKeys() {
  if (keysProc && keysProc.exitCode === null && !keysProc.killed) return;
  keysBuf = '';
  keysProc = spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', path.join(__dirname, 'keys.ps1')],
    { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
  keysWaiting.length = 0;
  keysWaiting.push(() => {});   // its "ready" line
  keysProc.stdout.setEncoding('utf8').on('data', (d) => {
    keysBuf += d;
    let nl;
    while ((nl = keysBuf.indexOf('\n')) >= 0) {
      const line = keysBuf.slice(0, nl).replace(/^﻿/, '').trim(); keysBuf = keysBuf.slice(nl + 1);
      if (!line) continue;
      let res; try { res = JSON.parse(line); } catch { res = { ok: false, error: line.slice(0, 200) }; }
      const waiter = keysWaiting.shift();
      if (waiter) waiter(res);
    }
  });
  keysProc.stderr.setEncoding('utf8').on('data', (d) => logLine('keys helper:', d.trim().slice(0, 300)));
  keysProc.on('exit', (code) => {
    logLine('keys helper ended', code);
    while (keysWaiting.length) keysWaiting.shift()({ ok: false, error: 'helper ended' });
    keysProc = null;
  });
  keysProc.on('error', (e) => logLine('keys helper failed to start:', e.message));
}

function keysCall(cmd) {
  const run = keysQueue.then(() => new Promise((resolve) => {
    startKeys();
    let done = false;
    const finish = (res) => { if (!done) { done = true; clearTimeout(timer); resolve(res); } };
    // A late answer still takes its place in the queue, so later answers stay matched to their commands.
    const timer = setTimeout(() => finish({ ok: false, error: 'timeout' }), 8000);
    keysWaiting.push(finish);
    try { keysProc.stdin.write(cmd + '\n'); } catch (e) { finish({ ok: false, error: String(e.message || e) }); }
  }));
  keysQueue = run.catch(() => {});
  return run;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function saveClipboard() {
  const image = clipboard.readImage(), text = clipboard.readText();
  return () => { if (!image.isEmpty()) clipboard.writeImage(image); else clipboard.writeText(text); };
}
// Runs fn with the clipboard to itself: dictation and rephrasing never use it at the same time.
function withClipboard(fn) {
  const run = typeQueue.then(fn);
  typeQueue = run.catch(() => {});
  return run;
}

let rephrasing = null;   // { id, hwnd, at } while a rewrite is on its way

async function rephraseSelection() {
  if (rephrasing && Date.now() - rephrasing.at < 60000) { send('rephrase-status', 'busy'); return; }
  rephrasing = { id: String(Date.now()), hwnd: null, at: Date.now() };
  const mine = rephrasing;
  const grabbed = await withClipboard(async () => {
    const restore = saveClipboard();
    clipboard.clear();
    const res = await keysCall('grab');
    let text = '';
    if (res.ok) for (let i = 0; i < 20 && !text; i++) { await sleep(40); text = clipboard.readText(); }
    restore();
    return { ...res, text };
  }).catch((e) => ({ ok: false, error: String(e) }));
  if (rephrasing !== mine) return;
  // Too long: refuse rather than paste a rewrite of only part of it over the whole selection.
  const problem = !grabbed.ok ? (grabbed.error === 'keys' ? 'keys' : 'failed')
    : !grabbed.text.trim() ? 'empty' : grabbed.text.length > 5000 ? 'long' : '';
  if (problem) {
    rephrasing = null;
    if (problem === 'failed') logLine('rephrase: could not copy the selection:', grabbed.error);
    send('rephrase-status', problem);
    return;
  }
  mine.hwnd = grabbed.hwnd;
  send('rephrase', { id: mine.id, text: grabbed.text });
}

async function pasteRephrase(id, text) {
  const job = rephrasing;
  if (!job || job.id !== id) return;
  rephrasing = null;
  const res = await withClipboard(async () => {
    const restore = saveClipboard();
    clipboard.writeText(text);
    const r = await keysCall(`paste ${job.hwnd}`);
    if (r.ok) { await sleep(500); restore(); }   // otherwise the rewrite stays on the clipboard for Ctrl+V
    return r;
  }).catch((e) => ({ ok: false, error: String(e) }));
  if (!res.ok) {
    send('rephrase-status', 'clipboard');
    if (Notification.isSupported()) new Notification({ title: 'Rephrased', body: 'You switched windows, so it is on the clipboard. Press Ctrl+V to paste it.', silent: true }).show();
  }
}

// Orange when listening, grey when paused or quiet during a call, so the state is visible at a glance.
function trayIcon(dim = false) {
  const size = 32, buf = Buffer.alloc(size * size * 4);
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
    const d = Math.hypot(x - 15.5, y - 16.5), i = (y * size + x) * 4;
    const ear = (y < 12 && (Math.abs(x - 8 - (12 - y) * 0.2) < (y - 2) * 0.45 || Math.abs(x - 23 + (12 - y) * 0.2) < (y - 2) * 0.45));
    const eye = Math.hypot(x - 11, y - 15) < 2.3 || Math.hypot(x - 20, y - 15) < 2.3;
    if (d < 12.5 || ear) {
      const [r, g, b] = eye ? [43, 33, 26] : dim ? [150, 150, 150] : [240, 138, 60];
      buf[i] = b; buf[i + 1] = g; buf[i + 2] = r; buf[i + 3] = 255;   // BGRA
    }
  }
  return nativeImage.createFromBitmap(buf, { width: size, height: size });
}

// While you hold or throw the pet the window grows to the whole work area so he can fly above the strip;
// it shrinks back once he has landed. The bottom edge stays put, so nothing on the page moves.
// He can be carried or thrown onto another screen: the window then moves to that screen's work area, and his
// strip stays on the screen he last landed on (homeId) until he is moved again.
let lifted = false, homeId = null;
function homeDisplay() { return screen.getAllDisplays().find((d) => d.id === homeId) || screen.getPrimaryDisplay(); }
function placeWindow() {
  const display = homeDisplay(), area = display.workArea;   // excludes the taskbar
  const height = lifted ? area.height : STRIP_HEIGHT;
  const bounds = { x: area.x, y: area.y + area.height - height, width: area.width, height };
  const moving = win.__displayId !== display.id;
  win.setBounds(bounds);
  // Moving onto a screen with a different scale can leave the size off by the ratio; the second call settles it.
  if (moving) { win.setBounds(bounds); win.__displayId = display.id; }
}
function sendDisplays() {
  send('displays', { home: homeDisplay().id, list: screen.getAllDisplays().map((d) => ({ id: d.id, bounds: d.bounds, area: d.workArea })) });
}

// After sleep, unlock or a monitor change: put the strip back in place, on top, and make it draw again.
// After a longer absence the page is reloaded too, which costs nothing and clears any stuck drawing.
function reassertWindow(reload) {
  if (!win || win.isDestroyed()) return;
  placeWindow();
  win.setAlwaysOnTop(true, 'screen-saver');
  win.moveTop();
  if (!win.isVisible()) win.showInactive();
  win.webContents.invalidate();
  if (reload) { logLine('reloading the pet page after', reload); load(); }
}

// The dismiss hotkey: drop a rephrase still on its way (so nothing gets pasted) and tell the page to stop and hide.
function dismiss() {
  rephrasing = null;
  send('dismiss');
}

function send(name, value) {
  if (!win || win.isDestroyed()) return;
  if (['talk', 'dictate', 'capture', 'rephrase'].includes(name)) { win.setAlwaysOnTop(true, 'screen-saver'); win.moveTop(); }
  win.webContents.send('pet:command', name, value);
}

// ---- pause and call detection ----
// Windows records which apps are using the microphone right now (LastUsedTimeStop = 0). While any app other
// than the pet itself has it open, e.g. a Teams call, the pet stops listening for the wake word and stays silent.
const MIC_KEY = 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\microphone';
const SELF = path.basename(process.execPath).toLowerCase();

function checkMicInUse() {
  execFile('reg.exe', ['query', MIC_KEY, '/s', '/v', 'LastUsedTimeStop'], { windowsHide: true, timeout: 5000 }, (err, out) => {
    if (err || !out) return;
    let key = '', busy = false;
    for (const line of out.split(/\r?\n/)) {
      if (line.startsWith('HKEY_')) key = line.toLowerCase();
      else if (/LastUsedTimeStop\s+REG_QWORD\s+0x0\s*$/i.test(line) && !key.includes(SELF)) busy = true;
    }
    if (busy !== state.inCall) { state.inCall = busy; applyQuiet(); }
  });
}

function applyQuiet() {
  send('incall', state.autoPause && state.inCall);
  if (tray) {
    tray.setImage(trayIcon(state.paused || (state.autoPause && state.inCall)));
    tray.setToolTip(state.paused ? 'Desktop Pet: paused' : (state.autoPause && state.inCall) ? 'Desktop Pet: quiet while you are on a call' : 'Desktop Pet');
    buildMenu();
  }
}

function togglePause(on) {
  state.paused = on === undefined ? !state.paused : on;
  pausedAt = state.paused ? Date.now() : 0;
  awayDuringPause = false;
  send('pause', state.paused);
  applyQuiet();
}

// Start with Windows: a per-user startup entry that runs this shell. The same path and arguments must be used
// to read it back, or Windows reports it as off.
const LOGIN_ITEM = () => ({ path: process.execPath, args: [app.getAppPath()] });
function setAutostart(on) {
  app.setLoginItemSettings({ openAtLogin: on, ...LOGIN_ITEM() });
  logLine('start with Windows', on ? 'on' : 'off');
  if (tray) buildMenu();
}

function buildMenu() {
  const login = app.getLoginItemSettings(LOGIN_ITEM());
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: `Pause: don't listen or speak  (${keyLabel(PAUSE_HOTKEY)})`, type: 'checkbox',
      checked: state.paused, click: (item) => togglePause(item.checked) },
    { label: 'Go quiet automatically during calls' + (state.autoPause && state.inCall ? '  (on a call now)' : ''), type: 'checkbox',
      checked: state.autoPause, click: (item) => { state.autoPause = item.checked; applyQuiet(); } },
    { type: 'separator' },
    { label: `Talk  (${keyLabel(HOTKEY)})`, click: () => send('talk') },
    { label: `Stop and hide  (${keyLabel(DISMISS_HOTKEY)})`, click: () => dismiss() },
    { label: 'Type a message', click: () => { win.focus(); send('type'); } },
    { label: `Dictate into the active app  (${keyLabel(DICTATE_HOTKEY)})`, click: () => send('dictate') },
    { label: `Listen, then summarise  (${keyLabel(CAPTURE_HOTKEY)})`, click: () => send('capture') },
    { label: `Rephrase selected text: select it, then press ${keyLabel(REPHRASE_HOTKEY)}`, enabled: false },
    { type: 'separator' },
    { label: `Listen for the wake word ("${WAKE_PHRASE}")`, type: 'checkbox', checked: state.handsfree,
      click: (item) => { state.handsfree = item.checked; send('handsfree', item.checked); } },
    { label: `Silent mode: show messages, don't speak  (${keyLabel(SILENT_HOTKEY)})`, type: 'checkbox', checked: state.silent,
      click: (item) => { item.checked = state.silent; send('silent'); } },
    { label: 'Mute voice', type: 'checkbox', checked: state.muted,
      click: (item) => { state.muted = item.checked; send('mute', item.checked); } },
    { label: 'Stay on screen (otherwise it comes only when called)', type: 'checkbox', checked: state.stay,
      click: (item) => { state.stay = item.checked; send('stay', item.checked); } },
    { type: 'separator' },
    { label: 'Open chat and lists', click: () => shell.openExternal(ORIGIN + '/') },
    { label: 'Settings', click: () => shell.openExternal(ORIGIN + '/settings') },
    { label: 'Status and usage', click: () => shell.openExternal(ORIGIN + '/status') },
    { label: 'Start with Windows', type: 'checkbox', checked: login.openAtLogin,
      click: (item) => setAutostart(item.checked) },
    { label: 'Reload', click: () => load() },
    { label: 'Quit', click: () => { quitReason = 'Quit from the tray menu'; app.quit(); } },
  ]));
}

function load() {
  win.loadURL(ORIGIN + '/pet/').catch(() => {});
}

function createWindow() {
  win = new BrowserWindow({
    transparent: true, frame: false, resizable: false, movable: false, hasShadow: false,
    skipTaskbar: true, alwaysOnTop: true, show: false, backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true, nodeIntegration: false, sandbox: true,
      autoplayPolicy: 'no-user-gesture-required', backgroundThrottling: false,
    },
  });
  win.setAlwaysOnTop(true, 'screen-saver');
  win.setIgnoreMouseEvents(true, { forward: true });
  placeWindow();
  win.once('ready-to-show', () => win.showInactive());
  // The window must never close on its own (Alt+F4 while the type box has focus would otherwise quit the pet).
  win.on('close', (e) => { if (!quitting) { e.preventDefault(); logLine('window close blocked'); } });
  // If the page's process dies (out of memory, GPU trouble after sleep), load it again.
  win.webContents.on('render-process-gone', (_e, details) => {
    logLine('pet page process gone:', details.reason, details.exitCode);
    setTimeout(() => { if (win && !win.isDestroyed()) load(); }, 2000);
  });
  win.webContents.on('unresponsive', () => logLine('pet page unresponsive'));

  // The backend may not be up yet (Docker still starting): keep retrying quietly.
  win.webContents.on('did-fail-load', (_e, _code, _desc, _url, isMainFrame) => { if (isMainFrame) setTimeout(load, 3000); });
  // This window only ever shows the pet page; anything else opens in the normal browser.
  win.webContents.on('will-navigate', (e, url) => { if (!url.startsWith(ORIGIN + '/')) e.preventDefault(); });
  win.webContents.setWindowOpenHandler(({ url }) => { if (/^https?:/.test(url)) shell.openExternal(url); return { action: 'deny' }; });
  load();
}

let quitting = false;
app.setAppUserModelId('local.desktop-pet');
app.on('before-quit', () => { quitting = true; logLine('quitting:', quitReason); });
app.on('child-process-gone', (_e, details) => logLine('helper process gone:', details.type, details.reason, details.exitCode));
// Starting a second copy hands its arguments to this one: "--autostart=on|off" switches Start with Windows.
app.on('second-instance', (_e, argv) => {
  const flag = argv.find((a) => a.startsWith('--autostart='));
  if (flag) setAutostart(flag.endsWith('=on'));
});
if (!app.requestSingleInstanceLock()) { quitReason = 'another copy was already running'; app.quit(); }
else app.whenReady().then(() => {
  // Microphone access is granted to the pet page only.
  session.defaultSession.setPermissionRequestHandler((wc, permission, done) => {
    done(permission === 'media' && wc.getURL().startsWith(ORIGIN + '/'));
  });
  session.defaultSession.setPermissionCheckHandler((_wc, permission, origin) => permission === 'media' && origin.startsWith(ORIGIN));

  logLine('started', process.argv.slice(1).join(' '));
  createWindow();
  tray = new Tray(trayIcon());
  tray.setToolTip('Desktop Pet');
  tray.on('click', () => send('talk'));
  buildMenu();
  for (const [key, action] of [[HOTKEY, () => send('talk')], [DISMISS_HOTKEY, () => dismiss()], [PAUSE_HOTKEY, () => togglePause()],
    [DICTATE_HOTKEY, () => send('dictate')], [CAPTURE_HOTKEY, () => send('capture')], [SILENT_HOTKEY, () => send('silent')],
    [REPHRASE_HOTKEY, () => { rephraseSelection().catch((e) => logLine('rephrase failed:', e && e.stack || e)); }]]) {
    try { hotkeyOk[key] = globalShortcut.register(key, action); } catch { hotkeyOk[key] = false; }
  }
  if (hotkeyOk[REPHRASE_HOTKEY]) startKeys();   // warm, so the first rephrase is as quick as the rest
  logLine('shortcuts:', Object.entries(hotkeyOk).map(([k, ok]) => `${k} ${ok ? 'ok' : 'TAKEN'}`).join(', '));
  buildMenu();
  // How long since the last keyboard or mouse input: the server waits for the user before briefings and wrap-ups,
  // and counts working time for break reminders.
  setInterval(() => {
    const idle = powerMonitor.getSystemIdleTime();
    send('idle', idle);
    if (!state.paused) return;
    if (idle >= AWAY_RESUMES_S) awayDuringPause = true;
    if ((awayDuringPause && idle < 10) || Date.now() - pausedAt > PAUSE_MAX_MS) {
      console.log('pause lifted', awayDuringPause ? 'after a break' : 'after three hours');
      togglePause(false);
    }
  }, 5000);
  // After sleep or a locked screen the microphone capture is often dead: reopen it. A long sleep also lifts a pause.
  powerMonitor.on('suspend', () => { suspendedAt = Date.now(); logLine('suspend'); });
  powerMonitor.on('shutdown', () => { quitReason = 'Windows shutting down'; });
  powerMonitor.on('lock-screen', () => { suspendedAt = suspendedAt || Date.now(); });
  const wake = () => {
    logLine('resume');
    const away = suspendedAt ? Date.now() - suspendedAt : 0;
    if (state.paused && away >= AWAY_RESUMES_S * 1000) awayDuringPause = true;
    suspendedAt = 0;
    // A reload also reopens the microphone; a short lock only needs the mic reset and a redraw.
    setTimeout(() => {
      if (away >= 60000 || !away) reassertWindow('waking up');
      else { reassertWindow(); send('mic-reset'); }
    }, 2000);
  };
  powerMonitor.on('resume', wake);
  powerMonitor.on('unlock-screen', wake);
  setInterval(checkMicInUse, 5000);
  for (const ev of ['display-metrics-changed', 'display-added', 'display-removed']) screen.on(ev, () => setTimeout(() => { reassertWindow(); sendDisplays(); }, 1000));
  // Other always-on-top windows (a full-screen Citrix or remote desktop session, full-screen video, some meeting apps)
  // put themselves above the strip whenever they are clicked: raise it back every few seconds. The strip is
  // click-through and see-through, so being on top costs nothing; moveTop never takes the keyboard focus.
  setInterval(() => {
    if (!win || win.isDestroyed()) return;
    win.setAlwaysOnTop(true, 'screen-saver');
    win.moveTop();
  }, 3000);

  ipcMain.on('pet:ignore-mouse', (_e, ignore) => win.setIgnoreMouseEvents(ignore, { forward: true }));
  ipcMain.on('pet:lift', (_e, on, displayId) => {
    if (screen.getAllDisplays().some((d) => d.id === displayId)) homeId = displayId;
    lifted = !!on; placeWindow();
  });
  ipcMain.on('pet:copy-text', (e, text) => {
    if (e.senderFrame && e.senderFrame.url.startsWith(ORIGIN + '/pet/')) clipboard.writeText(String(text || '').slice(0, 20000));
  });
  ipcMain.on('pet:type-text', (e, text) => {
    if (e.senderFrame && e.senderFrame.url.startsWith(ORIGIN + '/pet/')) typeText(text);
  });
  ipcMain.on('pet:rephrase-result', (e, id, text, error) => {
    if (!e.senderFrame || !e.senderFrame.url.startsWith(ORIGIN + '/pet/')) return;
    if (error || !text) { if (rephrasing && rephrasing.id === id) rephrasing = null; return; }
    pasteRephrase(String(id), String(text).slice(0, 20000)).catch((err) => logLine('rephrase paste failed:', err && err.stack || err));
  });
  ipcMain.on('pet:notify', (_e, title, body) => { if (Notification.isSupported()) new Notification({ title, body, silent: true }).show(); });
  ipcMain.handle('pet:host-call', (e, op, args) => {
    if (!e.senderFrame || !e.senderFrame.url.startsWith(ORIGIN + '/pet/')) return { ok: false, error: 'Not allowed from this page.' };
    return hostCall(op, args);
  });
  ipcMain.on('pet:stay', (_e, on) => { if (state.stay !== !!on) { state.stay = !!on; if (tray) buildMenu(); } });
  ipcMain.on('pet:silent', (_e, on) => { if (state.silent !== !!on) { state.silent = !!on; if (tray) buildMenu(); } });
  ipcMain.on('pet:ready', (_e, settings) => {
    state = { ...state, ...settings }; buildMenu();
    sendDisplays();
    // The page was (re)loaded: give it the current pause and call state again.
    if (state.paused) send('pause', true);
    send('hotkey', hotkeyOk[HOTKEY] === false ? 'the tray icon' : keyLabel(HOTKEY));
    send('incall', state.autoPause && state.inCall);
  });

  // --selftest: bring the pet up, capture it in each pose to client/selftest-<pose>.png, print its state, quit.
  if (SELFTEST) setTimeout(async () => {
    const js = (code) => win.webContents.executeJavaScript(code).catch((e) => String(e));
    const before = await js('JSON.stringify(window.__pet ? window.__pet.state() : null)');
    const shots = [];
    for (const pose of ['idle', 'talk', 'listen', 'think', 'sit', 'walk']) {
      const rect = JSON.parse(await js(`(async () => {
        window.__pet.setStay(true); window.__pet.arrive(); window.__pet.setAnim('${pose}');
        document.getElementById('pet').style.setProperty('--mouth', '${pose === 'talk' ? 0.7 : 0}');
        await new Promise(r => setTimeout(r, 700));
        const r = document.getElementById('pet').getBoundingClientRect();
        return JSON.stringify({ x: Math.round(r.left) - 20, y: Math.round(r.top) - 20, width: 160, height: 180 });
      })()`));
      const image = await win.webContents.capturePage(rect);
      const out = path.join(__dirname, `selftest-${pose}.png`);
      fs.writeFileSync(out, image.toPNG());
      shots.push(out);
    }
    await js("window.__pet.setStay(false)");
    console.log('selftest', JSON.stringify({ bounds: win.getBounds(), before: JSON.parse(before), shots }));
    app.quit();
  }, 6000);
});

app.on('will-quit', () => { globalShortcut.unregisterAll(); if (keysProc) keysProc.kill(); if (bridge) bridge.child.kill(); });
app.on('window-all-closed', () => { quitReason = 'all windows closed'; app.quit(); });
