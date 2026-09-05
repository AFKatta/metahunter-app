'use strict';

/**
 * Metahunter desktop shell.
 *
 * Electron owns the window, the tray and the lifecycle. All the actual
 * work happens in the Python backend, which serves both the API and the
 * built React UI. This process:
 *
 *   1. picks a TCP port, preferring the same one every launch,
 *   2. spawns the backend bound to it with the browser-opening disabled,
 *   3. waits for /api/health to answer,
 *   4. points a BrowserWindow at it.
 *
 * The port is chosen here rather than by the backend so we always know
 * where to connect; the backend's own port-scanning fallback stays for
 * people running it standalone from a terminal. Keeping the port stable
 * matters more than it looks: it is what makes the window's origin
 * stable, and therefore what lets the UI keep a cache between runs.
 */

const { app, BrowserWindow, Tray, Menu, shell, dialog, nativeImage, nativeTheme } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const net = require('net');
const http = require('http');
const { autoUpdater } = require('electron-updater');

// Running unpackaged means we were launched from the source tree, so the
// source tree is what should run — otherwise a months-old dist\ folder
// wins silently and your changes appear to do nothing. Electron already
// tracks this, so no env var and no cross-env dependency is needed.
// METAHUNTER_DEV stays as an opt-in for backend log passthrough.
const isDev = !app.isPackaged;
const verbose = isDev || process.env.METAHUNTER_DEV === '1';
const HEALTH_TIMEOUT_MS = 60000;
const HEALTH_POLL_MS = 250;

let mainWindow = null;
let tray = null;
let backend = null;
let backendPort = null;
let isQuitting = false;
let backendExitInfo = null;
let updateReady = false;
let updateStatus = 'Checking for updates…';

/* ------------------------------------------------------------------ */
/* backend discovery                                                    */
/* ------------------------------------------------------------------ */

/**
 * Locate the backend executable.
 *
 * Packaged: extraResources puts the PyInstaller folder at
 * resources/backend/. Dev: fall back to running serve.py out of the repo
 * with the project venv, so `npm start` works without a build first.
 */
function resolveBackend() {
  const packaged = path.join(process.resourcesPath || '', 'backend', 'Metahunter.exe');
  if (fs.existsSync(packaged)) {
    return { command: packaged, args: [], cwd: path.dirname(packaged) };
  }

  const repoRoot = path.resolve(__dirname, '..');
  const venvPython = path.join(repoRoot, '.venv', 'Scripts', 'python.exe');
  const serveScript = path.join(repoRoot, 'scripts', 'serve.py');
  const fromSource = fs.existsSync(venvPython) && fs.existsSync(serveScript)
    ? { command: venvPython, args: [serveScript], cwd: repoRoot }
    : null;

  const builtExe = path.join(repoRoot, 'dist', 'Metahunter', 'Metahunter.exe');
  const fromBuild = fs.existsSync(builtExe)
    ? { command: builtExe, args: [], cwd: path.dirname(builtExe) }
    : null;

  // In dev, run the source tree. Otherwise a stale dist\ folder wins
  // silently and you spend an afternoon wondering why your changes did
  // nothing — the built exe can be months older than the checkout.
  if (isDev && fromSource) return fromSource;

  return fromBuild || fromSource;
}

// The window is served from http://127.0.0.1:<port>, and the browser
// scopes localStorage to that origin. A different port each launch
// therefore means a different origin, an empty cache, and the UI
// rebuilding everything from scratch every single time. So we ask for
// the same port every run and only wander if something else holds it.
const PREFERRED_PORTS = [8765, 8766, 8767, 8768, 8769];

function tryPort(port) {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.unref();
    srv.on('error', () => resolve(null));
    srv.listen(port, '127.0.0.1', () => {
      const actual = srv.address().port;
      srv.close(() => resolve(actual));
    });
  });
}

async function findFreePort() {
  for (const p of PREFERRED_PORTS) {
    const got = await tryPort(p);
    if (got) return got;
  }
  // Everything preferred is taken. Port 0 lets the OS pick one that is
  // definitely free — the cache is cold this run, which beats not
  // starting at all.
  const any = await tryPort(0);
  if (any) return any;
  throw new Error('no free port available on 127.0.0.1');
}

function waitForHealth(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      if (backendExitInfo) {
        reject(new Error(
          `Backend exited before it was ready (code ${backendExitInfo.code}).`
          + (backendExitInfo.tail ? `\n\n${backendExitInfo.tail}` : '')
        ));
        return;
      }
      if (Date.now() > deadline) {
        reject(new Error('Backend did not become ready in time.'));
        return;
      }
      const req = http.get(
        { host: '127.0.0.1', port, path: '/api/health', timeout: 2000 },
        (res) => {
          res.resume();
          if (res.statusCode === 200) resolve();
          else setTimeout(attempt, HEALTH_POLL_MS);
        }
      );
      req.on('error', () => setTimeout(attempt, HEALTH_POLL_MS));
      req.on('timeout', () => { req.destroy(); setTimeout(attempt, HEALTH_POLL_MS); });
    };
    attempt();
  });
}

function startBackend(port) {
  const found = resolveBackend();
  if (!found) {
    return Promise.reject(new Error(
      'Could not find the Metahunter backend.\n\n'
      + 'Build it with build.ps1, or run from the repo with a .venv present.'
    ));
  }

  const args = [
    ...found.args,
    '--port', String(port),
    '--host', '127.0.0.1',
    // Electron is the window; the backend must not also open a browser.
    '--no-open',
  ];

  backend = spawn(found.command, args, {
    cwd: found.cwd,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: {
      ...process.env,
      // The backend ships its own GitHub-polling updater that runs the
      // Inno installer with /SILENT. Under Electron this shell owns
      // updating, and two updaters racing to replace the same install
      // is how you corrupt one. The backend checks this and stands down.
      METAHUNTER_DISABLE_SELF_UPDATE: '1',
    },
  });

  // Keep a rolling tail so a startup failure can be shown to the user
  // instead of a blank window.
  const tail = [];
  const keep = (chunk) => {
    tail.push(chunk.toString());
    if (tail.length > 40) tail.shift();
    if (verbose) process.stdout.write(chunk);
  };
  backend.stdout.on('data', keep);
  backend.stderr.on('data', keep);

  backend.on('exit', (code, signal) => {
    backendExitInfo = { code, signal, tail: tail.join('').slice(-2000) };
    backend = null;
    if (!isQuitting) {
      dialog.showErrorBox(
        'Metahunter backend stopped',
        `The backend process exited (code ${code}).\n\n${backendExitInfo.tail}`
      );
      app.quit();
    }
  });

  return waitForHealth(port, HEALTH_TIMEOUT_MS);
}

function stopBackend() {
  if (!backend) return;
  const proc = backend;
  backend = null;
  try {
    if (process.platform === 'win32') {
      // The PyInstaller launcher spawns a child; taskkill /T gets the tree.
      spawn('taskkill', ['/pid', String(proc.pid), '/f', '/t'], { windowsHide: true });
    } else {
      proc.kill('SIGTERM');
    }
  } catch {
    /* process already gone */
  }
}


/* ------------------------------------------------------------------ */
/* auto-update                                                          */
/* ------------------------------------------------------------------ */

// How long after launch the first check runs.
//
// This was 45s on the theory that a check would compete with startup.
// That was over-cautious: the check is a single request for a ~200 byte
// manifest, which costs nothing next to spawning the backend and
// classifying a thousand matches. Waiting three quarters of a minute
// only made the app look like it was ignoring updates.
//
// It cannot happen *before* the window opens, because the app is the
// thing doing the checking — blocking startup on a network round trip
// would trade a fast launch for a slow one, and fail badly offline. The
// standard pattern, and what we do, is show the app immediately and
// check right behind it.
const UPDATE_FIRST_CHECK_MS = 3_000;
// Long-running sessions still get updates without restarting.
const UPDATE_INTERVAL_MS = 6 * 60 * 60 * 1000; // 6 hours

function setupUpdater() {
  // Only a packaged build has the metadata (and the installer path) the
  // updater needs; in dev it would just log a confusing error.
  if (!app.isPackaged) return;

  autoUpdater.autoDownload = true;
  // Never install behind the user's back mid-session: a silent restart
  // during a match would lose the game they are tracking.
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.logger = null;

  autoUpdater.on('update-downloaded', async (info) => {
    updateReady = true;
    rebuildTrayMenu();

    const { response } = await dialog.showMessageBox({
      type: 'info',
      buttons: ['Restart now', 'Later'],
      defaultId: 0,
      cancelId: 1,
      title: 'Update ready',
      message: `Metahunter ${info.version} is ready to install.`,
      detail:
        'The update installs when you restart. Nothing is lost — your '
        + 'match history and settings stay where they are.',
    });

    if (response === 0) {
      isQuitting = true;
      stopBackend();
      // isSilent=true, isForceRunAfter=true: no installer UI, reopen
      // the app when it finishes.
      autoUpdater.quitAndInstall(true, true);
    }
  });

  autoUpdater.on('checking-for-update', () => {
    updateStatus = 'Checking for updates…';
    rebuildTrayMenu();
  });

  autoUpdater.on('update-not-available', () => {
    // Say so. A silent updater is indistinguishable from a broken one,
    // which is exactly the conclusion a user draws after leaving the app
    // open overnight and seeing nothing.
    updateStatus = `Up to date (v${app.getVersion()})`;
    rebuildTrayMenu();
  });

  autoUpdater.on('update-available', (info) => {
    updateStatus = `Downloading v${info.version}…`;
    rebuildTrayMenu();
  });

  autoUpdater.on('download-progress', (p) => {
    updateStatus = `Downloading update… ${Math.round(p.percent)}%`;
    rebuildTrayMenu();
  });

  autoUpdater.on('error', (err) => {
    updateStatus = 'Update check failed';
    rebuildTrayMenu();
    // A failed check must never interrupt the app. Being offline, or
    // GitHub rate-limiting, is an ordinary Tuesday.
    console.error('[updater]', err && err.message ? err.message : err);
  });

  const check = () => autoUpdater.checkForUpdates().catch(() => {});
  setTimeout(check, UPDATE_FIRST_CHECK_MS);
  setInterval(check, UPDATE_INTERVAL_MS);
}

function checkForUpdatesNow() {
  if (!app.isPackaged) {
    dialog.showMessageBox({
      type: 'info',
      title: 'Updates',
      message: 'Running from source.',
      detail: 'Auto-update only applies to an installed build.',
    });
    return;
  }
  autoUpdater
    .checkForUpdates()
    .then((r) => {
      if (updateReady) return;
      const remote = r && r.updateInfo && r.updateInfo.version;
      if (!remote || remote === app.getVersion()) {
        dialog.showMessageBox({
          type: 'info',
          title: 'Updates',
          message: `Metahunter ${app.getVersion()} is up to date.`,
        });
      }
    })
    .catch((err) => {
      dialog.showMessageBox({
        type: 'warning',
        title: 'Updates',
        message: 'Could not check for updates.',
        detail: String(err && err.message ? err.message : err),
      });
    });
}

/* ------------------------------------------------------------------ */
/* window + tray                                                        */
/* ------------------------------------------------------------------ */

function iconPath(name) {
  const p = path.join(__dirname, 'assets', name);
  return fs.existsSync(p) ? p : null;
}

/**
 * Drop the HTTP cache when the app version changes.
 *
 * The window is served from a local HTTP server, so Chromium caches it
 * like any website — including index.html, whose URL stays the same
 * across releases while its contents change. After an update the
 * renderer would reload the *old* index.html from cache, which names
 * the old asset hashes, which are also cached. The backend updates,
 * the interface does not, and nothing anywhere reports an error.
 *
 * The server now sends no-store on index.html, which prevents this
 * from recurring. This handles the installs that already have a stale
 * copy, and costs one cache flush per upgrade — nothing the user
 * notices, since the assets are local.
 */
async function clearCacheOnUpgrade() {
  const { session } = require('electron');
  const stampFile = path.join(app.getPath('userData'), 'last-version');
  const current = app.getVersion();

  let previous = null;
  try {
    previous = fs.readFileSync(stampFile, 'utf8').trim();
  } catch {
    // No stamp: either a fresh install or one from before this existed.
    // Clearing once is harmless, and the second case is exactly the one
    // that needs it.
  }

  if (previous === current) return;

  try {
    await session.defaultSession.clearCache();
    if (verbose) console.log(`[metahunter] cleared cache (${previous || 'unknown'} -> ${current})`);
  } catch (err) {
    console.warn('[metahunter] could not clear cache:', err && err.message);
  }
  try {
    fs.mkdirSync(path.dirname(stampFile), { recursive: true });
    fs.writeFileSync(stampFile, current, 'utf8');
  } catch {
    // Not fatal — we would just clear the cache again next launch.
  }
}

function createWindow() {
  const icon = iconPath('icon.png');

  // The UI is a dark-first design, and Chromium inside Electron does not
  // reliably inherit the Windows app-theme setting, so it was reporting
  // prefers-color-scheme: light and the window opened white. Pin it.
  nativeTheme.themeSource = 'dark';

  mainWindow = new BrowserWindow({
    title: 'Metahunter',
    width: 1500,
    height: 940,
    minWidth: 1100,
    minHeight: 700,
    show: false,
    backgroundColor: '#0d1014',
    autoHideMenuBar: true,
    icon: icon || undefined,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  // The page sets its own <title>; without this the window caption
  // follows it and shows whatever the document happens to be called.
  mainWindow.on('page-title-updated', (e) => e.preventDefault());

  mainWindow.once('ready-to-show', () => mainWindow.show());

  // Links to anywhere other than our own server open in the real
  // browser rather than replacing the app UI.
  const isOwn = (url) => {
    try {
      const u = new URL(url);
      return u.hostname === '127.0.0.1' && String(u.port) === String(backendPort);
    } catch { return false; }
  };
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (!isOwn(url)) { shell.openExternal(url); return { action: 'deny' }; }
    return { action: 'allow' };
  });
  mainWindow.webContents.on('will-navigate', (e, url) => {
    if (!isOwn(url)) { e.preventDefault(); shell.openExternal(url); }
  });

  // Closing hides to tray; quitting is explicit via the tray menu.
  mainWindow.on('close', (e) => {
    if (!isQuitting && tray) {
      e.preventDefault();
      mainWindow.hide();
    }
  });

  mainWindow.on('closed', () => { mainWindow = null; });

  mainWindow.loadURL(`http://127.0.0.1:${backendPort}/`);
}

function showWindow() {
  if (!mainWindow) { createWindow(); return; }
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function rebuildTrayMenu() {
  if (!tray) return;
  tray.setContextMenu(Menu.buildFromTemplate(buildTrayTemplate()));
  tray.setToolTip(updateReady ? 'Metahunter — update ready' : 'Metahunter');
}

function buildTrayTemplate() {
  return [
    { label: updateStatus, enabled: false },
    { type: 'separator' },
    { label: 'Open Metahunter', click: showWindow },
    {
      label: 'Open in browser',
      click: () => shell.openExternal(`http://127.0.0.1:${backendPort}/`),
    },
    { type: 'separator' },
    {
      label: 'Start at login',
      type: 'checkbox',
      checked: app.getLoginItemSettings().openAtLogin,
      click: (item) => app.setLoginItemSettings({ openAtLogin: item.checked }),
    },
    { type: 'separator' },
    ...(updateReady
      ? [{
          label: 'Restart to update',
          click: () => {
            isQuitting = true;
            stopBackend();
            autoUpdater.quitAndInstall(true, true);
          },
        }]
      : [{ label: 'Check for updates…', click: checkForUpdatesNow }]),
    { type: 'separator' },
    { label: 'Quit', click: () => { isQuitting = true; app.quit(); } },
  ];
}

function createTray() {
  const p = iconPath('tray.ico') || iconPath('icon.png');
  if (!p) return;
  tray = new Tray(nativeImage.createFromPath(p));
  tray.setToolTip('Metahunter');
  tray.setContextMenu(Menu.buildFromTemplate(buildTrayTemplate()));
  tray.on('click', showWindow);
}

/* ------------------------------------------------------------------ */
/* lifecycle                                                            */
/* ------------------------------------------------------------------ */

if (!app.requestSingleInstanceLock()) {
  // Someone already holds the lock. In a packaged build that is the
  // right outcome: the running window gets focused and this copy exits
  // quietly, which is what a user double-clicking the icon expects.
  //
  // In development it is a trap. `npm start` appears to do nothing at
  // all - no window, no error, no output - because the *installed*
  // Metahunter is already running and owns the lock, so you sit there
  // wondering why your changes had no effect. Say so instead.
  if (!app.isPackaged) {
    dialog.showErrorBox(
      'Metahunter is already running',
      'Another copy already holds the single-instance lock, so this one '
      + 'exited without opening a window.' + '\\n\\n'
      + 'That is almost always the installed Metahunter. Quit it from '
      + 'its tray icon, then run npm start again - otherwise you are '
      + 'looking at the installed build, not your source changes.'
    );
  }
  app.quit();
} else {
  app.on('second-instance', showWindow);

  app.whenReady().then(async () => {
    try {
      await clearCacheOnUpgrade();
      backendPort = await findFreePort();
      await startBackend(backendPort);
      createTray();
      createWindow();
      setupUpdater();
    } catch (err) {
      dialog.showErrorBox('Metahunter could not start', String(err && err.message ? err.message : err));
      isQuitting = true;
      app.quit();
    }
  });

  app.on('window-all-closed', () => {
    // Tray keeps it alive on Windows; without a tray there is nothing
    // left to interact with, so exit.
    if (!tray) app.quit();
  });

  app.on('before-quit', () => { isQuitting = true; });
  app.on('will-quit', stopBackend);
  process.on('exit', stopBackend);
}
