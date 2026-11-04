"use strict";

const { test, expect } = require("@playwright/test");
const { RACE, chooseRace, open, openPicker } = require("./helpers");

test.describe("The race picker", () => {
  test("it opens from the session title and offers matches as you type", async ({ page }) => {
    await open(page);
    await openPicker(page);
    await expect(page.locator("#pickerBtn")).toHaveAttribute("aria-expanded", "true");
    await page.locator("#search").fill(RACE);
    await expect(page.locator("#results")).toContainText(RACE);
    await expect(page.locator("#results .result")).not.toHaveCount(0);
  });

  test("a query that matches nothing does not offer a race", async ({ page }) => {
    await open(page);
    await openPicker(page);
    await page.locator("#search").fill("zzzzzz");
    await expect(page.locator("#results")).not.toContainText(RACE);
  });

  test("escape puts it away", async ({ page }) => {
    await open(page);
    await openPicker(page);
    await page.locator("#search").press("Escape");
    await expect(page.locator("#picker")).toBeHidden();
  });

  test("choosing a replay says so in the top bar", async ({ page }) => {
    await open(page);
    await chooseRace(page);
    await expect(page.locator("#modeTag")).toHaveText(/replay/i);
    await expect(page.locator("#sessionTitle")).toContainText(RACE);
  });
});

test.describe("The circuit", () => {
  test("choosing a race draws the track", async ({ page }) => {
    await open(page);
    await chooseRace(page);
    // The outline is reconstructed from the position feed, so a long path is
    // the evidence that the whole pipeline ran.
    await expect
      .poll(async () => (await page.locator("#trackSurface").getAttribute("d") || "").length,
        { timeout: 20_000 })
      .toBeGreaterThan(1000);
  });

  test("the circuit stays clear of the standings", async ({ page }) => {
    await open(page);
    await chooseRace(page);
    const tower = await page.locator("#tower").boundingBox();
    const track = await page.locator("#trackSurface").boundingBox();
    expect(track.x).toBeGreaterThan(tower.x + tower.width);
  });

  test("cars are drawn on the track", async ({ page }) => {
    await open(page);
    await chooseRace(page);
    const cars = page.locator("#cars g.car");
    // A full grid, less anyone who has already retired.
    await expect.poll(async () => cars.count(), { timeout: 20_000 }).toBeGreaterThan(15);
  });
});

test.describe("A car, picked out", () => {
  test("clicking a car holds its telemetry over it", async ({ page }) => {
    await open(page);
    await chooseRace(page);
    await page.locator("#scrub").evaluate((slider) => {
      slider.value = "2400";
      slider.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const car = page.locator("#cars g.car[visibility=visible]").first();
    await car.click({ force: true });

    const probe = page.locator("#probe");
    await expect(probe).toBeVisible();
    // Real numbers from the feed, not the placeholder dashes.
    await expect(probe.locator(".gauge.speed .read")).toHaveText(/^\d+$/, { timeout: 20_000 });
    await expect(probe.locator(".gauge.gear .read")).toHaveText(/^(N|\d)$/);

    // Sitting over the car once it has sprung open: the car is within the
    // card's width. Polled, because the card grows out of the car.
    await expect.poll(async () => {
      const box = await probe.boundingBox();
      const dot = await car.locator(".car-dot").boundingBox();
      const carX = dot.x + dot.width / 2;
      return carX > box.x - 2 && carX < box.x + box.width + 2 && box.width > 250;
    }, { timeout: 3000 }).toBe(true);
  });

  test("the standings row selects the same car, and escape lets go", async ({ page }) => {
    await open(page);
    await chooseRace(page);
    const row = page.locator(".tower-row").nth(2);
    const number = await row.getAttribute("data-number");
    await row.click();
    await expect(page.locator(`#cars g.car[data-number="${number}"]`)).toHaveClass(/selected/);
    await expect(page.locator("#probe")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.locator("#probe")).toBeHidden();
  });
});

test.describe("Playback", () => {
  test("play advances the race and pause holds it", async ({ page }) => {
    await open(page);
    await chooseRace(page);

    const scrub = page.locator("#scrub");
    const at = async () => Number(await scrub.inputValue());
    const start = await at();

    await page.locator("#playBtn").click();
    await expect.poll(at, { timeout: 20_000 }).toBeGreaterThan(start);

    await page.locator("#playBtn").click();  // pause
    const held = await at();
    await page.waitForTimeout(1200);
    // Paused means paused: allow a frame in flight, not a second of racing.
    expect(Math.abs((await at()) - held)).toBeLessThan(3);
  });

  test("scrubbing moves the clock and the lap in the standings", async ({ page }) => {
    await open(page);
    await chooseRace(page);

    const before = await page.locator("#clock").textContent();
    const lapBefore = await page.locator("#towerLap").textContent();
    await page.locator("#scrub").evaluate((slider) => {
      slider.value = String(Math.round(Number(slider.max) * 0.6));
      slider.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await expect(page.locator("#clock")).not.toHaveText(before);
    await expect(page.locator("#towerLap")).not.toHaveText(lapBefore);
  });
});

test.describe("The standings", () => {
  test("fill once a race is loaded, leader first", async ({ page }) => {
    await open(page);
    await chooseRace(page);
    const rows = page.locator(".tower-row");
    await expect.poll(async () => rows.count(), { timeout: 20_000 }).toBeGreaterThan(15);
    await expect(rows.first()).toContainText(/leader/i);
    await expect(rows.first().locator(".pos")).toHaveText("1");
    await expect(page.locator("#towerCol")).toHaveText(/interval/i);
  });

  test("never show a negative interval", async ({ page }) => {
    await open(page);
    await chooseRace(page);
    for (const share of [0.2, 0.5, 0.8]) {
      await page.locator("#scrub").evaluate((slider, s) => {
        slider.value = String(Math.round(Number(slider.max) * s));
        slider.dispatchEvent(new Event("input", { bubbles: true }));
      }, share);
      const values = await page.locator(".tower-row .val .v").allTextContents();
      expect(values.filter((v) => v.includes("-"))).toEqual([]);
    }
  });
});

test.describe("The prediction", () => {
  test("is summoned over the map, follows the lap, and goes away", async ({ page }) => {
    await open(page);
    await chooseRace(page);
    await page.locator("#scrub").evaluate((slider) => {
      slider.value = "2400";
      slider.dispatchEvent(new Event("input", { bubbles: true }));
    });

    await page.locator("#predictBtn").click();
    const sheet = page.locator("#predict");
    await expect(sheet).toBeVisible();
    await expect(page.locator("#predictBtn")).toHaveAttribute("aria-pressed", "true");
    // Computed server-side on first ask, so slow the first time.
    const rows = page.locator("#predictRows .predict-row");
    await expect.poll(async () => rows.count(), { timeout: 60_000 }).toBeGreaterThan(15);
    await expect(rows.first().locator(".p")).toHaveText("1");
    const lap = await page.locator("#predictLap").textContent();

    // It follows the playhead to another lap.
    await page.locator("#scrub").evaluate((slider) => {
      slider.value = "4200";
      slider.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await expect(page.locator("#predictLap")).not.toHaveText(lap);

    // The standings are still the one static panel beside it.
    await expect(page.locator("#tower")).toBeVisible();

    await page.keyboard.press("p");
    await expect(sheet).toBeHidden();
    await expect(page.locator("#predictBtn")).toHaveAttribute("aria-pressed", "false");
  });
});

test.describe("Qualifying", () => {
  // Silverstone qualifying, built locally: Q1, Q2 and Q3 on the clock.
  const QUALI = 11322;

  async function openAt(page, seconds) {
    await page.goto(`/?session=${QUALI}`);
    await expect(page.locator("#playBtn")).toBeEnabled({ timeout: 30_000 });
    await page.locator("#scrub").evaluate((slider, s) => {
      slider.value = String(s);
      slider.dispatchEvent(new Event("input", { bubbles: true }));
    }, seconds);
  }

  test("ranks Q1 by best lap with the drop zone marked", async ({ page }) => {
    await openAt(page, 900);
    await expect(page.locator("#towerLap")).toContainText("Q1");
    await expect(page.locator("#towerLap")).toContainText("left");
    // Six cars go home from Q1; they are the ones marked in danger.
    await expect(page.locator(".tower-row.danger")).toHaveCount(6);
    await expect(page.locator(".tower-row").first().locator(".val .v")).toHaveText(/^\d:\d{2}\.\d{3}$/);
    // Prediction is about races; it is not offered here.
    await expect(page.locator("#predictBtn")).toBeHidden();
  });

  test("ends Q3 with the knocked-out cars classified below", async ({ page }) => {
    await openAt(page, 4150);
    await expect(page.locator("#towerLap")).toContainText("Q3");
    await expect(page.locator(".tower-row:not(.out)")).toHaveCount(10);
    await expect(page.locator(".tower-row.out")).toHaveCount(12);
    await expect(page.locator(".tower-row.danger")).toHaveCount(0);
  });
});
