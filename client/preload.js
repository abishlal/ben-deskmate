// The only bridge between the pet page and the desktop shell.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('petShell', {
  setIgnoreMouse: (ignore) => ipcRenderer.send('pet:ignore-mouse', !!ignore),
  // Grow the window to the whole work area of a screen while the pet is held or flying (false: back to the strip).
  lift: (on, displayId) => ipcRenderer.send('pet:lift', !!on, Number(displayId) || null),
  notify: (title, body) => ipcRenderer.send('pet:notify', String(title), String(body)),
  // Runs one allow-listed operation against Outlook tasks or OneNote; resolves to {ok, data} or {ok:false, error}.
  hostCall: (op, args) => ipcRenderer.invoke('pet:host-call', String(op), args || {}),
  // Types dictated text into the app that has the keyboard focus (paste through the clipboard).
  typeText: (text) => ipcRenderer.send('pet:type-text', String(text)),
  // Puts a summary or rewrite on the clipboard.
  copyText: (text) => ipcRenderer.send('pet:copy-text', String(text)),
  // The rewrite of the selected text (Ctrl+Alt+R), or why there is none; the shell pastes it over the selection.
  rephraseResult: (id, text, error) => ipcRenderer.send('pet:rephrase-result', String(id), String(text || ''), String(error || '')),
  ready: (settings) => ipcRenderer.send('pet:ready', settings),
  // Stay on screen switched from the pet (by voice), so the tray menu can show it.
  stay: (on) => ipcRenderer.send('pet:stay', !!on),
  // Silent mode switched (on the server), so the tray menu can show it.
  silent: (on) => ipcRenderer.send('pet:silent', !!on),
  onCommand: (handler) => ipcRenderer.on('pet:command', (_event, name, value) => handler(name, value)),
});
