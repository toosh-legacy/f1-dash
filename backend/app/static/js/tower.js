/* The timing tower down the right of the page.
 *
 * One row per car: position, team colour, driver, tyre, and the one number
 * that matters for the kind of session it is --
 *
 *   race        the interval to the car ahead
 *   qualifying  the gap to the car ahead, and the gap to the elimination line
 *   practice    the car's best lap
 *
 * Everything is read from the bundle's per-lap rows, which carry position,
 * gap to the leader, lap time and tyre for every car on every lap.
 */

import { el } from "./core.js";
import { player, selectCar } from "./map.js";

const tower = { mode: "race", totalLaps: 0 };

/** How many cars each qualifying phase sends home, for the elimination gap.
 *  With 22 cars in 2026, Q1 drops six and Q2 drops six more. */
const DROPPED_PER_PHASE = 6;

function modeOf(session) {
  const kind = String((session && (session.type || session.name)) || "").toLowerCase();
  if (kind.includes("qualifying") || kind.includes("shootout")) return "qualifying";
  if (kind.includes("practice")) return "practice";
  return "race";
}

function setTowerSession(session, totalLaps) {
  tower.mode = modeOf(session);
  tower.totalLaps = totalLaps || 0;
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

/** Each row's position and the value column, for the session's kind. */
function rowsFor(lap) {
  const laps = (player.bundle && player.bundle.laps) || {};
  const rows = laps[String(lap)] || laps["1"] || [];

  if (tower.mode === "race") {
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

  // Qualifying and practice are ranked by each car's best lap so far.
  const best = new Map();
  const latest = new Map();
  for (let n = 1; n <= lap; n += 1) {
    for (const row of laps[String(n)] || []) {
      latest.set(row.number, row);
      const t = row.lap_time_s;
      if (t != null && (!best.has(row.number) || t < best.get(row.number))) best.set(row.number, t);
    }
  }
  const ranked = [...latest.values()].sort((a, b) =>
    (best.get(a.number) ?? Infinity) - (best.get(b.number) ?? Infinity));
  const fastest = best.get(ranked[0] && ranked[0].number);

  if (tower.mode === "practice") {
    return ranked.map((row, index) => ({
      row, position: index + 1,
      value: fmtLap(best.get(row.number)),
      sub: index && fastest != null ? fmtGap(best.get(row.number) - fastest) : "",
    }));
  }

  const safe = Math.max(ranked.length - DROPPED_PER_PHASE, 1);   // last position through
  const cut = best.get(ranked[safe - 1] && ranked[safe - 1].number);
  return ranked.map((row, index) => {
    const mine = best.get(row.number);
    const ahead = index ? best.get(ranked[index - 1].number) : null;
    // Above the line: how far clear of being knocked out. Below it: how much
    // is needed to get through.
    const elim = mine == null || cut == null ? null
      : index < safe ? (best.get(ranked[safe] && ranked[safe].number) ?? Infinity) - mine
      : mine - cut;
    return {
      row, position: index + 1, danger: index >= safe,
      value: index === 0 ? fmtLap(mine) : fmtGap(mine != null && ahead != null ? mine - ahead : null),
      sub: elim == null || !Number.isFinite(elim) ? "" : (index < safe ? "−" : "+") + Math.abs(elim).toFixed(3),
    };
  });
}

function renderTower(lap) {
  if (!player.bundle) return;
  el("towerLap").textContent = tower.mode === "race"
    ? `Lap ${Math.max(lap, 1)}${tower.totalLaps ? ` / ${tower.totalLaps}` : ""}`
    : "";
  const drivers = new Map(player.bundle.drivers.map((d) => [d.number, d]));
  draw(rowsFor(lap).map((entry) => ({ ...entry, driver: drivers.get(entry.row.number) || {} })));
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
  for (const { row, position, value, sub, danger, driver } of entries) {
    const node = document.createElement("button");
    node.type = "button";
    node.className = "tower-row" + (position === 1 ? " leader" : "") + (danger ? " danger" : "")
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

export { modeOf, renderProvisional, renderTower, setTowerSession, tower };
