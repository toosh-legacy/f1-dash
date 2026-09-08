"use strict";

const { test, expect } = require("@playwright/test");
const { open } = require("./helpers");

// A race that is actually built in the local database. Picking a real one is
// the point: the map, the running order and the transport all draw from a
// bundle, and a race with no bundle exercises none of them.
const RACE = "Silverstone";

/** Type into the finder and open a race whose replay is already built.
 *
 * Deliberately only a "ready" one. Choosing a race that has not been built
 * starts a build against OpenF1 -- minutes of real network, and a different
 * code path from the one these tests are about.
 */
async function chooseRace(page, query = RACE) {
  await page.locator("#search").fill(query);
  const results = page.locator("#results");
  await expect(results).toBeVisible();
  const ready = results.locator(".result", { has: page.locator(".badge.ready") }).first();
  await expect(ready).toBeVisible();
  await ready.click();
  // The bundle is fetched and the circuit drawn; the transport appears with it.
  await page.locator("#playBtn").waitFor({ state: "visible", timeout: 30_000 });
}

test.describe("Finding a race", () => {
  test("the finder offers matches as you type", async ({ page }) => {
    await open(page);
    await page.locator("#search").fill(RACE);
    const results = page.locator("#results");
    await expect(results).toBeVisible();
    await expect(results).toContainText(RACE);
    await expect(results.locator(".result")).not.toHaveCount(0);
  });

  test("a query that matches nothing does not offer a race", async ({ page }) => {
    await open(page);
    await page.locator("#search").fill("zzzzzz");
    await expect(page.locator("#results")).not.toContainText(RACE);
  });
});

test.describe("The circuit", () => {
  test("choosing a race draws the track", async ({ page }) => {
    await open(page);
    await chooseRace(page);

    // The outline is reconstructed from the position feed, so a long path is
    // the evidence that the whole pipeline ran. Polled rather than read once:
    // the transport appears as soon as the bundle lands, a moment before the
    // circuit is drawn from it.
    await expect
      .poll(async () => {
        const ds = await page
          .locator('.widget[data-panel="map"] svg path')
          .evaluateAll((paths) => paths.map((p) => (p.getAttribute("d") || "").length));
        return Math.max(0, ...ds);
      }, { timeout: 20_000 })
      .toBeGreaterThan(1000);
  });

  test("cars are drawn on the track", async ({ page }) => {
    await open(page);
    await chooseRace(page);
    const cars = page.locator('.widget[data-panel="map"] svg g.car');
    // A full grid, less anyone who has already retired.
    await expect.poll(async () => cars.count(), { timeout: 20_000 }).toBeGreaterThan(15);
  });
});

test.describe("Playback", () => {
  test("play advances the race and pause holds it", async ({ page }) => {
    await open(page);
    await chooseRace(page);

    const scrub = page.locator("#scrub");
    const at = async () => Number(await scrub.inputValue());

    await page.locator("#playBtn").click();
    await expect.poll(at, { timeout: 20_000 }).toBeGreaterThan(0);

    await page.locator("#playBtn").click();  // pause
    const held = await at();
    await page.waitForTimeout(1200);
    // Paused means paused: allow a frame in flight, not a second of racing.
    expect(Math.abs((await at()) - held)).toBeLessThan(3);
  });

  test("scrubbing moves the clock and the lap counter", async ({ page }) => {
    await open(page);
    await chooseRace(page);

    const before = await page.locator("#clock").textContent();
    await page.locator("#scrub").evaluate((slider) => {
      slider.value = String(Math.round(Number(slider.max) * 0.6));
      slider.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await page.waitForTimeout(400);

    await expect(page.locator("#clock")).not.toHaveText(before);
    await expect(page.locator("#lapCount")).not.toContainText("—");
  });

  test("the running order fills once a race is loaded", async ({ page }) => {
    await open(page);
    await chooseRace(page);
    const rows = page.locator('.widget[data-panel="order"] .order-row');
    await expect.poll(async () => rows.count(), { timeout: 20_000 }).toBeGreaterThan(15);
    // Whoever is first is the leader, and is labelled as such rather than by a gap.
    await expect(rows.first()).toContainText("leader");
    await expect(rows.first().locator(".pos")).toHaveText("1");
  });
});

test.describe("Prediction", () => {
  test("the projection panel fills for a loaded race", async ({ page }) => {
    await open(page);
    await chooseRace(page);

    await page.locator("#panelsBtn").click();
    await page.locator("#panelsMenu").getByText("Prediction", { exact: false }).first().click();
    const panel = page.locator('.widget[data-panel="prediction"]');
    await expect(panel).toBeVisible();
    // The projection is computed server-side on first ask, so it is slow the
    // first time and cached after.
    await expect(panel).not.toBeEmpty({ timeout: 30_000 });
  });
});
