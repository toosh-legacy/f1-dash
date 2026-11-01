"use strict";

const { test, expect } = require("@playwright/test");
const { open } = require("./helpers");

// Silverstone, the race: built locally, with a mapped surroundings file and
// race control that has yellows, a marshal call and blue flags in it.
const RACE_KEY = 11326;

async function openRaceAt(page, seconds) {
  await page.goto(`/?session=${RACE_KEY}`);
  await expect(page.locator("#playBtn")).toBeEnabled({ timeout: 30_000 });
  await page.locator("#scrub").evaluate((slider, s) => {
    slider.value = String(s);
    slider.dispatchEvent(new Event("input", { bubbles: true }));
  }, seconds);
}

const pathLength = (page, id) => page.locator(id).evaluate((node) => (node.getAttribute("d") || "").length);

test.describe("The surroundings", () => {
  test("a mapped circuit is drawn over its real grass, gravel and stands", async ({ page }) => {
    await openRaceAt(page, 600);
    await expect.poll(() => pathLength(page, "#osm-grass"), { timeout: 15_000 }).toBeGreaterThan(1000);
    expect(await pathLength(page, "#osm-gravel")).toBeGreaterThan(200);
    expect(await pathLength(page, "#osm-stand")).toBeGreaterThan(200);
    // The guessed surroundings step aside, and OpenStreetMap is credited.
    await expect(page.locator("#terrain")).toHaveCSS("display", "none");
    await expect(page.locator("#osmCredit")).toBeVisible();
    // The pit lane is a lane: edged, surfaced and marked.
    expect(await pathLength(page, "#pitMark")).toBeGreaterThan(100);
  });
});

test.describe("Flags on the track", () => {
  test("a double yellow colours its sector and puts out a waving marshal", async ({ page }) => {
    // 37:52 -- double yellow in sector 11, marshals on track at turn 10.
    await openRaceAt(page, 2272);
    await expect(page.locator("#flagEdges .flag-edge.yellow.double")).toHaveCount(1);
    await expect(page.locator("#marshals .marshal")).toHaveCount(2);
    await expect(page.locator("#marshals .marshal .m-arm")).toHaveCount(2);   // both flags up
    await expect(page.locator("#marshals .marshal.on-track")).toHaveCount(1);
  });

  test("a blue flag follows the car it was shown to, then goes", async ({ page }) => {
    await openRaceAt(page, 2229);
    const blue = page.locator("#flagEdges .flag-edge.blue");
    await expect(blue).not.toHaveCount(0);
    expect(((await blue.first().getAttribute("d")) || "").length).toBeGreaterThan(20);
    await page.locator("#scrub").evaluate((slider) => {
      slider.value = "2250";
      slider.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await expect(blue).toHaveCount(0);
    await expect(page.locator("#marshals .marshal")).toHaveCount(0);
  });

  test("the green track has a plain white edge", async ({ page }) => {
    await openRaceAt(page, 600);
    await expect(page.locator("#flagEdges .flag-edge")).toHaveCount(0);
  });
});

test.describe("Building a replay", () => {
  test("shows the steps and how many cars are in, then goes", async ({ page }) => {
    await open(page);
    await page.evaluate(async () => {
      const m = await import("/js/building.js");
      await m.showBuilding({ circuit: "Silverstone", name: "Race" });
      m.buildProgress({ stages: ["session", "drivers", "timing", "locations"], detail: { car: 44, done: 8, of: 22 } });
    });
    await expect(page.locator("#building")).toBeVisible();
    await expect(page.locator("#buildDetail")).toHaveText(/car 9 of 22/);
    await expect(page.locator('#buildSteps li[data-step="timing"]')).toHaveClass(/done/);
    await expect(page.locator('#buildSteps li[data-step="locations"]')).toHaveClass(/now/);
    // The trace takes the circuit's own shape once its layout is known.
    await expect.poll(() => pathLength(page, "#buildTrace")).toBeGreaterThan(400);
    await page.evaluate(async () => (await import("/js/building.js")).hideBuilding());
    await expect(page.locator("#building")).toBeHidden();
  });
});

test.describe("The countdown", () => {
  test("sits on its own plate, on one line, clear of the standings", async ({ page }) => {
    await page.setViewportSize({ width: 1100, height: 700 });
    await open(page);
    const clock = page.locator("#cdTime");
    await expect(clock).toBeVisible();
    const box = await clock.boundingBox();
    const tower = await page.locator("#tower").boundingBox();
    const plate = await page.locator(".cd-plate").boundingBox();
    expect(box.x).toBeGreaterThan(tower.x + tower.width);
    expect(box.height).toBeLessThan(140);                 // one line, not wrapped
    expect(box.x + box.width).toBeLessThanOrEqual(plate.x + plate.width + 1);
  });
});
