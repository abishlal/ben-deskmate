// The only bridge between the pet page and the desktop shell.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('petShell', {
  setIgnoreMouse: (ignore) => ipcRenderer.send('pet:ignore-mouse', !!ignore),
  notify: (title, body) => ipcRenderer.send('pet:notify', String(title), String(body)),
  // Runs one allow-listed operation against Outlook tasks or OneNote; resolves to {ok, data} or {ok:false, error}.
  hostCall: (op, args) => ipcRenderer.invoke('pet:host-call', String(op), args || {}),
  // Types dictated text into the app that has the keyboard focus (paste through the clipboard).
  typeText: (text) => ipcRenderer.send('pet:type-text', String(text)),
  // Puts a summary or rewrite on the clipboard.
  copyText: (text) => ipcRenderer.send('pet:copy-text', String(text)),
  ready: (settings) => ipcRenderer.send('pet:ready', settings),
  onCommand: (handler) => ipcRenderer.on('pet:command', (_event, name, value) => handler(name, value)),
});
