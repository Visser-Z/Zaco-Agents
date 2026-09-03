// ZacoAgents desktop shell.
//
// A thin native window around the deployed web app. All the real work -- login,
// PDF extraction, nett apportionment, Excel export -- lives in the web app and
// is loaded over HTTPS, so this file never duplicates any of it. Because the
// content is served from the deployment, pushing a fix to the web app updates
// every installed copy; there is nothing to re-distribute.
//
// The database is cloud Supabase, reached by the web app itself, so the shell
// needs an internet connection to be useful -- exactly as the browser version
// does.

const { app, BrowserWindow, Menu, shell, dialog } = require("electron");
const path = require("path");
const fs = require("fs");

const CONFIG = JSON.parse(
  fs.readFileSync(path.join(__dirname, "config.json"), "utf-8")
);
const APP_URL = CONFIG.appUrl;
const APP_ORIGIN = new URL(APP_URL).origin;

// Only one window; a second launch focuses the existing one.
if (!app.requestSingleInstanceLock()) {
  app.quit();
}

let win;

function createWindow() {
  win = new BrowserWindow({
    width: 1360,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    title: CONFIG.title,
    backgroundColor: "#F5F6F8", // matches the app ground so no white flash
    autoHideMenuBar: true,      // menu is available via Alt, but out of the way
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,   // the loaded page is remote; give it no Node access
      spellcheck: true,
    },
  });

  win.loadURL(APP_URL);

  // Keep navigation inside the app. Anything pointing elsewhere (a help link,
  // a Supabase docs link) opens in the user's real browser instead of hijacking
  // the app window.
  const external = (url) => {
    try { return new URL(url).origin !== APP_ORIGIN; }
    catch { return true; }
  };
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (external(url)) { shell.openExternal(url); return { action: "deny" }; }
    return { action: "allow" };
  });
  win.webContents.on("will-navigate", (e, url) => {
    if (external(url)) { e.preventDefault(); shell.openExternal(url); }
  });

  // A clear message if the app can't be reached, rather than Chromium's raw
  // error page -- the usual cause is no internet, which the shell can't fix.
  win.webContents.on("did-fail-load", (e, code, desc, url, isMainFrame) => {
    if (!isMainFrame || code === -3 /* aborted, e.g. redirect */) return;
    win.loadURL(
      "data:text/html," +
        encodeURIComponent(`
        <html><body style="font-family:system-ui;background:#F5F6F8;color:#16191F;
          display:grid;place-items:center;height:100vh;margin:0;text-align:center">
          <div>
            <h2 style="margin:0 0 8px">Can't reach ZacoAgents</h2>
            <p style="color:#4C5461;max-width:36ch;margin:0 auto 20px">
              This app needs an internet connection to open the database.
              Check your connection and try again.</p>
            <button onclick="location.href='${APP_URL}'"
              style="padding:10px 18px;border:0;border-radius:8px;background:#1F5FA8;
              color:#fff;font:600 14px system-ui;cursor:pointer">Retry</button>
          </div></body></html>`)
    );
  });

  buildMenu();
}

function buildMenu() {
  const template = [
    {
      label: "File",
      submenu: [
        { label: "Reload", accelerator: "CmdOrCtrl+R", click: () => win.reload() },
        { type: "separator" },
        { role: "quit", label: "Exit" },
      ],
    },
    {
      label: "Edit",
      submenu: [
        { role: "undo" }, { role: "redo" }, { type: "separator" },
        { role: "cut" }, { role: "copy" }, { role: "paste" }, { role: "selectAll" },
      ],
    },
    {
      label: "View",
      submenu: [
        { label: "Back", accelerator: "Alt+Left", click: () => win.webContents.canGoBack() && win.webContents.goBack() },
        { label: "Forward", accelerator: "Alt+Right", click: () => win.webContents.canGoForward() && win.webContents.goForward() },
        { type: "separator" },
        { role: "resetZoom" }, { role: "zoomIn" }, { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" },
      ],
    },
    {
      label: "Help",
      submenu: [
        {
          label: "About ZacoAgents",
          click: () =>
            dialog.showMessageBox(win, {
              type: "info",
              title: "About ZacoAgents",
              message: "ZacoAgents",
              detail:
                `Account-sales statements to Excel.\n\n` +
                `Version ${app.getVersion()}\nConnected to: ${APP_ORIGIN}`,
            }),
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

app.on("second-instance", () => {
  if (win) { if (win.isMinimized()) win.restore(); win.focus(); }
});

app.whenReady().then(createWindow);

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
