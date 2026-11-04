/* A replay being built, shown while it builds.
 *
 * A build is a minute or two of fetching -- most of it the position feed, one
 * car at a time -- so the page says so plainly: the circuit drawing itself
 * over and over with a car running round it, the step the build is on, and
 * how many cars' positions are in. It goes when the replay opens.
 */

import { api, el } from "./core.js";

const STEPS = ["session", "timing", "locations", "frames", "stored"];

/** Where each stage the builder reports sits on the bar. */
const AT = {
  session: 0.03, drivers: 0.06, timing: 0.1, locations: 0.12,
  track: 0.84, frames: 0.88, race_state: 0.93, stored: 0.97, published: 1,
};

/** A generic circuit, for when no race at this track has been built. */
const FALLBACK = "M18 52 C10 40 14 22 30 18 L70 14 C86 12 104 18 104 32 C104 44 92 46 82 44 L64 40 C56 38 52 46 58 52 L66 58 C72 64 64 70 54 68 L28 64 C22 63 20 57 18 52 Z";

let shown = false;

/** The circuit's shape squeezed into the card's 120 x 80 box. */
function shapeOf(track) {
  const b = track.bounds;
  const spanX = b.max_x - b.min_x, spanY = b.max_y - b.min_y;
  const k = Math.min(108 / spanX, 68 / spanY);
  const ox = (120 - spanX * k) / 2, oy = (80 + spanY * k) / 2;
  const step = Math.max(1, Math.floor(track.path.length / 160));
  const points = track.path.filter((_, i) => i % step === 0);
  return points.map(([x, y], i) =>
    `${i ? "L" : "M"}${(ox + (x - b.min_x) * k).toFixed(1)} ${(oy - (y - b.min_y) * k).toFixed(1)}`).join(" ") + " Z";
}

async function showBuilding(round) {
  shown = true;
  const box = el("building");
  el("buildTitle").textContent = `Building ${round.circuit} · ${round.name || "Race"}`;
  setShape(FALLBACK);
  progress({ stages: [], detail: null });
  box.hidden = false;
  requestAnimationFrame(() => { if (shown) box.classList.add("in"); });
  document.body.classList.add("building");
  try {
    const layout = await api(`/layouts/${encodeURIComponent(round.circuit)}`);
    if (shown) setShape(shapeOf(layout.track));
  } catch (_) { /* never built here: the generic loop stays */ }
}

function setShape(d) {
  el("buildGhost").setAttribute("d", d);
  el("buildTrace").setAttribute("d", d);
  el("buildMotion").setAttribute("path", d);
}

/** Update from a job record: { stages, detail }. */
function progress(record) {
  const stages = record.stages || [];
  const stage = stages[stages.length - 1] || "session";
  let at = AT[stage] ?? 0.02;
  const detail = record.detail || {};
  let words = "asking OpenF1…";
  if (stage === "locations" && detail.of) {
    at = AT.locations + (AT.track - AT.locations) * ((detail.done + 1) / detail.of);
    words = `car ${detail.done + 1} of ${detail.of} · #${detail.car}`;
  } else if (stage === "timing" && detail.drivers) {
    words = `${detail.drivers} drivers · laps, stints and pit stops`;
  } else if (stage === "track") {
    words = "tracing the circuit from the cars' positions";
  } else if (stage === "frames" || stage === "race_state") {
    words = "laying out the race second by second";
  } else if (stage === "stored" || stage === "published") {
    words = "saving it for next time";
  }
  el("buildBar").style.transform = `scaleX(${Math.min(Math.max(at, 0.02), 1).toFixed(3)})`;
  el("buildDetail").textContent = words;

  const reached = Math.max(...stages.map((s) => {
    const index = STEPS.indexOf(s === "drivers" ? "session" : s === "track" ? "locations"
      : s === "race_state" ? "frames" : s === "published" ? "stored" : s);
    return index;
  }), 0);
  el("buildSteps").querySelectorAll("li").forEach((li, index) => {
    li.classList.toggle("done", index < reached);
    li.classList.toggle("now", index === reached);
  });
}

function hideBuilding() {
  if (!shown) return;
  shown = false;
  const box = el("building");
  box.classList.remove("in");
  document.body.classList.remove("building");
  setTimeout(() => { if (!shown) box.hidden = true; }, 320);
}

export { hideBuilding, progress as buildProgress, showBuilding };
