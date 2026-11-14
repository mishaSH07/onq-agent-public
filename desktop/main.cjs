const { app, BrowserWindow, dialog, Menu, nativeImage, shell, Tray } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');
const { workerCommand, allowedNavigation, newScheduledCompletion } = require('./runtime.cjs');

const base = 'http://127.0.0.1:8765';
const started = Date.now();
const background = process.argv.includes('--background');
let window;
let tray;
let timer;
let quitting = false;
let initialized = false;
let openRequested = !background;
let lastCompletion;
let polling = false;

async function workerReady() {
  let response;
  try {
    response = await fetch(base + '/api/health', { signal: AbortSignal.timeout(1500) });
  } catch {
    return false;
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.application !== 'onq-library' || data.protocol !== 2 || data.version !== app.getVersion()) {
    throw Error('An older or incompatible worker is already running. Wait for its sync to finish, stop it with stop-worker.ps1, then reopen OnQ Agent.');
  }
  return true;
}

async function ensureWorker() {
  if (await workerReady()) return;
  const command = workerCommand(app.isPackaged, process.resourcesPath, path.resolve(__dirname, '..'));
  const child = spawn(command.file, command.args, {
    cwd: command.cwd, windowsHide: true, detached: true, stdio: 'ignore'
  });
  let launchError;
  child.on('error', error => { launchError = error; });
  child.unref();
  for (let attempt = 0; attempt < 90; attempt++) {
    if (launchError) throw launchError;
    if (await workerReady()) return;
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  throw Error('The local worker did not become ready. See %LOCALAPPDATA%\\onQ Library\\service.log.');
}

function openDashboard(activity = false) {
  if (!initialized) {
    openRequested = true;
    return;
  }
  const target = activity ? base + '/#activity' : base;
  if (!window) {
    window = new BrowserWindow({
      width: 1400, height: 950, minWidth: 850, minHeight: 640,
      backgroundColor: '#ffffff', title: 'OnQ Agent', autoHideMenuBar: true,
      webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true }
    });
    window.webContents.setWindowOpenHandler(({ url }) => {
      if (url.startsWith('https://')) shell.openExternal(url);
      return { action: 'deny' };
    });
    window.webContents.on('will-navigate', (event, url) => {
      if (!allowedNavigation(url)) event.preventDefault();
    });
    window.on('close', event => {
      if (!quitting) {
        event.preventDefault();
        window.hide();
      }
    });
    window.on('closed', () => { window = null; });
    window.loadURL(target).catch(reportDashboardError);
  } else if (activity) {
    window.loadURL(target).catch(reportDashboardError);
  }
  if (window.isMinimized()) window.restore();
  window.show();
  window.focus();
}

function reportDashboardError(error) {
  if (!quitting && error.code !== 'ERR_ABORTED') {
    dialog.showErrorBox('OnQ Agent could not open', error.message);
  }
}

async function pollScheduledCompletion() {
  if (polling || quitting) return;
  polling = true;
  try {
    const response = await fetch(base + '/api/scheduled-completion', { signal: AbortSignal.timeout(3000) });
    if (!response.ok || quitting) return;
    const { completion } = await response.json();
    if (!quitting && newScheduledCompletion(completion, lastCompletion, started)) {
      lastCompletion = completion.id;
      openDashboard(true);
    }
  } catch {
    // An unavailable worker may be restarting. Check again on the next poll.
  } finally {
    polling = false;
  }
}

async function launch() {
  tray = new Tray(nativeImage.createFromPath(path.join(__dirname, 'tray.png')));
  tray.setToolTip('OnQ Agent');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Open OnQ Agent', click: () => openDashboard() },
    { type: 'separator' },
    { label: 'Close tray app', click: () => app.quit() }
  ]));
  tray.on('double-click', () => openDashboard());
  await ensureWorker();
  initialized = true;
  if (openRequested) openDashboard();
  timer = setInterval(pollScheduledCompletion, 4000);
  void pollScheduledCompletion();
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', (_event, argv) => {
    if (!argv.includes('--background')) openDashboard();
  });
  app.on('before-quit', () => {
    quitting = true;
    clearInterval(timer);
    if (tray) tray.destroy();
  });
  app.whenReady().then(launch).catch(error => {
    reportDashboardError(error);
    app.quit();
  });
}
