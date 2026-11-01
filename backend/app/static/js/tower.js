/* The timing tower down the left of the page -- the page's one fixed panel.
 *
 * One row per car: position, team colour, driver, tyre, and the one number
 * that matters for the kind of session it is --
 *
 *   race        the interval to the car ahead, read lap by lap
 *   qualifying  the gap to the car ahead and to the elimination line, read by
 *               the clock within the current part (Q1, Q2, Q3)
 *   practice    each car's best lap so far, read by the clock
 *
 * A race is the order at the end of each lap, so it comes from the bundle's
 * per-lap rows. Practice and qualifying are best laps against the clock, so
 * they come from the bundle's timed lap events and the parts of the session
 * race control marked out (see backend app/data/replay/timing.py).
 */

import { clock, el } from "./core.js";
import { player, selectCar } from "./map.js";

const tower = { mode: "race", totalLaps: 0, clockKey: "" };

/** How many cars each qualifying part sends home. With 22 cars in 2026, Q1
 *  drops six and Q2 drops six more; Q3 decides the top ten. */
const DROPPED_PER_PHASE = 6;

/** A lap started before the chequered flag still counts when it is finished
 *  after it; this is how long after the flag those laps keep arriving. */
const IN_LAP_GRACE_S = 150;

function modeOf(session) {
  const kind = String((session && (session.type || session.name)) || "").toLowerCase();
  if (kind.includes("qualifying") || kind.includes("shootout")) return "qualifying";
  if (kind.includes("practice")) return "practice";
  return "race";
}

function setTowerSession(session, totalLaps) {
  tower.mode = modeOf(session);
  tower.totalLaps = totalLaps || 0;
  tower.clockKey = "";
  el("towerKind").textContent = session.name || "Session";
  el("towerCol").innerHTML = tower.mode === "race" ? "Interval"
    : tower.mode === "qualifying" ? "Gap<span>elim</span>" : "Best lap";
  el("tower").dataset.mode = tower.mode;
}

const fmtGap = (s) => (s == null || !Number.isFinite(s) ? "–" : `+${s.toFixed(s < 10 ? 3 : 1)}`);
const fmtLap = (s) => {
  if (s == null || !Number.isFinite(s)) return "–";
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(3).padStart(6, "0")}`;
};

function surnameOf(driver) {
  const parts = String(driver.name || "").trim().split(/\s+/);
  const last = parts[parts.length - 1] || "";
  return last.charAt(0) + last.slice(1).toLowerCase();
}

/* -- a race, lap by lap -------------------------------------------------- */

function raceRows(lap) {
  const laps = (player.bundle && player.bundle.laps) || {};
  const rows = laps[String(lap)] || laps["1"] || [];
  const ordered = [...rows].sort((a, b) => (a.position ?? 99) - (b.position ?? 99));
  return ordered.map((row, index) => {
    const ahead = ordered[index - 1];
    // The order and the gaps are sampled at slightly different moments, so
    // around a pit stop a car can be listed behind one it is timed ahead
    // of. A negative interval is that seam, not a fact; it is not shown.
    const raw = index === 0 ? null
      : row.gap_to_leader_s != null && ahead && ahead.gap_to_leader_s != null
        ? row.gap_to_leader_s - ahead.gap_to_leader_s : null;
    const interval = raw != null && raw >= 0 ? raw : null;
    return {
      row, position: row.position ?? index + 1,
      value: index === 0 ? "Leader" : fmtGap(interval),
    };
  });
}

/* -- practice and qualifying, by the clock ------------------------------- */

/** Each car's best lap completed between `from` and `to`. */
function bestBetween(events, from, to) {
  const best = new Map();
  for (const [at, car, time] of events) {
    if (at < from) continue;
    if (at > to) break;
    if (!best.has(car) || time < best.get(car)) best.set(car, time);
  }
  return best;
}

/** Cars in `field` ordered by best time, those without one after. */
function rank(field, best) {
  return [...field].sort((a, b) => (best.get(a) ?? Infinity) - (best.get(b) ?? Infinity));
}

/** Which part of the session `time` falls in, and how the field stands.
 *
 *  The field for each part is the cars that got through the one before: a
 *  qualifying part ends by sending the slowest home, and only the rest go on.
 *  Cars knocked out earlier are kept at the bottom, in the order they went.
 */
function clockState(time) {
  const bundle = player.bundle;
  const events = bundle.lap_events || [];
  const segments = bundle.segments && bundle.segments.length
    ? bundle.segments : [{ name: bundle.session.name, start: 0, end: bundle.duration_s }];
  let index = 0;
  segments.forEach((segment, i) => { if (segment.start <= time) index = i; });

  let field = bundle.drivers.map((d) => d.number);
  const out = [];   // knocked out, earliest first
  for (let i = 0; i < index; i += 1) {
    const segment = segments[i];
    const best = bestBetween(events, segment.start, (segment.end ?? Infinity) + IN_LAP_GRACE_S);
    const ranked = rank(field, best);
    if (tower.mode === "qualifying") {
      const through = Math.max(ranked.length - DROPPED_PER_PHASE, 1);
      out.unshift(...ranked.slice(through).map((car) => ({ car, time: best.get(car), part: segment.name })));
      field = ranked.slice(0, through);
    }
  }

  const segment = segments[index];
  const until = Math.min(time, (segment.end ?? Infinity) + IN_LAP_GRACE_S);
  const best = tower.mode === "practice"
    ? bestBetween(events, segments[0].start, until)   // practice: the whole session so far
    : bestBetween(events, segment.start, until);
  const eliminating = tower.mode === "qualifying" && index < segments.length - 1;
  return { segment, index, segments, field: rank(field, best), best, out, eliminating };
}

function clockRows(time) {
  const state = clockState(time);
  const { best, field, eliminating } = state;
  const fastest = best.get(field[0]);
  const safe = eliminating ? Math.max(field.length - DROPPED_PER_PHASE, 1) : field.length;
  const tyres = latestTyres();

  const rows = field.map((car, index) => {
    const mine = best.get(car);
    const ahead = index ? best.get(field[index - 1]) : null;
    let sub = "";
    if (tower.mode === "practice") {
      sub = index && mine != null && fastest != null ? fmtGap(mine - fastest) : "";
    } else if (eliminating && mine != null) {
      // Above the line: how far clear of the first car out. Below it: how
      // much is needed to beat the last car through.
      const firstOut = best.get(field[safe]);
      const lastIn = best.get(field[safe - 1]);
      const margin = index < safe ? (firstOut ?? Infinity) - mine : mine - lastIn;
      if (Number.isFinite(margin)) sub = `${index < safe ? "−" : "+"}${Math.abs(margin).toFixed(3)}`;
    }
    return {
      row: { number: car, ...(tyres.get(car) || {}) },
      position: index + 1,
      danger: eliminating && index >= safe,
      value: tower.mode === "practice" || index === 0 ? fmtLap(mine)
        : fmtGap(mine != null && ahead != null ? mine - ahead : null),
      sub,
    };
  });

  // Knocked out in an earlier part: still classified, below the line.
  let position = field.length;
  for (const { car, time: lap, part } of state.out) {
    position += 1;
    rows.push({
      row: { number: car, ...(tyres.get(car) || {}) },
      position, out: true, value: fmtLap(lap), sub: `out ${part}`,
    });
  }
  return { rows, state };
}

/** Each car's tyre as of the lap under the playhead. */
function latestTyres() {
  const laps = (player.bundle && player.bundle.laps) || {};
  const tyres = new Map();
  for (let n = 1; n <= Math.max(player.lap || 1, 1); n += 1) {
    for (const row of laps[String(n)] || []) {
      tyres.set(row.number, { compound: row.compound, tyre_age: row.tyre_age });
    }
  }
  return tyres;
}

function clockLabel(state, time) {
  const { segment } = state;
  if (segment.end == null) return segment.name;
  const left = segment.end - time;
  if (left > 0) return `${segment.name} · ${clock(left)} left`;
  const next = state.segments[state.index + 1];
  return next ? `${segment.name} · finished` : `${segment.name} · chequered`;
}

/* -- drawing --------------------------------------------------------------- */

/** Redraw for a race lap; practice and qualifying redraw from the clock. */
function renderTower(lap) {
  if (!player.bundle) return;
  if (tower.mode !== "race" && player.bundle.lap_events) { tickTower(player.time, true); return; }
  el("towerLap").textContent = tower.mode === "race"
    ? `Lap ${Math.max(lap, 1)}${tower.totalLaps ? ` / ${tower.totalLaps}` : ""}`
    : "";
  const drivers = new Map(player.bundle.drivers.map((d) => [d.number, d]));
  draw(raceRows(lap).map((entry) => ({ ...entry, driver: drivers.get(entry.row.number) || {} })));
}

/** Practice and qualifying, every frame: cheap unless a lap has been
 *  completed or a part of the session has changed since the last draw. */
function tickTower(time, force = false) {
  const bundle = player.bundle;
  if (!bundle || tower.mode === "race" || !bundle.lap_events) return;
  const events = bundle.lap_events;
  let done = 0;
  while (done < events.length && events[done][0] <= time) done += 1;
  const { rows, state } = clockRows(time);
  el("towerLap").textContent = clockLabel(state, time);
  el("towerCol").innerHTML = tower.mode === "practice" ? "Best lap"
    : state.eliminating ? "Gap<span>elim</span>" : "Gap";
  const key = `${done}:${state.index}`;
  if (!force && key === tower.clockKey) return;
  tower.clockKey = key;
  const drivers = new Map(bundle.drivers.map((d) => [d.number, d]));
  draw(rows.map((entry) => ({ ...entry, driver: drivers.get(entry.row.number) || {} })));
}

/** Where a car stands right now, whatever the session. */
function positionOf(number) {
  const row = document.querySelector(`.tower-row[data-number="${number}"] .pos`);
  return row ? row.textContent : "";
}


/** Before a session has run: the last race's classification, labelled as such. */
function renderProvisional(bundle) {
  el("towerLap").textContent = "Provisional";
  el("towerCol").textContent = "";
  if (!bundle) {
    el("towerRows").innerHTML = '<div class="tower-empty">No finished race is built yet, so there is no order to show.</div>';
    return;
  }
  const drivers = new Map(bundle.drivers.map((d) => [d.number, d]));
  draw(bundle.results.map((row) => ({
    row: { ...row, compound: null }, position: row.position,
    driver: drivers.get(row.number) || row, value: "",
  })), `Order from ${bundle.session.circuit}. The grid is set in qualifying.`);
}

function draw(entries, note) {
  const container = el("towerRows");
  if (!entries.length) {
    container.innerHTML = '<div class="tower-empty">No timing on this lap.</div>';
    return;
  }
  const fragment = document.createDocumentFragment();
  if (note) {
    const foot = document.createElement("div");
    foot.className = "tower-note";
    foot.textContent = note;
    fragment.append(foot);
  }
  for (const { row, position, value, sub, danger, out, driver } of entries) {
    const node = document.createElement("button");
    node.type = "button";
    node.className = "tower-row" + (position === 1 ? " leader" : "") + (danger ? " danger" : "") + (out ? " out" : "")
      + (String(player.selected) === String(row.number) ? " selected" : "");
    node.dataset.number = String(row.number);
    node.style.setProperty("--team", driver.colour || "#777");
    const compound = row.compound ? String(row.compound).toUpperCase() : "";
    node.innerHTML = `
      <span class="pos">${position ?? "–"}</span>
      <span class="team"></span>
      <span class="who"><b></b><small></small></span>
      <span class="tyre ${compound}" title="${compound.toLowerCase()}${row.tyre_age != null ? `, ${row.tyre_age} laps` : ""}">
        ${compound ? `<i>${compound[0]}</i><em>${row.tyre_age ?? ""}</em>` : ""}
      </span>
      <span class="val"><span class="v"></span>${sub ? '<small class="s"></small>' : ""}</span>`;
    node.querySelector(".who b").textContent = driver.code || row.number;
    node.querySelector(".who small").textContent = surnameOf(driver);
    node.querySelector(".val .v").textContent = value;
    if (sub) node.querySelector(".val .s").textContent = sub;
    node.addEventListener("click", (event) => { event.stopPropagation(); selectCar(row.number); });
    fragment.append(node);
  }
  const before = measure(container);
  container.replaceChildren(fragment);
  slide(container, before);
}

/* -- rows sliding into their new places ---------------------------------
   When the order changes, every row is redrawn in its new place and then
   played back from where it was -- the FLIP technique -- so a pass reads as
   two rows trading places rather than as the list blinking. A car that
   gained places glows green as it settles, one that lost them red. */

function measure(container) {
  const rects = new Map();
  for (const row of container.querySelectorAll(".tower-row")) {
    rects.set(row.dataset.number, { top: row.offsetTop, pos: Number(row.querySelector(".pos").textContent) });
  }
  return rects;
}

function slide(container, before) {
  if (!before.size || reducedMotion()) return;
  for (const row of container.querySelectorAll(".tower-row")) {
    const was = before.get(row.dataset.number);
    if (!was) continue;
    const dy = was.top - row.offsetTop;
    if (Math.abs(dy) < 1) continue;
    row.animate(
      [{ transform: `translateY(${dy}px)` }, { transform: "translateY(0)" }],
      { duration: Math.min(380 + Math.abs(dy) * 0.6, 700), easing: "cubic-bezier(.34,1.3,.5,1)" },
    );
    const now = Number(row.querySelector(".pos").textContent);
    if (Number.isFinite(now) && Number.isFinite(was.pos) && now !== was.pos) {
      row.classList.add(now < was.pos ? "gained" : "lost");
      setTimeout(() => row.classList.remove("gained", "lost"), 1600);
    }
  }
}

const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export { modeOf, positionOf, renderProvisional, renderTower, setTowerSession, tickTower, tower };
