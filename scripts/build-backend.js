const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const python = process.env.CNP2P_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
const executable = process.platform === 'win32' ? 'cnp2p-backend.exe' : 'cnp2p-backend';
const destination = path.join(root, 'build', 'backend', executable);

fs.mkdirSync(path.dirname(destination), { recursive: true });

const result = spawnSync(
  python,
  [
    '-m',
    'PyInstaller',
    '--noconfirm',
    '--clean',
    '--onefile',
    '--name',
    'cnp2p-backend',
    '--paths',
    path.join(root, 'src'),
    '--distpath',
    path.join(root, 'build', 'backend'),
    '--workpath',
    path.join(root, 'build', 'pyinstaller'),
    '--specpath',
    path.join(root, 'build', 'pyinstaller'),
    path.join(root, 'scripts', 'backend_entry.py'),
  ],
  { cwd: root, stdio: 'inherit' },
);

if (result.error) {
  console.error(`Could not start PyInstaller: ${result.error.message}`);
  process.exit(1);
}
if (result.status !== 0 || !fs.existsSync(destination)) {
  console.error('The CNP2P backend executable was not created.');
  console.error('Install build dependencies with: python -m pip install -e ".[desktop]"');
  process.exit(result.status || 1);
}

console.log(`Backend ready: ${destination}`);

