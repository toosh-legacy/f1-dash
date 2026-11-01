"use strict";

// The page around the circuit: the glass, the chrome on it, and the view
// before a session has run.

const { test, expect } = require("@playwright/test");
const { open, openPicker } = require("./helpers");

test.describe("The page", () => {
  test("connects and says so", async ({ page }) => {
    await open(page);
    await expect(page.locator("#statusText")).not.toHaveText("idle");
  });

  test("has one static panel, the standings, on the left", async ({ page }) => {
    await open(page);
    const tower = await page.locator("#tower").boundingBox();
    const viewport = page.viewportSize();
    expect(tower.x).toBeLessThan(viewport.width / 4);
    expect(tower.height).toBeGreaterThan(viewport.height * 0.8);
  });

  test("the picker lists what is next with a countdown", async ({ page }) => {
    await open(page);
    await openPicker(page);
    const next = page.locator("#results .result.is-next");
    // Only during the season: past the last race there is nothing to come.
    if (await next.count()) {
      await expect(next.first().locator("[data-countdown]")).toHaveText(/\d{2}:\d{2}:\d{2}/);
    }
  });

  test("a session still to come shows a countdown and no transport", async ({ page }) => {
    await open(page);
    await openPicker(page);
    const next = page.locator("#results .result.is-next");
    test.skip(!(await next.count()), "no session left to come this season");
    await next.first().click();
    await expect(page.locator("#modeTag")).toHaveText(/next/i);
    await expect(page.locator("#countdown")).toBeVisible();
    const first = await page.locator("#cdTime").textContent();
    await expect(page.locator("#cdTime")).not.toHaveText(first, { timeout: 3000 });
    await expect(page.locator("#transport")).toHaveCSS("opacity", "0");
  });
});
