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
 *
 * Under the rings, the car's timing -- last lap, best lap, tyre, gap -- read
 * from the bundle itself, so the card has something to say even when the
 * telemetry cannot be had.
 */

import { api, el } from "./core.js";
import { player, renderFrame } from "./map.js";
import { positionOf } from "./tower.js";

const windows = new Map();   // `${session}:${car}:${index}` -> Promise<rows>
const WINDOW_S = 120;

/** A window that failed is asked for again after this long, not every frame. */
const RETRY_MS = 15000;
const failures = new Map();   // window key -> { until, message }

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
    <div class="probe-stats" id="probeStats"></div>
    <div class="probe-foot" id="probeFoot">reading telemetry…</div>`;
  probe.querySelector(".probe-code").textContent = driver.code || driver.number;
  probe.querySelector(".probe-name").textContent = driver.team || "";
  const nodes = {};
  for (const g of GAUGES) {
    const gauge = probe.querySelector(`.gauge[data-key="${g.key}"]`);
    nodes[g.key] = { fill: gauge.querySelector(".fill"), read: gauge.querySelector(".read"), gauge };
  }
  current = { driver, nodes, last: null };
  statsKey = "";
  probe.hidden = false;
  probe.classList.remove("in");
  requestAnimationFrame(() => probe.classList.add("in"));
}

function closeProbe() {
  current = null;
  const probe = el("probe");
  probe.classList.remove("in");
  // Shrink back into the car rather than blink out; a card opened again in
  // the meantime cancels the hide.
  setTimeout(() => { if (!current) probe.hidden = true; }, 200);
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
  // Whatever the standings say -- a race lap, or a best lap against the clock.
  const place = positionOf(number);
  if (pos) pos.textContent = place ? `P${place}` : "";

  renderStats(number, time);

  if (sample === "loading") { if (foot) foot.textContent = "reading telemetry…"; return; }
  if (sample && sample.failed) {
    if (foot) foot.textContent = sample.failed;
    for (const g of GAUGES) setGauge(g, null);
    return;
  }
  if (!sample) {
    if (foot) foot.textContent = time < 0 ? "" : "car not on track at this moment";
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
  const failed = failures.get(key);
  if (failed && Date.now() < failed.until) return { failed: failed.message };
  if (!windows.has(key)) {
    const pending = api(`/replays/${sessionKey}/telemetry/${number}?t=${index * WINDOW_S}`)
      .then((answer) => {
        pending.rows = answer.rows;
        // While paused there is no next frame to pick the numbers up, so the
        // card is filled the moment they land.
        if (current && current.driver.number === number && !player.playing) renderFrame(player.time);
        return answer.rows;
      })
      .catch((error) => {
        // Held off for a while rather than asked again on the next frame,
        // and the card says why instead of showing dashes.
        windows.delete(key);
        failures.set(key, { until: Date.now() + RETRY_MS, message: reasonOf(error) });
        if (current && current.driver.number === number && !player.playing) renderFrame(player.time);
        return [];
      });
    windows.set(key, pending);
  }
  return windows.get(key);
}

/** The car's state at `time`, or "loading" while its window is on the way. */
function sampleAt(sessionKey, number, time) {
  if (time < 0) return null;
  const index = Math.floor(time / WINDOW_S);
  const pending = windowRows(sessionKey, number, index);
  if (pending.failed) return pending;
  // Ask for the next window before the playhead gets there.
  if (!player.live && time - index * WINDOW_S > WINDOW_S * 0.7) windowRows(sessionKey, number, index + 1);
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

/** What went wrong with a telemetry request, in words for the card. */
function reasonOf(error) {
  const text = String((error && error.message) || "");
  try {
    const body = JSON.parse(text.slice(text.indexOf("{")));
    const detail = body.detail;
    if (detail && detail.reason === "locked") return "telemetry paused: OpenF1 is locked during the live session";
    if (detail && detail.message) return `telemetry unavailable: ${detail.message}`.slice(0, 120);
  } catch (_) { /* not JSON */ }
  if (text.startsWith("404")) return "no telemetry for this session";
  return "telemetry unavailable right now";
}

/* -- timing, from the bundle --------------------------------------------- */

const fmtLap = (s) => {
  if (s == null || !Number.isFinite(s)) return null;
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(3).padStart(6, "0")}`;
};
const fmtGap = (s) => (s == null || !Number.isFinite(s) ? null : `+${s.toFixed(s < 10 ? 3 : 1)}`);

/** This car's timing at `time`: last lap, best lap, tyre, and in a race the
 *  gap and stops. Nothing here needs OpenF1; it is all in the bundle. */
function timingOf(number, time) {
  const bundle = player.bundle;
  if (!bundle) return [];
  const laps = bundle.laps || {};
  const lapNow = Math.max(player.lap || 1, 1);
  let row = null;
  let bestRace = null;
  for (let n = 1; n <= lapNow; n += 1) {
    const found = (laps[String(n)] || []).find((r) => r.number === number);
    if (!found) continue;
    row = found;
    if (found.lap_time_s != null && (bestRace == null || found.lap_time_s < bestRace)) bestRace = found.lap_time_s;
  }

  // Practice and qualifying are laps against the clock.
  const race = !bundle.lap_events || !bundle.lap_events.length;
  let last = null, best = null;
  if (race) {
    last = row ? row.lap_time_s : null;
    best = bestRace;
  } else {
    for (const [at, car, lapTime] of bundle.lap_events) {
      if (at > time) break;
      if (car !== number) continue;
      last = lapTime;
      if (best == null || lapTime < best) best = lapTime;
    }
  }

  const stats = [
    { k: "last", v: fmtLap(last) },
    { k: "best", v: fmtLap(best) },
  ];
  if (row && row.compound) {
    const compound = String(row.compound).toUpperCase();
    stats.push({ k: "tyre", v: `${compound[0]}${row.tyre_age != null ? ` · ${row.tyre_age}L` : ""}`, cls: compound });
  }
  if (race && row) {
    stats.push({ k: "gap", v: row.position === 1 ? "leader" : fmtGap(row.gap_to_leader_s) });
    if (row.stops != null) stats.push({ k: "stops", v: String(row.stops) });
  }
  return stats.filter((stat) => stat.v != null);
}

let statsKey = "";
function renderStats(number, time) {
  const box = el("probeStats");
  if (!box) return;
  const stats = timingOf(number, time);
  const key = stats.map((stat) => `${stat.k}=${stat.v}`).join("|");
  if (key === statsKey && box.childElementCount) return;   // most frames change nothing
  statsKey = key;
  box.textContent = "";
  if (!stats.length) {
    box.innerHTML = '<span class="none">no timed laps yet</span>';
    return;
  }
  for (const stat of stats) {
    const cell = document.createElement("span");
    cell.className = `stat ${stat.k}${stat.cls ? ` ${stat.cls}` : ""}`;
    cell.innerHTML = "<b></b><small></small>";
    cell.querySelector("b").textContent = stat.v;
    cell.querySelector("small").textContent = stat.k;
    box.append(cell);
  }
}

export { closeProbe, openProbe, placeProbe };
