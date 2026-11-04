// Tests for the dashboard, which had none.
//
// They drive a real browser against the real stack rather than a mocked one:
// the things most worth protecting here -- dragging a panel onto a grid cell,
// scrubbing a replay, a layout that survives a reload -- are exactly the
// things a mock would have to fake, and a faked answer proves nothing.
//
// Start the stack first:
//   python -m uvicorn app.main:app --port 8000   (from backend/)
//   npm start
"use strict";

const { defineConfig, devices } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "./tests/e2e",
  // Panels animate into place and the API is a real one, so a first paint is
  // not instant; generous per-test time, but no retries -- a test that only
  // passes sometimes is a test that is lying.
  timeout: 45_000,
  retries: 0,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.WEB_TARGET || "http://127.0.0.1:3000",
    viewport: { width: 1600, height: 950 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
