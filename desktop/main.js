const { app, BrowserWindow, ipcMain } = require('electron');
const { EventEmitter } = require('node:events');
const { randomUUID } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const readline = require('node:readline');

const ROOT = path.resolve(__dirname, '..');
const ALLOWED_ACTIONS = new Set([
  'start',
  'stop',
  'snapshot',
  'connect',
  'send',
  'ping',
  'offline',
  'nat',
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
    this.child = spawn(runtime.command, runtime.args, {
      cwd: app.isPackaged ? process.resourcesPath : ROOT,
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
      env: runtime.env,
    });

    const lines = readline.createInterface({ input: this.child.stdout });
    lines.on('line', (line) => this.handleLine(line));
    this.child.stderr.on('data', (chunk) => {
      const detail = chunk.toString().trim();
      if (detail) {
        this.emit('event', {
          event: 'status',
          data: { category: 'backend', state: 'warning', detail },
        });
      }
    });
    this.child.on('error', (error) => this.failAll(`Could not start Python: ${error.message}`));
    this.child.on('exit', (code) => {
      this.failAll(`Networking service stopped${code === null ? '' : ` with code ${code}`}`);
      this.child = null;
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
      }, action === 'start' ? 20_000 : 12_000);
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

function settingsPath() {
  return path.join(app.getPath('userData'), 'settings.json');
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
  };
}

function loadSettings() {
  let settings;
  try {
    settings = { ...defaultSettings(), ...JSON.parse(fs.readFileSync(settingsPath(), 'utf8')) };
  } catch {
    settings = defaultSettings();
  }
  return settings;
}

function sanitizeSettings(value) {
  const port = Number(value.port);
  return {
    name: String(value.name || '').trim().slice(0, 64),
    port: Number.isInteger(port) && port >= 0 && port <= 65_535 ? port : 9000,
    bootstrap: String(value.bootstrap || '').trim().slice(0, 512),
    relay: String(value.relay || '').trim().slice(0, 512),
    stun: String(value.stun || '').trim().slice(0, 512),
    lanDiscovery: value.lanDiscovery !== false,
    upnp: value.upnp !== false,
  };
}

function saveSettings(value) {
  const settings = sanitizeSettings(value);
  fs.mkdirSync(app.getPath('userData'), { recursive: true });
  fs.writeFileSync(settingsPath(), `${JSON.stringify(settings, null, 2)}\n`, 'utf8');
  return settings;
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1080,
    height: 720,
    minWidth: 860,
    minHeight: 600,
    backgroundColor: '#f6f5f2',
    title: 'CNP2P',
    icon: path.join(__dirname, 'assets', 'icon.png'),
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.on('closed', () => { mainWindow = null; });
}

app.whenReady().then(() => {
  ipcMain.handle('settings:load', () => loadSettings());
  ipcMain.handle('settings:save', (_event, value) => saveSettings(value));
  ipcMain.handle('bridge:request', async (_event, action, payload) => {
    if (!ALLOWED_ACTIONS.has(action)) throw new Error('Unsupported desktop action');
    const requestPayload = { ...(payload || {}) };
    if (action === 'start') {
      requestPayload.data_dir = path.join(app.getPath('userData'), 'node');
    }
    return bridge.request(action, requestPayload);
  });
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

app.on('before-quit', () => {
  void bridge.close();
});
