const {
  app,
  BrowserWindow,
  Notification,
  dialog,
  ipcMain,
  nativeTheme,
  net,
  protocol,
  shell,
} = require('electron');
const { EventEmitter } = require('node:events');
const { randomUUID } = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { spawn } = require('node:child_process');
const readline = require('node:readline');

const ROOT = path.resolve(__dirname, '..');
const MEDIA_SCHEME = 'cnp2p-media';
const ALLOWED_ACTIONS = new Set([
  'start',
  'stop',
  'snapshot',
  'connect',
  'send',
  'ping',
  'offline',
  'nat',
  'conversations',
  'history',
  'send_file',
  'cancel_transfer',
  'retry',
  'mark_read',
  'rename_contact',
  'set_verified',
  'delete_conversation',
  'contact_info',
]);
const REQUEST_TIMEOUTS = { start: 20_000, send: 45_000, retry: 45_000, send_file: 120_000, connect: 30_000 };
const CLIPBOARD_IMAGE_TYPES = new Map([
  ['image/png', 'png'],
  ['image/jpeg', 'jpg'],
  ['image/webp', 'webp'],
  ['image/gif', 'gif'],
]);
const MAX_CLIPBOARD_BYTES = 25 * 1024 * 1024;

// CNP2P_PROFILE=alice npm start keeps a separate identity, history, and
// settings, so two instances can talk to each other on one computer.
const profile = String(process.env.CNP2P_PROFILE || '').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 32);
if (profile) {
  app.setPath('userData', path.join(app.getPath('appData'), `${app.getName()}-${profile}`));
}

protocol.registerSchemesAsPrivileged([
  { scheme: MEDIA_SCHEME, privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true } },
]);

class PythonBridge extends EventEmitter {
  constructor() {
    super();
    this.child = null;
    this.pending = new Map();
  }

  backendRuntime() {
    if (app.isPackaged) {
      const executable = path.join(
        process.resourcesPath,
        'backend',
        process.platform === 'win32' ? 'cnp2p-backend.exe' : 'cnp2p-backend',
      );
      if (!fs.existsSync(executable)) {
        throw new Error('The packaged networking service is missing');
      }
      return { command: executable, args: [], env: { ...process.env } };
    }
    if (process.env.CNP2P_PYTHON) {
      return this.pythonRuntime(process.env.CNP2P_PYTHON);
    }
    const local = process.platform === 'win32'
      ? path.join(ROOT, '.venv', 'Scripts', 'python.exe')
      : path.join(ROOT, '.venv', 'bin', 'python');
    const python = fs.existsSync(local) ? local : (process.platform === 'win32' ? 'python' : 'python3');
    return this.pythonRuntime(python);
  }

  pythonRuntime(python) {
    const sourceRoot = path.join(ROOT, 'src');
    const separator = process.platform === 'win32' ? ';' : ':';
    const pythonPath = [sourceRoot, process.env.PYTHONPATH].filter(Boolean).join(separator);
    return {
      command: python,
      args: ['-u', '-m', 'cnp2p.desktop_bridge'],
      env: { ...process.env, PYTHONPATH: pythonPath, PYTHONUNBUFFERED: '1' },
    };
  }

  ensureStarted() {
    if (this.child && !this.child.killed) {
      return;
    }
    const runtime = this.backendRuntime();
    const child = spawn(runtime.command, runtime.args, {
      cwd: app.isPackaged ? process.resourcesPath : ROOT,
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
      env: runtime.env,
    });
    this.child = child;

    const lines = readline.createInterface({ input: child.stdout });
    lines.on('line', (line) => this.handleLine(line));
    child.stderr.on('data', (chunk) => {
      const detail = chunk.toString().trim();
      if (detail) {
        this.emit('event', {
          event: 'status',
          data: { category: 'backend', state: 'warning', detail },
        });
      }
    });
    child.on('error', (error) => this.failAll(`Could not start Python: ${error.message}`));
    child.on('exit', (code) => {
      if (this.child === child) this.child = null;
      this.failAll(`Networking service stopped${code === null ? '' : ` with code ${code}`}`);
      this.emit('event', {
        event: 'status',
        data: { category: 'node', state: 'offline', detail: 'Networking service stopped' },
      });
    });
  }

  handleLine(line) {
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      this.emit('event', {
        event: 'status',
        data: { category: 'backend', state: 'warning', detail: 'Invalid backend response' },
      });
      return;
    }
    if (message.kind === 'event') {
      this.emit('event', message);
      return;
    }
    if (message.kind === 'response') {
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      clearTimeout(pending.timeout);
      if (message.ok) pending.resolve(message.data);
      else pending.reject(new Error(message.error || 'The networking service rejected the request'));
    }
  }

  failAll(detail) {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timeout);
      pending.reject(new Error(detail));
    }
    this.pending.clear();
  }

  request(action, payload = {}) {
    if (!ALLOWED_ACTIONS.has(action)) {
      return Promise.reject(new Error('Unsupported desktop action'));
    }
    this.ensureStarted();
    const id = randomUUID();
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error('The networking service did not respond in time'));
      }, REQUEST_TIMEOUTS[action] || 12_000);
      this.pending.set(id, { resolve, reject, timeout });
      this.child.stdin.write(`${JSON.stringify({ id, action, payload })}\n`, (error) => {
        if (error) {
          clearTimeout(timeout);
          this.pending.delete(id);
          reject(error);
        }
      });
    });
  }

  async close() {
    if (!this.child) return;
    try {
      await this.request('stop', {});
    } catch {
      // The process is still terminated below.
    }
    if (this.child && !this.child.killed) this.child.kill();
  }
}

const bridge = new PythonBridge();
let mainWindow = null;
let quitting = false;
let lastBadge = 0;

function settingsPath() {
  return path.join(app.getPath('userData'), 'settings.json');
}

function nodeDirectory() {
  return path.join(app.getPath('userData'), 'node');
}

function attachmentsDirectory() {
  return path.join(nodeDirectory(), 'attachments');
}

function defaultSettings() {
  return {
    name: '',
    port: 9000,
    bootstrap: '',
    relay: '',
    stun: '',
    lanDiscovery: true,
    upnp: true,
    theme: 'system',
    notifications: true,
    readReceipts: true,
    maxFileMb: 100,
  };
}

function loadSettings() {
  let settings;
  try {
    settings = { ...defaultSettings(), ...JSON.parse(fs.readFileSync(settingsPath(), 'utf8')) };
  } catch {
    settings = defaultSettings();
  }
  return sanitizeSettings(settings);
}

function sanitizeSettings(value) {
  const port = Number(value.port);
  const maxFileMb = Number(value.maxFileMb);
  return {
    name: String(value.name || '').trim().slice(0, 64),
    port: Number.isInteger(port) && port >= 0 && port <= 65_535 ? port : 9000,
    bootstrap: String(value.bootstrap || '').trim().slice(0, 512),
    relay: String(value.relay || '').trim().slice(0, 512),
    stun: String(value.stun || '').trim().slice(0, 512),
    lanDiscovery: value.lanDiscovery !== false,
    upnp: value.upnp !== false,
    theme: ['light', 'dark'].includes(value.theme) ? value.theme : 'system',
    notifications: value.notifications !== false,
    readReceipts: value.readReceipts !== false,
    maxFileMb: Number.isInteger(maxFileMb) && maxFileMb >= 1 && maxFileMb <= 2_048 ? maxFileMb : 100,
  };
}

function saveSettings(value) {
  const settings = sanitizeSettings(value);
  fs.mkdirSync(app.getPath('userData'), { recursive: true });
  fs.writeFileSync(settingsPath(), `${JSON.stringify(settings, null, 2)}\n`, 'utf8');
  nativeTheme.themeSource = settings.theme;
  return settings;
}

function insideAttachments(candidate) {
  const root = path.resolve(attachmentsDirectory());
  const resolved = path.resolve(String(candidate || ''));
  const relative = path.relative(root, resolved);
  if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) return null;
  return resolved;
}

function requireAttachment(candidate) {
  const resolved = insideAttachments(candidate);
  if (!resolved || !fs.existsSync(resolved)) throw new Error('That file is no longer available');
  return resolved;
}

function focusWindow() {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1120,
    height: 760,
    minWidth: 860,
    minHeight: 600,
    backgroundColor: nativeTheme.shouldUseDarkColors ? '#16181a' : '#f6f5f2',
    title: profile ? `CNP2P (${profile})` : 'CNP2P',
    icon: path.join(__dirname, 'assets', 'icon.png'),
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: true,
    },
  });
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  mainWindow.webContents.on('will-navigate', (event) => event.preventDefault());
  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.on('focus', () => mainWindow.flashFrame(false));
  mainWindow.on('closed', () => { mainWindow = null; });
}

function registerMediaProtocol() {
  // Inline images are served only from the attachments directory.
  protocol.handle(MEDIA_SCHEME, (request) => {
    const url = new URL(request.url);
    const resolved = url.hostname === 'file' ? insideAttachments(decodeURIComponent(url.pathname.slice(1))) : null;
    if (!resolved || !fs.existsSync(resolved)) {
      return new Response('Not found', { status: 404 });
    }
    return net.fetch(pathToFileURL(resolved).toString());
  });
}

function registerIpc() {
  ipcMain.handle('settings:load', () => loadSettings());
  ipcMain.handle('settings:save', (_event, value) => saveSettings(value));
  ipcMain.handle('bridge:request', async (_event, action, payload) => {
    if (!ALLOWED_ACTIONS.has(action)) throw new Error('Unsupported desktop action');
    const requestPayload = { ...(payload || {}) };
    if (action === 'start') {
      requestPayload.data_dir = nodeDirectory();
    }
    return bridge.request(action, requestPayload);
  });

  ipcMain.handle('files:pick', async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      title: 'Send files',
      properties: ['openFile', 'multiSelections'],
    });
    return result.canceled ? [] : result.filePaths;
  });

  ipcMain.handle('files:sendClipboard', async (_event, nodeId, bytes, mime) => {
    const extension = CLIPBOARD_IMAGE_TYPES.get(String(mime));
    if (!extension) throw new Error('Only PNG, JPEG, WebP, and GIF images can be pasted');
    const buffer = Buffer.from(bytes);
    if (!buffer.length || buffer.length > MAX_CLIPBOARD_BYTES) throw new Error('The pasted image is too large');
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'cnp2p-paste-'));
    const file = path.join(directory, `Pasted image ${stamp}.${extension}`);
    fs.writeFileSync(file, buffer);
    try {
      return await bridge.request('send_file', { node_id: nodeId, path: file });
    } finally {
      fs.rmSync(directory, { recursive: true, force: true });
    }
  });

  ipcMain.handle('files:open', async (_event, filePath) => {
    const error = await shell.openPath(requireAttachment(filePath));
    if (error) throw new Error(error);
  });
  ipcMain.handle('files:reveal', (_event, filePath) => {
    shell.showItemInFolder(requireAttachment(filePath));
  });
  ipcMain.handle('files:saveAs', async (_event, filePath) => {
    const source = requireAttachment(filePath);
    const result = await dialog.showSaveDialog(mainWindow, {
      defaultPath: path.join(app.getPath('downloads'), path.basename(source).replace(/^[0-9a-f]{8}-/, '')),
    });
    if (result.canceled || !result.filePath) return false;
    await fs.promises.copyFile(source, result.filePath);
    return true;
  });
  ipcMain.handle('files:openFolder', async () => {
    fs.mkdirSync(attachmentsDirectory(), { recursive: true });
    await shell.openPath(attachmentsDirectory());
  });

  ipcMain.handle('link:open', async (_event, url) => {
    const parsed = new URL(String(url));
    if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('Only web links can be opened');
    await shell.openExternal(parsed.toString());
  });

  ipcMain.handle('app:notify', (_event, notice) => {
    if (!mainWindow || mainWindow.isFocused() || !Notification.isSupported()) return;
    const notification = new Notification({
      title: String(notice.title || 'CNP2P').slice(0, 120),
      body: String(notice.body || '').slice(0, 240),
      silent: false,
    });
    notification.on('click', () => {
      focusWindow();
      if (mainWindow) mainWindow.webContents.send('ui:select-peer', String(notice.peerId || ''));
    });
    notification.show();
  });

  ipcMain.handle('app:badge', (_event, count) => {
    const unread = Math.max(0, Number(count) || 0);
    app.setBadgeCount(unread);
    if (mainWindow && unread > lastBadge && !mainWindow.isFocused()) mainWindow.flashFrame(true);
    lastBadge = unread;
  });

  ipcMain.handle('app:info', () => ({ profile, platform: process.platform, version: app.getVersion() }));
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', focusWindow);

  app.whenReady().then(() => {
    nativeTheme.themeSource = loadSettings().theme;
    registerMediaProtocol();
    registerIpc();
    bridge.on('event', (message) => {
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('bridge:event', message);
      }
    });
    createWindow();
    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
  });

  // Wait for the node to stop so UPnP mappings are removed, but never hang.
  app.on('before-quit', (event) => {
    if (quitting) return;
    event.preventDefault();
    quitting = true;
    const deadline = new Promise((resolve) => { setTimeout(resolve, 3_000); });
    Promise.race([bridge.close(), deadline]).finally(() => app.quit());
  });
}
