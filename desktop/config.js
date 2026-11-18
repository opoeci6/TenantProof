"use strict";

// Pure helpers for the desktop shell. No electron import here so that
// check.js can load this file under plain Node.

const DEFAULT_APP_URL = "http://localhost:8000";

const WEB_PREFERENCES = {
  contextIsolation: true,
  sandbox: true,
  nodeIntegration: false,
  webSecurity: true,
  allowRunningInsecureContent: false,
};

function resolveAppUrl(raw = DEFAULT_APP_URL) {
  const url = new URL(raw);
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error(
      `APP_URL must use http or https, got "${url.protocol}" (file:// and other schemes are refused)`,
    );
  }
  return url.href;
}

function isSameOrigin(candidate, appUrl) {
  try {
    const origin = new URL(candidate).origin;
    // Opaque origins (file:, data:, etc.) report "null" and never match.
    return origin !== "null" && origin === new URL(appUrl).origin;
  } catch {
    return false;
  }
}

module.exports = {
  WEB_PREFERENCES,
  resolveAppUrl,
  isSameOrigin,
};
