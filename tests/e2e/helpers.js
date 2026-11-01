"use strict";

// Tests for the dashboard.
//
// They drive a real browser against the real stack rather than a mocked one:
// the things most worth protecting here -- a replay drawn and played, a car
// picked out and its telemetry read, the race picker opening on what is live,
// next and past -- are exactly the things a mock would have to fake, and a
// faked answer proves nothing.
//
// Start the stack first:
//   python -m uvicorn app.main:app --port 8000   (from backend/)
//   npm start

const { expect } = require("@playwright/test");

// A race that is actually built in the local database. Picking a real one is
// the point: the map, the standings and the transport all draw from a bundle,
// and a race with no bundle exercises none of them.
const RACE = "Silverstone";

/** Open the dashboard and wait for it to finish booting.
 *
 * The boot overlay covers the page until the API answers, so every test would
 * otherwise begin by racing it.
 */
async function open(page) {
  await page.goto("/");
  await page.locator("#tower").waitFor({ state: "visible" });
  await page.waitForFunction(() => !document.getElementById("boot"), null, { timeout: 20_000 });
}

/** Open the race picker. */
async function openPicker(page) {
  await page.locator("#pickerBtn").click();
  await expect(page.locator("#picker")).toBeVisible();
}

/** Pick a race whose replay is already built, through the picker.
 *
 * Deliberately only a "ready" one. Choosing a race that has not been built
 * starts a build against OpenF1 -- minutes of real network, and a different
 * code path from the one these tests are about.
 */
async function chooseRace(page, query = RACE) {
  await openPicker(page);
  await page.locator("#search").fill(query);
  const ready = page.locator("#results .result", { has: page.locator(".badge.ready") }).first();
  await expect(ready).toBeVisible();
  await ready.click();
  // The frames stream in behind the circuit; play is enabled once they land.
  await expect(page.locator("#playBtn")).toBeEnabled({ timeout: 30_000 });
}

module.exports = { RACE, chooseRace, open, openPicker };
