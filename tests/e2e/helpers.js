"use strict";

const LAYOUT_KEY = "f1dash.layout.v4";

/** Open the dashboard on a clean layout and wait for it to finish booting.
 *
 * The boot overlay covers the page until the API answers, so every test would
 * otherwise begin by racing it. Waiting on a panel rather than on a timeout
 * means the wait is as long as the machine needs and no longer.
 */
async function open(page, { layout = null } = {}) {
  await page.addInitScript(
    ([key, value]) => {
      // Runs before the page's own script, so the dashboard reads whatever
      // layout a test asked for rather than one left by the test before it.
      //
      // Once only. Playwright re-runs init scripts on every navigation, and a
      // test that reloads to prove the layout was remembered would otherwise
      // have it wiped on the way back in -- and pass for the wrong reason.
      // sessionStorage survives a reload but not a new page, which is exactly
      // the lifetime wanted.
      const GUARD = "f1dash.test.seeded";
      if (window.sessionStorage.getItem(GUARD)) return;
      window.sessionStorage.setItem(GUARD, "1");
      if (value === null) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, value);
    },
    [LAYOUT_KEY, layout]
  );
  await page.goto("/");
  await page.locator('.widget[data-panel="map"]').waitFor({ state: "visible" });
  await page.waitForFunction(() => !document.getElementById("boot") ||
    getComputedStyle(document.getElementById("boot")).opacity === "0" ||
    document.getElementById("boot").hidden);
}

/** A panel's position and size in grid cells, read back off the DOM. */
async function cellBox(page, id) {
  return page.evaluate((panelId) => {
    const board = document.getElementById("board");
    const widget = document.querySelector(`.widget[data-panel="${panelId}"]`);
    if (!widget) return null;
    const b = board.getBoundingClientRect();
    const w = widget.getBoundingClientRect();
    const cw = b.width / 24;
    const ch = b.height / 12;
    return {
      c: Math.round((w.left - b.left) / cw),
      r: Math.round((w.top - b.top) / ch),
      cw: Math.round(w.width / cw),
      ch: Math.round(w.height / ch),
    };
  }, id);
}

/** Drag a panel by its header, which is the handle the viewer actually uses. */
async function dragPanel(page, id, dx, dy) {
  const head = page.locator(`.widget[data-panel="${id}"] .w-head`);
  const box = await head.boundingBox();
  const from = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  // Several small steps: the board tracks pointermove, and one jump can be
  // read as a click rather than a drag.
  for (let step = 1; step <= 10; step += 1) {
    await page.mouse.move(from.x + (dx * step) / 10, from.y + (dy * step) / 10);
  }
  await page.mouse.up();
  // The board settles the panel into its cell and only then writes the
  // layout, so this waits for the write, not just the animation.
  await page.waitForTimeout(600);
}

module.exports = { LAYOUT_KEY, open, cellBox, dragPanel };
