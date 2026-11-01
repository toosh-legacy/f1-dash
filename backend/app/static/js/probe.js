/* The probe: a small glass card held over the selected car, reading out what
 * the car is doing at the playhead.
 *
 * Telemetry is not in the replay bundle -- four samples a second for every car
 * would be most of a megabyte per minute of race -- so the probe asks the API
 * for the selected car, two minutes at a time, and reads between samples.
 *
 * What the public feed carries is speed, gear, rpm, and throttle and brake.
 * Brake is reported as on or off, not as a pressure, and the feed has nothing
 * on the battery; acceleration is derived here from the change in speed.
 */

import { api, el } from "./core.js";
import { player, renderFrame } from "./map.js";

const windows = new Map();   // `${session}:${car}:${index}` -> Promise<rows>
const WINDOW_S = 120;

/** Gauges, in the order they sit on the card. */
const GAUGES = [
  { key: "speed",    label: "km/h",     max: 360 },
  { key: "gear",     label: "gear",     max: 8 },
  { key: "throttle", label: "throttle", max: 100, unit: "%" },
  { key: "brake",    label: "brake",    max: 100 },
  { key: "accel",    label: "g long.",  max: 5, signed: true },
];

const R = 21;
const CIRC = 2 * Math.PI * R;

let current = null;   // { driver, nodes }

function openProbe(driver) {
  const probe = el("probe");
  if (!driver) { closeProbe(); return; }
  probe.style.setProperty("--team", driver.colour || "#888");
  probe.innerHTML = `
    <div class="probe-head">
      <span class="probe-team"></span>
      <b class="probe-code"></b>
      <span class="probe-name"></span>
      <span class="probe-pos" id="probePos"></span>
    </div>
    <div class="probe-gauges">
      ${GAUGES.map((g) => `
        <div class="gauge ${g.key}" data-key="${g.key}">
          <svg viewBox="0 0 54 54" aria-hidden="true">
            <circle class="track" cx="27" cy="27" r="${R}" />
            <circle class="fill" cx="27" cy="27" r="${R}"
              stroke-dasharray="${CIRC.toFixed(2)}" stroke-dashoffset="${CIRC.toFixed(2)}" />
          </svg>
          <span class="read">–</span>
          <span class="lbl">${g.label}</span>
        </div>`).join("")}
    </div>
    <div class="probe-foot" id="probeFoot">reading telemetry…</div>`;
  probe.querySelector(".probe-code").textContent = driver.code || driver.number;
  probe.querySelector(".probe-name").textContent = driver.team || "";
  const nodes = {};
  for (const g of GAUGES) {
    const gauge = probe.querySelector(`.gauge[data-key="${g.key}"]`);
    nodes[g.key] = { fill: gauge.querySelector(".fill"), read: gauge.querySelector(".read"), gauge };
  }
  current = { driver, nodes, last: null };
  probe.hidden = false;
  probe.classList.remove("in");
  requestAnimationFrame(() => probe.classList.add("in"));
}

function closeProbe() {
  current = null;
  const probe = el("probe");
  probe.hidden = true;
  probe.classList.remove("in");
}

/** Put the card over the car and fill it for this moment of the race. */
function placeProbe(node, time) {
  const probe = el("probe");
  if (!current || !node) { probe.style.visibility = "hidden"; return; }
  probe.style.visibility = "";

  // Above the car, unless that would push it off the top of the page.
  const stage = el("stage");
  const width = probe.offsetWidth, height = probe.offsetHeight;
  const gap = player.carRadius + 12;
  let left = node.x - width / 2;
  let top = node.y - gap - height;
  const below = top < 64;
  if (below) top = node.y + gap;
  left = Math.max(10, Math.min(left, stage.clientWidth - width - 10));
  probe.classList.toggle("below", below);
  probe.style.transform = `translate(${left.toFixed(1)}px, ${top.toFixed(1)}px)`;
  probe.style.setProperty("--tip", `${(node.x - left).toFixed(1)}px`);

  fill(time);
}

function fill(time) {
  const sessionKey = player.sessionKey;
  const number = current.driver.number;
  const sample = sampleAt(sessionKey, number, time);
  const foot = el("probeFoot");
  const pos = el("probePos");
  const rows = player.bundle && player.bundle.laps && player.bundle.laps[String(player.lap || 1)];
  const mine = rows && rows.find((r) => r.number === number);
  if (pos) pos.textContent = mine && mine.position ? `P${mine.position}` : "";

  if (sample === "loading") { if (foot) foot.textContent = "reading telemetry…"; return; }
  if (!sample) {
    if (foot) foot.textContent = "no telemetry at this moment";
    for (const g of GAUGES) setGauge(g, null);
    return;
  }
  if (foot) foot.textContent = "";
  for (const g of GAUGES) setGauge(g, sample[g.key]);
}

function setGauge(g, value) {
  const node = current && current.nodes[g.key];
  if (!node) return;
  if (value == null || !Number.isFinite(value)) {
    node.read.textContent = "–";
    node.fill.setAttribute("stroke-dashoffset", CIRC.toFixed(2));
    return;
  }
  const share = Math.min(Math.abs(value) / g.max, 1);
  node.fill.setAttribute("stroke-dashoffset", (CIRC * (1 - share)).toFixed(2));
  node.gauge.classList.toggle("neg", g.signed && value < 0);
  node.read.textContent = g.key === "gear" ? (value <= 0 ? "N" : String(Math.round(value)))
    : g.key === "brake" ? (value > 0 ? "ON" : "OFF")   // the feed has no pressure, only on/off
    : g.key === "accel" ? `${value >= 0 ? "+" : "−"}${Math.abs(value).toFixed(1)}`
    : String(Math.round(value));
}

/* -- fetching and reading between samples ------------------------------- */

function windowRows(sessionKey, number, index) {
  const key = `${sessionKey}:${number}:${index}`;
  if (!windows.has(key)) {
    const pending = api(`/replays/${sessionKey}/telemetry/${number}?t=${index * WINDOW_S}`)
      .then((answer) => {
        pending.rows = answer.rows;
        // While paused there is no next frame to pick the numbers up, so the
        // card is filled the moment they land.
        if (current && current.driver.number === number && !player.playing) renderFrame(player.time);
        return answer.rows;
      })
      .catch(() => { windows.delete(key); return []; });
    windows.set(key, pending);
  }
  return windows.get(key);
}

/** The car's state at `time`, or "loading" while its window is on the way. */
function sampleAt(sessionKey, number, time) {
  if (time < 0) return null;
  const index = Math.floor(time / WINDOW_S);
  const pending = windowRows(sessionKey, number, index);
  // Ask for the next window before the playhead gets there.
  if (time - index * WINDOW_S > WINDOW_S * 0.7) windowRows(sessionKey, number, index + 1);
  const rows = pending.rows;
  if (!rows) return "loading";
  if (!rows.length) return null;

  const at = lowerBound(rows, time);
  const a = rows[Math.max(at - 1, 0)], b = rows[Math.min(at, rows.length - 1)];
  if (Math.abs(a[0] - time) > 3 && Math.abs(b[0] - time) > 3) return null;   // a gap in the feed
  const span = b[0] - a[0];
  const k = span > 0 ? Math.min(Math.max((time - a[0]) / span, 0), 1) : 0;
  const lerp = (i) => a[i] + (b[i] - a[i]) * k;
  const nearest = k < 0.5 ? a : b;

  return {
    speed: lerp(1),
    gear: nearest[2],
    throttle: lerp(3),
    brake: nearest[4],
    accel: accelAt(rows, at, time),
  };
}

/** Longitudinal acceleration in g, from the speed half a second either side.
 *  The feed's speed is whole km/h at about four samples a second, so a
 *  shorter baseline would be mostly rounding noise. */
function accelAt(rows, at, time) {
  const speedAt = (t) => {
    const i = Math.min(Math.max(lowerBound(rows, t), 1), rows.length - 1);
    const a = rows[i - 1], b = rows[i];
    const k = b[0] > a[0] ? Math.min(Math.max((t - a[0]) / (b[0] - a[0]), 0), 1) : 0;
    return a[1] + (b[1] - a[1]) * k;
  };
  if (rows.length < 3) return null;
  const dv = (speedAt(time + 0.5) - speedAt(time - 0.5)) / 3.6;   // m/s over one second
  return dv / 9.81;
}

function lowerBound(rows, time) {
  let lo = 0, hi = rows.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (rows[mid][0] < time) lo = mid + 1; else hi = mid;
  }
  return lo;
}

export { closeProbe, openProbe, placeProbe };
