"use strict";

const { test, expect } = require("@playwright/test");
const { LAYOUT_KEY, open, cellBox, dragPanel } = require("./helpers");

test.describe("Boot", () => {
  test("the page connects and says so", async ({ page }) => {
    await open(page);
    await expect(page.locator("#statusText")).not.toHaveText("idle");
    await expect(page.locator("#regime")).toContainText("2026");
  });

  test("the panels a viewer starts with are on the board", async ({ page }) => {
    await open(page);
    for (const id of ["map", "order", "notify", "control"]) {
      await expect(page.locator(`.widget[data-panel="${id}"]`)).toBeVisible();
    }
    // These two start put away.
    await expect(page.locator('.widget[data-panel="prediction"]')).toHaveCount(0);
    await expect(page.locator('.widget[data-panel="log"]')).toHaveCount(0);
  });
});

test.describe("The board", () => {
  test("a panel lands on whole grid cells when dragged", async ({ page }) => {
    await open(page);
    const before = await cellBox(page, "order");
    await dragPanel(page, "order", -180, 0);
    const after = await cellBox(page, "order");

    expect(after.c).not.toBe(before.c);
    // The point of the grid: an edge lands on a line, never between two.
    expect(Number.isInteger(after.c)).toBe(true);
    expect(after.cw).toBe(before.cw);
    expect(after.ch).toBe(before.ch);
  });

  test("the circuit is pinned and does not move", async ({ page }) => {
    await open(page);
    const before = await cellBox(page, "map");
    await dragPanel(page, "map", 260, 120);
    expect(await cellBox(page, "map")).toEqual(before);
  });

  test("a panel can be put away and brought back", async ({ page }) => {
    await open(page);
    await page.locator('.widget[data-panel="control"] .w-head button.ghost').last().click();
    await expect(page.locator('.widget[data-panel="control"]')).toHaveCount(0);

    await page.locator("#panelsBtn").click();
    await page.locator("#panelsMenu").getByText("Race control", { exact: false }).first().click();
    await expect(page.locator('.widget[data-panel="control"]')).toBeVisible();
  });

  test("the circuit has no close button, being the page itself", async ({ page }) => {
    await open(page);
    const closers = page.locator('.widget[data-panel="map"] .w-head button.ghost');
    for (const closer of await closers.all()) {
      await expect(closer).not.toHaveText("✕");
    }
  });

  test("a named size resizes the panel it belongs to", async ({ page }) => {
    await open(page);
    const before = await cellBox(page, "order");
    await page.locator('.widget[data-panel="order"] .w-head').hover();
    await page.locator('.widget[data-panel="order"] .w-head .size-btn').click();
    await page.locator('.widget[data-panel="order"] .size-list button', {
      hasText: "Small",
    }).click();
    await page.waitForTimeout(500);

    const after = await cellBox(page, "order");
    expect([after.cw, after.ch]).toEqual([5, 4]);
    expect(after.ch).toBeLessThan(before.ch);
  });
});

test.describe("Layout memory", () => {
  test("an arrangement survives a reload", async ({ page }) => {
    await open(page);
    const before = await cellBox(page, "order");
    // Left, into the space the circuit gives up -- a move the board accepts.
    // Moving a panel onto an occupied cell is refused, which would make this
    // test pass without ever having moved anything.
    await dragPanel(page, "order", -180, 0);
    const moved = await cellBox(page, "order");
    expect(moved.c).not.toBe(before.c);

    await page.reload();
    await page.locator('.widget[data-panel="map"]').waitFor({ state: "visible" });
    expect(await cellBox(page, "order")).toEqual(moved);
  });

  test("reset puts the panels back where they started", async ({ page }) => {
    await open(page);
    const original = await cellBox(page, "order");
    await dragPanel(page, "order", -200, 0);
    expect(await cellBox(page, "order")).not.toEqual(original);

    await page.locator("#resetLayout").click();
    await page.waitForTimeout(400);
    expect(await cellBox(page, "order")).toEqual(original);
  });

  test("a stored layout is what the page opens on", async ({ page }) => {
    await open(page);
    const before = await cellBox(page, "order");
    await dragPanel(page, "order", -180, 0);
    expect((await cellBox(page, "order")).c).not.toBe(before.c);

    const stored = await page.evaluate((key) => localStorage.getItem(key), LAYOUT_KEY);
    expect(stored).toBeTruthy();
    expect(JSON.parse(stored)).toHaveProperty("panels.order");
  });
});

test.describe("Presets", () => {
  test("Strategy opens the projection and narrows the circuit", async ({ page }) => {
    await open(page);
    const wide = await cellBox(page, "map");

    await page.locator("#panelsBtn").click();
    await page.locator("#panelsMenu").getByText("Strategy", { exact: false }).first().click();
    await page.waitForTimeout(450);

    await expect(page.locator('.widget[data-panel="prediction"]')).toBeVisible();
    expect((await cellBox(page, "map")).cw).toBeLessThan(wide.cw);
  });
});
