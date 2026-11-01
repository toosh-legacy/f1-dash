/* The circuit, the cars on it, and playing a race back over both.
 *
 * The circuit is the whole page now: it is drawn as large as the space beside
 * the timing tower allows, over a map of its real surroundings -- grass,
 * gravel, stands, buildings, the other tarmac on site -- taken from
 * OpenStreetMap and laid into the feed's frame offline (see
 * backend/scripts/build_circuit_maps.py). A circuit without one falls back to
 * surroundings derived from the racing line itself.
 *
 * Drawing is two jobs kept apart: the circuit is laid down once when a race is
 * opened (and again on resize), and the cars are moved over it on every frame
 * by transform alone, so playback never rebuilds a path.
 */

import { setBackgroundAwake } from "./chrome.js";
import { api, clock, el, setStatus, svgNS, when } from "./core.js";
import { clearNotices, islandTick, notify, renderEvents, reportOvertakes, seenUpTo, worthAnnouncing } from "./notices.js";
import { predictionReady, refreshPrediction, resetPrediction } from "./predict.js";
import { clearFlags, layoutFlags, renderFlags, setFlagTimeline } from "./flags.js";
import { closeProbe, openProbe, placeProbe } from "./probe.js";
import { catalogue } from "./search.js";
import { renderProvisional, renderTower, setTowerSession, tickTower } from "./tower.js";

const player = {
  bundle: null, sessionKey: null, path: [], pitPath: [], bounds: null,
  rawPath: [], rawPit: [],
  playing: false, time: 0, speed: 10, lastFrameAt: 0, lap: 0, selected: null,
  upcoming: null, framesReady: false, flag: "green",
  /** Drawn sizes, in screen pixels, settled by fitMap. */
  trackWidth: 18, carRadius: 13,
};

/* -- fitting the circuit to the page ------------------------------------ */

function fitTransform(bounds, box) {
  const spanX = Math.max(bounds.max_x - bounds.min_x, 1);
  const spanY = Math.max(bounds.max_y - bounds.min_y, 1);
  const scale = Math.min(box.width / spanX, box.height / spanY);
  const left = box.left + (box.width - spanX * scale) / 2;
  const bottom = box.top + (box.height + spanY * scale) / 2;   // y grows up in the feed
  const project = (x, y) => [
    left + (x - bounds.min_x) * scale,
    bottom - (y - bounds.min_y) * scale,
  ];
  project.scale = scale;   // screen pixels per feed unit (a decimetre)
  return project;
}

function toPathData(points, close) {
  if (!points.length) return "";
  return points.map(([x, y], index) => `${index ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ")
    + (close ? " Z" : "");
}

/** The part of the stage the circuit may use: everything inside the glass that
 *  the top bar, the timing tower and the transport do not cover. Measured off
 *  the real elements, so moving any of them in CSS moves the circuit too. */
function drawableBox(width, height) {
  const stage = el("stage").getBoundingClientRect();
  const rel = (node) => {
    const r = node && !node.hidden ? node.getBoundingClientRect() : null;
    return r && r.width ? { left: r.left - stage.left, right: r.right - stage.left,
      top: r.top - stage.top, bottom: r.bottom - stage.top } : null;
  };
  const top = rel(el("topbar")), tower = rel(el("tower")), foot = rel(el("transport"));
  const wide = width > 900;
  const left = wide && tower ? tower.right : 0;   // the standings hold the left edge
  const right = width;
  const upper = top ? top.bottom : 0;
  const lower = foot ? foot.top : height;
  return { left, top: upper, width: Math.max(right - left, 120), height: Math.max(lower - upper, 120) };
}

/** Redraw the circuit into whatever space the page currently has. */
function fitMap() {
  const stage = el("stage");
  if (!stage || !player.bounds) return;
  const width = Math.max(stage.clientWidth, 200);
  const height = Math.max(stage.clientHeight, 200);
  el("map").setAttribute("viewBox", `0 0 ${width} ${height}`);
  el("mapBase").setAttribute("viewBox", `0 0 ${width} ${height}`);

  const area = drawableBox(width, height);
  // Big, with a small margin: the circuit is the only thing on the page.
  const trackWidth = Math.round(Math.max(12, Math.min(24, Math.min(area.width, area.height) * 0.024)));
  const carRadius = Math.round(trackWidth * 0.78);
  const pad = carRadius + 18;
  const box = {
    left: area.left + pad, top: area.top + pad,
    width: area.width - pad * 2, height: area.height - pad * 2,
  };
  player.trackWidth = trackWidth;
  player.carRadius = carRadius;
  stage.style.setProperty("--track-w", `${trackWidth}px`);
  stage.style.setProperty("--car-r", `${carRadius}px`);

  const project = fitTransform(player.bounds, box);
  player.path = player.rawPath.map(([x, y]) => project(x, y));
  player.pitPath = player.rawPit.map(([x, y]) => project(x, y));

  const loop = toPathData(player.path, true);
  const pit = toPathData(player.pitPath, false);
  const set = (id, d, stroke) => {
    const node = el(id);
    node.setAttribute("d", d);
    if (stroke != null) node.setAttribute("stroke-width", stroke);
  };
  // The surroundings, widest first. Each is the same racing line at a
  // different width, which is what a simplified map of a circuit mostly is.
  set("infield", loop);
  set("serviceRoad", loop, trackWidth * 7.2);
  set("serviceRoadCut", loop, trackWidth * 7.2 - 3);
  set("grass", loop, trackWidth * 5.2);
  set("runoff", loop, trackWidth * 2.4);
  set("pitEdge", pit, trackWidth * 0.62 + 3);
  set("pitSurface", pit, trackWidth * 0.62);
  set("pitMark", pit, Math.max(1, trackWidth * 0.06));
  set("trackEdge", loop, trackWidth + 4);
  set("trackSurface", loop, trackWidth);
  set("trackSheen", loop, Math.max(1, trackWidth * 0.08));

  const [sx, sy] = player.path[0];
  const [nx, ny] = player.path[1] || [sx + 1, sy];
  const angle = Math.atan2(ny - sy, nx - sx) + Math.PI / 2;
  const half = trackWidth / 2 + 1;
  const line = el("startLine");
  line.setAttribute("x1", sx + Math.cos(angle) * half);
  line.setAttribute("y1", sy + Math.sin(angle) * half);
  line.setAttribute("x2", sx - Math.cos(angle) * half);
  line.setAttribute("y2", sy - Math.sin(angle) * half);
  line.setAttribute("stroke-width", Math.max(4, trackWidth * 0.36));

  for (const node of carNodes.values()) node.dot.setAttribute("r", carRadius);

  player.project = project;
  drawSurroundings();
  layoutFlags(player.path, trackWidth);
}

/* -- the real surroundings ------------------------------------------------ */

const surroundings = { circuit: null, data: null };

/** How wide, in metres, the lines on the base map are drawn. Areas are
 *  filled; these are the things OSM maps as a centre line. */
const LINE_WIDTH_M = { road: 5, tarmac: 11 };

function drawSurroundings() {
  const data = surroundings.data;
  const project = player.project;
  document.body.classList.toggle("has-osm", !!data);
  el("osmCredit").hidden = !data;
  for (const layer of ["grass", "wood", "water", "paved", "gravel", "road", "tarmac", "building", "stand"]) {
    const node = el(`osm-${layer}`);
    const shapes = data && project ? data.layers[layer] || [] : [];
    const closed = !(layer in LINE_WIDTH_M);
    node.setAttribute("d", shapes.map((shape) => toPathData(shape.map(([x, y]) => project(x, y)), closed)).join(" "));
    if (!closed && project) {
      node.setAttribute("stroke-width", Math.max(1, LINE_WIDTH_M[layer] * 10 * project.scale).toFixed(2));
    }
  }
}

/** Fetch what is around this circuit, if it has been mapped, and draw it
 *  under the track. A circuit with no map keeps the derived surroundings. */
async function loadSurroundings(circuit) {
  if (circuit === surroundings.circuit) { drawSurroundings(); return; }
  surroundings.circuit = circuit;
  surroundings.data = null;
  drawSurroundings();
  if (!circuit) return;
  // A circuit raced but never mapped is mapped on the spot; the API says so
  // ("mapping") and the map is asked for again until it lands.
  for (let attempt = 0; attempt < MAP_ATTEMPTS; attempt += 1) {
    let data;
    try {
      data = await api(`/circuits/${encodeURIComponent(circuit)}/map`);
    } catch (_) { return; /* not mapped: the derived surroundings stay */ }
    if (surroundings.circuit !== circuit) return;
    if (data && data.layers) {
      surroundings.data = data;
      drawSurroundings();
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 4000));
    if (surroundings.circuit !== circuit) return;
  }
}

/** How many times to ask for a map that is being built (about two minutes). */
const MAP_ATTEMPTS = 30;

function clearTrack() {
  player.bounds = null;
  player.project = null;
  surroundings.circuit = null;
  surroundings.data = null;
  drawSurroundings();
  clearFlags();
  for (const id of ["infield", "serviceRoad", "serviceRoadCut", "grass", "runoff",
    "pitEdge", "pitSurface", "pitMark", "trackEdge", "trackSurface", "trackSheen"]) {
    el(id).setAttribute("d", "");
  }
  el("startLine").setAttribute("x1", 0); el("startLine").setAttribute("x2", 0);
}

function setTrack(track, circuit) {
  player.rawPath = track.path;
  player.rawPit = track.pit_path || [];
  player.bounds = track.bounds;
  fitMap();
  loadSurroundings(circuit);
}

let resizeQueued = false;
window.addEventListener("resize", () => {
  if (resizeQueued) return;
  resizeQueued = true;
  requestAnimationFrame(() => {
    resizeQueued = false;
    if (player.bounds) { fitMap(); renderFrame(player.time); }
  });
});

/* -- a race that has run ------------------------------------------------ */

/** Open a race that has run.
 *
 *  The metadata (circuit, drivers, per-lap order, race control) is small and
 *  is drawn as soon as it lands; the playback frames are most of the bundle
 *  and stream in behind it, so the only thing that waits is the play button. */
async function openReplay(sessionKey, meta) {
  setStatus("reading the race…");
  let bundle;
  try {
    bundle = await api(`/replays/${sessionKey}/meta`);
  } catch (error) {
    setStatus("replay not built", "error");
    return false;
  }

  stopCountdown();
  document.body.dataset.view = "replay";
  resetPrediction();
  closeProbe();
  bundle.frames = [];
  Object.assign(player, {
    bundle, sessionKey, playing: false, framesReady: false, time: 0, lap: 0,
    upcoming: null, selected: null,
  });
  clearNotices();
  rcSeen = 0;
  seenUpTo(0);

  setTrack(bundle.track, bundle.session.circuit || bundle.session.location);
  setFlagTimeline(bundle.race_control || []);
  buildCarLayer(bundle.drivers);

  const session = bundle.session;
  el("sessionTitle").textContent = `${session.circuit || session.location} · ${session.name}`;
  setTowerSession(session, bundle.total_laps);
  el("scrub").max = String(Math.round(bundle.duration_s));
  renderTower(1);
  setPlayable(false);
  setStatus("loading playback…");

  loadFrames(sessionKey, bundle);
  return true;
}

/** The playback frames, fetched behind the drawn circuit. */
async function loadFrames(sessionKey, bundle) {
  try {
    const frames = await api(`/replays/${sessionKey}/frames`);
    if (player.bundle !== bundle) return;   // the viewer moved on while it loaded
    bundle.frames = frames;
    player.framesReady = true;

    // Open where the race does: the feed starts well before the lights, with
    // the cars stacked on the grid and no timing to show.
    const green = frames.find(
      (frame) => frame.lap >= 1 && Object.keys(frame.cars).length > bundle.drivers.length / 2
    );
    player.time = green ? green.t : 0;
    rcSeen = player.time;
    seenUpTo(player.time);
    clearNotices();

    el("flagStrip").style.background = flagGradient(bundle);
    setPlayable(true);
    predictionReady();
    renderFrame(player.time);
    setStatus("ready", "live");
  } catch (error) {
    setStatus(`playback unavailable: ${error.message}`, "error");
  }
}

function setPlayable(ready) {
  el("playBtn").disabled = !ready;
  el("scrub").disabled = !ready;
}

/* -- a race that has not ------------------------------------------------ */

let countdownTimer = null;

function stopCountdown() {
  if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; }
  el("countdown").hidden = true;
}

/** Show an upcoming session: its circuit if any race there has ever been
 *  built, the time to lights out over the middle of it, and the order the
 *  grid is expected to line up in. */
async function openUpcoming(round) {
  stopCountdown();
  document.body.dataset.view = "next";
  resetPrediction();
  closeProbe();
  Object.assign(player, { bundle: null, upcoming: round, playing: false, selected: null });
  buildCarLayer([]);
  clearNotices();
  rcSeen = 0;
  el("sessionTitle").textContent = `${round.circuit} · ${round.name}`;
  setTowerSession({ name: round.name, type: round.name }, 0);
  setPlayable(false);
  setStatus("not yet run");

  let laid = false;
  try {
    const layoutData = await api(`/layouts/${encodeURIComponent(round.circuit)}`);
    setTrack(layoutData.track, round.circuit);
    setFlagTimeline([]);
    laid = true;
  } catch (_) {
    clearTrack();
  }

  el("cdCircuit").textContent = `${round.circuit} · ${round.name}`;
  el("cdKind").textContent = /race|sprint/i.test(round.name || "") ? "lights out in" : "starts in";
  el("countdown").classList.toggle("no-layout", !laid);
  el("cdWhen").textContent = when(round.date_start);
  el("countdown").hidden = false;
  tickCountdown(round.date_start);
  countdownTimer = setInterval(() => tickCountdown(round.date_start), 1000);

  await showProvisionalGrid(round);
}

function tickCountdown(iso) {
  const remaining = new Date(iso).getTime() - Date.now();
  if (remaining <= 0) { setDigits(el("cdTime"), "under way"); return; }
  const seconds = Math.floor(remaining / 1000);
  const days = Math.floor(seconds / 86400);
  const hours = String(Math.floor((seconds % 86400) / 3600)).padStart(2, "0");
  const minutes = String(Math.floor((seconds % 3600) / 60)).padStart(2, "0");
  const secs = String(seconds % 60).padStart(2, "0");
  setDigits(el("cdTime"), (days ? `${days}d ` : "") + `${hours}:${minutes}:${secs}`);
}

/** Write the countdown a character at a time, so only the digits that
 *  changed roll over -- the seconds every tick, the minutes once a minute --
 *  instead of the whole clock being replaced. */
function setDigits(node, text) {
  const cells = node.querySelectorAll("span");
  if (cells.length !== text.length) {
    node.textContent = "";
    for (const char of text) {
      const cell = document.createElement("span");
      cell.textContent = char;
      if (char === ":" || char === " ") cell.className = "sep";
      node.append(cell);
    }
    return;
  }
  [...text].forEach((char, index) => {
    const cell = cells[index];
    if (cell.textContent === char) return;
    cell.textContent = char;
    cell.classList.remove("roll");
    void cell.offsetWidth;   // restart the animation
    cell.classList.add("roll");
  });
}

/** The grid as it stands before a session that has not run: the last
 *  completed race's finishing order, said plainly rather than dressed up as a
 *  prediction. */
async function showProvisionalGrid(round) {
  const previous = (catalogue || [])
    .filter((entry) => entry.cached && new Date(entry.date_start) < new Date(round.date_start))
    .sort((a, b) => new Date(b.date_start) - new Date(a.date_start))[0];
  if (!previous) { renderProvisional(null); return; }
  try {
    const bundle = await api(`/replays/${previous.session_key}/meta`);
    renderProvisional(bundle);
  } catch (_) {
    renderProvisional(null);
  }
}

/* -- cars --------------------------------------------------------------- */

const carNodes = new Map();

/** Black or white, whichever reads on the team's colour. */
function inkOn(hex) {
  const value = String(hex || "#666").replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(value.slice(i, i + 2), 16) / 255);
  const lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  return lum > 0.55 ? "#0b0d10" : "#ffffff";
}

function buildCarLayer(drivers) {
  const layer = el("cars");
  layer.textContent = "";
  carNodes.clear();
  for (const driver of drivers) {
    const group = document.createElementNS(svgNS, "g");
    group.setAttribute("class", "car");
    group.setAttribute("role", "button");
    group.setAttribute("tabindex", "0");
    group.setAttribute("aria-label", `${driver.name || driver.code}, show telemetry`);
    group.dataset.number = String(driver.number);
    group.style.setProperty("--team", driver.colour || "#888");

    const halo = document.createElementNS(svgNS, "circle");
    halo.setAttribute("class", "car-halo");
    halo.setAttribute("fill", driver.colour);

    const dot = document.createElementNS(svgNS, "circle");
    dot.setAttribute("class", "car-dot");
    dot.setAttribute("fill", driver.colour);
    dot.setAttribute("r", player.carRadius);

    const label = document.createElementNS(svgNS, "text");
    label.setAttribute("class", "car-label");
    label.setAttribute("fill", inkOn(driver.colour));
    label.textContent = driver.code;

    group.append(halo, dot, label);
    group.addEventListener("click", (event) => { event.stopPropagation(); selectCar(driver.number); });
    group.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectCar(driver.number); }
    });
    layer.append(group);
    carNodes.set(String(driver.number), { group, dot, driver, x: 0, y: 0, visible: false });
  }
}

/** Pick a car to look into, or put it back. */
function selectCar(number) {
  const same = player.selected != null && String(player.selected) === String(number);
  player.selected = same ? null : number;
  for (const [key, node] of carNodes) {
    node.group.classList.toggle("selected", key === String(player.selected));
  }
  document.querySelectorAll(".tower-row").forEach((row) => {
    row.classList.toggle("selected", row.dataset.number === String(player.selected));
  });
  if (player.selected == null) closeProbe();
  else {
    const node = carNodes.get(String(player.selected));
    // Keep the chosen car on top of the pack it is in.
    if (node) node.group.parentNode.append(node.group);
    openProbe(node ? node.driver : null);
  }
  if (player.bundle) renderFrame(player.time);
}

// Clicking the open map, away from any car, puts the card away.
el("stage").addEventListener("click", (event) => {
  if (event.target.closest("#probe")) return;
  if (player.selected != null) selectCar(player.selected);
});

function pointAt(path, progress) {
  if (!path.length) return [0, 0];
  const wrapped = ((progress % path.length) + path.length) % path.length;
  const index = Math.floor(wrapped);
  const [x0, y0] = path[index];
  const [x1, y1] = path[(index + 1) % path.length];
  const alpha = wrapped - index;
  return [x0 + (x1 - x0) * alpha, y0 + (y1 - y0) * alpha];
}

function blendProgress(from, to, alpha, loop) {
  let delta = to - from;
  if (delta > loop / 2) delta -= loop;
  if (delta < -loop / 2) delta += loop;
  return from + delta * alpha;
}

function renderFrame(time) {
  const bundle = player.bundle;
  if (!bundle || !bundle.frames || !bundle.frames.length) return;
  const step = bundle.frame_interval_s || 1;
  const position = Math.min(time / step, bundle.frames.length - 1);
  const index = Math.floor(position);
  const alpha = position - index;
  const frame = bundle.frames[index];
  const next = bundle.frames[Math.min(index + 1, bundle.frames.length - 1)];
  if (!frame) return;

  const loop = player.path.length;
  for (const [number, node] of carNodes) {
    const here = frame.cars[number];
    if (here === undefined) {
      if (node.visible) { node.group.setAttribute("visibility", "hidden"); node.visible = false; }
      continue;
    }
    if (!node.visible) { node.group.setAttribute("visibility", "visible"); node.visible = true; }

    let x, y;
    const inPit = Array.isArray(here);
    node.progress = null;
    if (inPit) {
      [x, y] = pointAt(player.pitPath.length ? player.pitPath : player.path, here[1]);
    } else {
      const there = next ? next.cars[number] : undefined;
      const progress = (typeof there === "number") ? blendProgress(here, there, alpha, loop) : here;
      [x, y] = pointAt(player.path, progress);
      node.progress = progress;   // where on the lap, for the flags
    }
    node.x = x; node.y = y;
    node.group.classList.toggle("pit", inPit);
    node.group.setAttribute("transform", `translate(${x.toFixed(1)} ${y.toFixed(1)})`);
  }

  if (player.selected != null) {
    const node = carNodes.get(String(player.selected));
    placeProbe(node && node.visible ? node : null, time);
  }

  el("clock").textContent = `${clock(time)} / ${clock(bundle.duration_s)}`;
  const scrub = el("scrub");
  scrub.value = String(Math.round(time));
  scrub.style.setProperty("--played", (time / (bundle.duration_s || 1) * 100).toFixed(2));
  player.flag = frame.flag || "green";   // the island shows it
  renderFlags(time, player.flag, (number) => carNodes.get(String(number))?.progress ?? null);

  announceRaceControl(time);
  tickTower(time);
  if (bundle.events) renderEvents(time);
  if (frame.lap !== player.lap) {
    const previous = player.lap;
    player.lap = frame.lap;
    renderTower(frame.lap);
    refreshPrediction();
    // Only when the backend did not supply events: a lap completed in order
    // says something about who passed whom, a scrub jump says nothing.
    if (!bundle.events && frame.lap === previous + 1) reportOvertakes(previous, frame.lap);
  }
  islandTick();   // after the lap lands, so the resting state is current
}

/* -- race control ------------------------------------------------------- */

/** How far through the race the race-control calls have been caught up to. */
let rcSeen = 0;

/** Moving the playhead further than this in one step is a seek, not the race
 *  running: everything crossed is marked as seen but none of it announced. */
const SEEK_JUMP_S = 30;

/** Calls from race control, announced on the island as the playhead reaches
 *  them. There is no panel keeping them any more; the island is the feed. */
function announceRaceControl(upTo) {
  if (!player.bundle) return;
  if (upTo < rcSeen || upTo - rcSeen > SEEK_JUMP_S) { rcSeen = upTo; return; }
  for (const event of player.bundle.race_control || []) {
    if (event.t <= rcSeen || event.t > upTo || !worthAnnouncing(event)) continue;
    notify("control", (event.state || "note").replace("_", " ").toUpperCase(),
      event.message || "", clock(event.t));
  }
  rcSeen = upTo;
}

/* -- transport ---------------------------------------------------------- */

function tick(now) {
  if (player.playing && player.bundle) {
    const elapsed = Math.min((now - player.lastFrameAt) / 1000, 0.25);
    player.time = Math.min(player.time + elapsed * player.speed, player.bundle.duration_s);
    renderFrame(player.time);
    if (player.time >= player.bundle.duration_s) setPlaying(false);
  }
  player.lastFrameAt = now;
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);

function setPlaying(playing) {
  player.playing = playing && !!player.bundle && player.framesReady;
  const button = el("playBtn");
  button.innerHTML = player.playing ? "&#10074;&#10074;" : "&#9654;";
  button.setAttribute("aria-label", player.playing ? "pause" : "play");
  setBackgroundAwake(!player.playing);
}

function seek(to) {
  if (!player.bundle) return;
  player.time = Math.min(Math.max(to, 0), player.bundle.duration_s);
  renderFrame(player.time);
}

el("playBtn").addEventListener("click", () => setPlaying(!player.playing));
el("scrub").addEventListener("input", (event) => seek(Number(event.target.value)));
el("speed").addEventListener("change", (event) => { player.speed = Number(event.target.value); });

/* The transport without having to find the scrub bar with a pointer. Typing
   in the search box is still typing, so the keys are ignored there. */
document.addEventListener("keydown", (event) => {
  const target = event.target;
  if (target && target.matches && target.matches("input, select, textarea")) return;
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key === "Escape" && player.selected != null) { selectCar(player.selected); return; }
  if (!player.bundle) return;
  const step = event.shiftKey ? 60 : 10;
  switch (event.key) {
    case " ":
      if (target && target.closest && target.closest(".car, button")) return;
      event.preventDefault(); setPlaying(!player.playing); break;
    case "ArrowLeft":  event.preventDefault(); seek(player.time - step); break;
    case "ArrowRight": event.preventDefault(); seek(player.time + step); break;
    case "Home":       event.preventDefault(); seek(0); break;
    case "End":        event.preventDefault(); seek(player.bundle.duration_s); break;
    default: break;
  }
});

const FLAG_COLOURS = {
  green: "rgba(255,255,255,.16)", yellow: "#c9a227", vsc: "#c98a15",
  safety_car: "#e0a020", red_flag: "#e0453a",
};

/** The flag state across a whole race, as a css gradient. Hard stops: a race
 *  is green or it is not, and a fade would invent a state that never was. */
function flagGradient(bundle) {
  const duration = bundle.duration_s || 1;
  const stops = [];
  let previous = null;
  for (const frame of bundle.frames) {
    const flag = frame.flag || "green";
    if (flag === previous) continue;
    const at = ((frame.t / duration) * 100).toFixed(3);
    const colour = FLAG_COLOURS[flag] || FLAG_COLOURS.green;
    if (stops.length) stops.push(`${stops[stops.length - 1].split(" ")[0]} ${at}%`);
    stops.push(`${colour} ${at}%`);
    previous = flag;
  }
  if (!stops.length) return FLAG_COLOURS.green;
  return `linear-gradient(90deg, ${stops.join(", ")}, ${stops[stops.length - 1].split(" ")[0]} 100%)`;
}

export {
  SEEK_JUMP_S,
  carNodes,
  fitMap,
  openReplay,
  openUpcoming,
  player,
  renderFrame,
  selectCar,
  setPlaying,
};
