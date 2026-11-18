"use strict";

// Static security check for the desktop shell. Plain Node, no dependencies.
// Does NOT require main.js (it imports electron, which is not installed in CI).

const fs = require("fs");
const path = require("path");
const { WEB_PREFERENCES, resolveAppUrl, isSameOrigin } = require("./config");

const failures = [];

function expect(label, condition, detail) {
  if (!condition) {
    failures.push(detail ? `${label} (${detail})` : label);
  }
}

function attempt(fn) {
  try {
    return { ok: true, value: fn() };
  } catch (err) {
    return { ok: false, value: undefined, error: err.message };
  }
}

function checkWebPreferences() {
  expect("WEB_PREFERENCES.contextIsolation must be true", WEB_PREFERENCES.contextIsolation === true);
  expect("WEB_PREFERENCES.sandbox must be true", WEB_PREFERENCES.sandbox === true);
  expect("WEB_PREFERENCES.nodeIntegration must be false", WEB_PREFERENCES.nodeIntegration === false);
  expect("WEB_PREFERENCES must not contain a preload key", !("preload" in WEB_PREFERENCES));
}

function checkResolveAppUrl() {
  const fileUrl = attempt(() => resolveAppUrl("file:///etc/passwd"));
  expect("resolveAppUrl must refuse file:// URLs", !fileUrl.ok);

  const explicit = attempt(() => resolveAppUrl("http://localhost:8000"));
  expect(
    "resolveAppUrl(http://localhost:8000) must succeed",
    explicit.ok && explicit.value === "http://localhost:8000/",
    explicit.error,
  );

  const fallback = attempt(() => resolveAppUrl());
  expect(
    "resolveAppUrl() default must succeed",
    fallback.ok && fallback.value === "http://localhost:8000/",
    fallback.error,
  );
}

function checkIsSameOrigin() {
  const appUrl = "http://localhost:8000";
  expect(
    "isSameOrigin must be true for same origin",
    isSameOrigin("http://localhost:8000/intake/some-token", appUrl) === true,
  );
  expect(
    "isSameOrigin must be false for another origin",
    isSameOrigin("https://evil.example.com/", appUrl) === false,
  );
  expect("isSameOrigin must be false for a non-URL", isSameOrigin("not a url", appUrl) === false);
}

function checkMainSource() {
  const mainPath = path.join(__dirname, "main.js");
  const source = fs.readFileSync(mainPath, "utf8");

  const required = ["setWindowOpenHandler", "will-navigate", "will-redirect", "setPermissionRequestHandler"];
  for (const token of required) {
    expect(`main.js must contain "${token}"`, source.includes(token));
  }

  const forbidden = ["preload", "nodeIntegration: true", "loadFile"];
  for (const token of forbidden) {
    expect(`main.js must not contain "${token}"`, !source.includes(token));
  }
}

function run() {
  checkWebPreferences();
  checkResolveAppUrl();
  checkIsSameOrigin();
  checkMainSource();

  if (failures.length > 0) {
    console.error("desktop security check FAILED:");
    for (const failure of failures) {
      console.error(`  - ${failure}`);
    }
    process.exit(1);
  }
  console.log("desktop security flags OK");
}

run();
