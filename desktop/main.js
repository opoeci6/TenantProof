"use strict";

const { app, BrowserWindow, session } = require("electron");
const { WEB_PREFERENCES, resolveAppUrl, isSameOrigin } = require("./config");

const appUrl = resolveAppUrl(process.env.APP_URL);

function lockDownPermissions() {
  session.defaultSession.setPermissionRequestHandler((_wc, _perm, cb) => cb(false));
  session.defaultSession.setPermissionCheckHandler(() => false);
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 860,
    webPreferences: WEB_PREFERENCES,
  });

  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  // event.url, not the positional url argument (deprecated since Electron 25).
  const blockOffOrigin = (event) => {
    if (!isSameOrigin(event.url, appUrl)) {
      event.preventDefault();
    }
  };
  win.webContents.on("will-navigate", blockOffOrigin);
  win.webContents.on("will-redirect", blockOffOrigin);

  win.loadURL(appUrl);
  return win;
}

app.whenReady().then(() => {
  lockDownPermissions();
  createWindow();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});
