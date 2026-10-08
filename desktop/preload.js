const { contextBridge, ipcRenderer, webUtils } = require('electron');

contextBridge.exposeInMainWorld('cnp2p', {
  request: (action, payload) => ipcRenderer.invoke('bridge:request', action, payload),
  loadSettings: () => ipcRenderer.invoke('settings:load'),
  saveSettings: (settings) => ipcRenderer.invoke('settings:save', settings),
  pickFiles: () => ipcRenderer.invoke('files:pick'),
  pathForFile: (file) => webUtils.getPathForFile(file),
  sendClipboardImage: (nodeId, bytes, mime) => ipcRenderer.invoke('files:sendClipboard', nodeId, bytes, mime),
  openFile: (filePath) => ipcRenderer.invoke('files:open', filePath),
  revealFile: (filePath) => ipcRenderer.invoke('files:reveal', filePath),
  saveFileAs: (filePath) => ipcRenderer.invoke('files:saveAs', filePath),
  openAttachmentsFolder: () => ipcRenderer.invoke('files:openFolder'),
  openLink: (url) => ipcRenderer.invoke('link:open', url),
  notify: (notice) => ipcRenderer.invoke('app:notify', notice),
  setBadge: (count) => ipcRenderer.invoke('app:badge', count),
  appInfo: () => ipcRenderer.invoke('app:info'),
  onEvent: (callback) => {
    const listener = (_event, message) => callback(message);
    ipcRenderer.on('bridge:event', listener);
    return () => ipcRenderer.removeListener('bridge:event', listener);
  },
  onSelectPeer: (callback) => {
    const listener = (_event, nodeId) => callback(nodeId);
    ipcRenderer.on('ui:select-peer', listener);
    return () => ipcRenderer.removeListener('ui:select-peer', listener);
  },
});
