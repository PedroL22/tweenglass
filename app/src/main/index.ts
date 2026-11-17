// SPDX-License-Identifier: GPL-3.0-or-later
import { appendFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { app, BrowserWindow, ipcMain, Menu, nativeImage, Tray } from "electron";
import {
  type BackendCommand,
  type BackendEvent,
  DEFAULT_SETTINGS,
  type Settings,
  type Snapshot,
} from "../shared/protocol";
import { fmt, type Strings, strings } from "../shared/i18n";
import { Backend, backendScript } from "./backend";
import { iconFor } from "./icons";

app.setName("TweenGlass");
// Matches the .desktop file, so KDE shows the right icon and name for the window.
app.setDesktopName("dev.tweenglass.TweenGlass.desktop");

const startHidden = process.argv.includes("--hidden");
let win: BrowserWindow | null = null;
let tray: Tray | null = null;
let quitting = false;
let t: Strings = strings("en");

// ---------------------------------------------------------------- settings
const settingsFile = () => join(app.getPath("userData"), "settings.json");

function loadSettings(): Settings {
  try {
    return { ...DEFAULT_SETTINGS, ...JSON.parse(readFileSync(settingsFile(), "utf8")) };
  } catch {
    return { ...DEFAULT_SETTINGS };
  }
}

let settings = loadSettings();

function saveSettings(patch: Partial<Settings>): void {
  settings = { ...settings, ...patch };
  mkdirSync(app.getPath("userData"), { recursive: true });
  writeFileSync(settingsFile(), JSON.stringify(settings, null, 2));
}

// ---------------------------------------------------------------- logging
const logFile = () => join(app.getPath("userData"), "tweenglass.log");
function log(line: string): void {
  const stamped = `${new Date().toISOString()} ${line}`;
  if (!app.isPackaged) console.log(stamped);
  try {
    appendFileSync(logFile(), `${stamped}\n`);
  } catch {
    /* ignore */
  }
}

// ---------------------------------------------------------------- backend
const snapshot: Snapshot = {
  ready: null,
  state: null,
  stats: null,
  checks: null,
  windows: [],
  settings,
  backendAlive: false,
};

function onBackendEvent(ev: BackendEvent): void {
  switch (ev.type) {
    case "hello":
      snapshot.backendAlive = true;
      break;
    case "ready":
      snapshot.ready = ev;
      break;
    case "state":
      snapshot.state = ev;
      if (!ev.active) snapshot.stats = null;
      // Activated from the UI: get out of the way so the overlay mirrors the
      // target window and clicks land on it.
      if (ev.active && ev.reason === "activated" && win?.isVisible()) win.hide();
      break;
    case "stats":
      snapshot.stats = ev;
      break;
    case "windows":
      for (const w of ev.items) w.icon = iconFor(w.app_id);
      snapshot.windows = ev.items;
      break;
    case "checks": {
      const { type: _t, ...checks } = ev;
      snapshot.checks = checks;
      break;
    }
    case "error":
      log(`[backend:error] ${ev.message}`);
      break;
    case "backend-exit":
      snapshot.backendAlive = false;
      snapshot.state = null;
      snapshot.stats = null;
      log(`backend exited (${ev.code}), restarting=${ev.restarting}`);
      break;
  }
  updateTray();
  win?.webContents.send("backend:event", ev);
}

const backend = new Backend(backendScript(app.getAppPath()), () => settings, onBackendEvent, log);

// ---------------------------------------------------------------- tray
function trayIcon(active: boolean) {
  const img = nativeImage.createFromPath(join(app.getAppPath(), "resources", active ? "tray-active.png" : "tray.png"));
  return img.isEmpty() ? nativeImage.createFromPath(join(app.getAppPath(), "resources", "icon.png")) : img;
}

function showWindow(): void {
  if (!win) createWindow();
  win?.show();
  win?.focus();
}

function updateTray(): void {
  if (!tray) return;
  const active = snapshot.state?.active ?? false;
  const title = snapshot.state?.target?.title;
  tray.setImage(trayIcon(active));
  tray.setToolTip(active ? `TweenGlass — ${settings.multiplier}x: ${title ?? ""}` : "TweenGlass");
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: t.trayOpen, click: showWindow },
      { type: "separator" },
      active
        ? { label: t.trayStop, click: () => backend.send({ cmd: "deactivate" }) }
        : { label: fmt(t.trayHint, { keys: settings.toggle_keys }), enabled: false },
      { type: "separator" },
      { label: t.trayQuit, click: () => app.quit() },
    ]),
  );
}

// ---------------------------------------------------------------- window
function createWindow(): void {
  win = new BrowserWindow({
    width: 460,
    height: 760,
    minWidth: 420,
    minHeight: 600,
    show: false,
    title: "TweenGlass",
    backgroundColor: "#16181d",
    autoHideMenuBar: true,
    icon: join(app.getAppPath(), "resources", "icon.png"),
    webPreferences: {
      preload: join(__dirname, "../preload/index.js"),
      sandbox: true,
      contextIsolation: true,
    },
  });
  win.removeMenu();
  win.on("ready-to-show", () => {
    if (!startHidden || process.env.TWEENGLASS_SHOW) win?.show();
  });
  // Closing the window keeps TweenGlass in the tray (the shortcut keeps working).
  win.on("close", (e) => {
    if (!quitting && tray) {
      e.preventDefault();
      win?.hide();
    }
  });
  win.on("closed", () => {
    win = null;
  });
  if (process.env.ELECTRON_RENDERER_URL) void win.loadURL(process.env.ELECTRON_RENDERER_URL);
  else void win.loadFile(join(__dirname, "../renderer/index.html"));
}

// ---------------------------------------------------------------- IPC
ipcMain.handle("tg:snapshot", () => ({ ...snapshot, settings }));
ipcMain.handle("tg:send", (_e, cmd: BackendCommand) => {
  if (cmd.cmd === "set") {
    const { cmd: _c, ...patch } = cmd;
    saveSettings(patch);
  }
  backend.send(cmd);
});
ipcMain.handle("tg:hide", () => win?.hide());
ipcMain.handle("tg:quit", () => app.quit());

// ---------------------------------------------------------------- lifecycle
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", showWindow);

  void app.whenReady().then(() => {
    t = strings(app.getLocale() || process.env.LANG || "en");
    log(`starting TweenGlass ${app.getVersion()} (backend: ${backendScript(app.getAppPath())})`);
    tray = new Tray(trayIcon(false));
    tray.on("click", showWindow);
    updateTray();
    createWindow();
    backend.start();
  });

  // Keep running in the tray when the window is closed.
  app.on("window-all-closed", () => {});

  app.on("before-quit", (e) => {
    if (quitting) return;
    quitting = true;
    e.preventDefault();
    void backend.stop().finally(() => {
      tray?.destroy();
      app.exit(0);
    });
  });

  for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"] as const) process.on(sig, () => app.quit());
}
