const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('cnp2p', {
  request: (action, payload) => ipcRenderer.invoke('bridge:request', action, payload),
  loadSettings: () => ipcRenderer.invoke('settings:load'),
  saveSettings: (settings) => ipcRenderer.invoke('settings:save', settings),
  onEvent: (callback) => {
    const listener = (_event, message) => callback(message);
    ipcRenderer.on('bridge:event', listener);
    return () => ipcRenderer.removeListener('bridge:event', listener);
  },
});

