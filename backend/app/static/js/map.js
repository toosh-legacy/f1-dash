/* The circuit, the cars on it, and playing a race back over both.
 *
 * The bundle arrives as a reconstructed track and a few thousand frames of
 * progress along it. Drawing is therefore two jobs kept apart: the circuit is
 * laid down once when a race is opened, and the cars are moved over it on
 * every frame -- so playback touches transforms rather than rebuilding paths.
 */

import { bodies, layout } from "./board.js";
import { setBackgroundAwake } from "./chrome.js";
import { api, clock, el, setStatus, svgNS, when } from "./core.js";
import { clearNotices, islandTick, notify, renderEvents, reportOvertakes, seenUpTo, worthAnnouncing } from "./notices.js";
import { projectionCache, refreshProjection, renderProjection } from "./prediction.js";
import { catalogue, upcoming } from "./search.js";

const player = {
  bundle: null, sessionKey: null, path: [], pitPath: [], bounds: null,
  playing: false, time: 0, speed: 10, lastFrameAt: 0, lap: 0, selected: null,
  upcoming: null,
};

function mapMarkup() {
  return `
    <div id="mapWrap">
      <svg id="map" preserveAspectRatio="xMidYMid meet">
        <path id="trackGlow" /><path id="trackHalo" />
        <path id="pitLine" /><path id="pitSurface" />
        <path id="trackLine" /><path id="trackSurface" />
        <line id="startLine" />
        <g id="cars"></g>
        <g id="carLabels"></g>
      </svg>
      <div id="countdown" hidden>
        <div class="k" id="cdKind">lights out in</div>
        <div class="t" id="cdTime">—</div>
        <div class="w" id="cdWhen"></div>
      </div>
    </div>`;
}

function fitTransform(bounds, width, height, pad) {
  const spanX = Math.max(bounds.max_x - bounds.min_x, 1);
  const spanY = Math.max(bounds.max_y - bounds.min_y, 1);
  const scale = Math.min((width - pad * 2) / spanX, (height - pad * 2) / spanY);
  const offsetX = (width - spanX * scale) / 2;
  const offsetY = (height - spanY * scale) / 2;
  return (x, y) => [
    offsetX + (x - bounds.min_x) * scale,
    height - offsetY - (y - bounds.min_y) * scale,
  ];
}

function toPathData(points, close) {
  if (!points.length) return "";
  return points.map(([x, y], index) => `${index ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ")
    + (close ? " Z" : "");
}

/** Redraw the circuit into whatever space the panel currently has.
 *  Called on load, on resize and whenever the map panel is resized. */
function fitMap() {
  const wrap = el("mapWrap");
  if (!wrap || !player.bounds) return;
  const width = Math.max(wrap.clientWidth, 200);
  const height = Math.max(wrap.clientHeight, 200);
  el("map").setAttribute("viewBox", `0 0 ${width} ${height}`);

  const project = fitTransform(player.bounds, width, height, 22);
  player.path = player.rawPath.map(([x, y]) => project(x, y));
  player.pitPath = player.rawPit.map(([x, y]) => project(x, y));

  for (const id of ["trackGlow", "trackHalo", "trackLine", "trackSurface"]) {
    el(id).setAttribute("d", toPathData(player.path, true));
  }
  el("pitLine").setAttribute("d", toPathData(player.pitPath, false));
  el("pitSurface").setAttribute("d", toPathData(player.pitPath, false));

  const [sx, sy] = player.path[0];
  const [nx, ny] = player.path[1] || [sx + 1, sy];
  const angle = Math.atan2(ny - sy, nx - sx) + Math.PI / 2;
  el("startLine").setAttribute("x1", sx + Math.cos(angle) * 8);
  el("startLine").setAttribute("y1", sy + Math.sin(angle) * 8);
  el("startLine").setAttribute("x2", sx - Math.cos(angle) * 8);
  el("startLine").setAttribute("y2", sy - Math.sin(angle) * 8);
}

function setTrack(track) {
  player.rawPath = track.path;
  player.rawPit = track.pit_path || [];
  player.bounds = track.bounds;
  fitMap();
}

window.addEventListener("resize", () => { if (player.bounds) { fitMap(); renderFrame(player.time); } });

/* -- a race that has run ------------------------------------------------ */

/** Open a race that has run.
 *
 *  The bundle is 475 KB and 439 KB of that is playback frames -- positions,
 *  once a second, for two hours. Everything needed to put the race on screen
 *  is in the other 36 KB: the circuit, the drivers, the per-lap order, the
 *  race-control log, the overtakes and the classification. So the metadata is
 *  fetched first and drawn immediately, and the frames stream in behind it.
 *  The circuit is up in a fraction of the time, and the only thing that waits
 *  is the play button. */
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
  bundle.frames = [];
  player.bundle = bundle;
  player.sessionKey = sessionKey;
  player.playing = false;
  player.framesReady = false;
  player.time = 0;
  player.lap = 0;
  player.upcoming = null;
  projectionCache.clear();
  clearNotices();
  rcSeen = 0;
  seenUpTo(0);
  rcShown = -1;

  setTrack(bundle.track);
  buildCarLayer(bundle.drivers);

  const session = bundle.session;
  setTitle("map", `${session.circuit || session.location} · ${session.name}`);
  el("scrub").max = String(Math.round(bundle.duration_s));
  el("flagChip").hidden = false;
  el("countdown").hidden = true;
  renderOrder(1);
  renderRaceControl(0);
  refreshProjection(1);
  setPlayable(false);
  setStatus(`${session.circuit} — loading playback…`);

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

    if (el("flagStrip")) el("flagStrip").style.background = flagGradient(bundle);
    setPlayable(true);
    renderFrame(player.time);
    setStatus(`${bundle.session.circuit} ready`, "live");
  } catch (error) {
    setStatus(`playback unavailable: ${error.message}`, "error");
  }
}

function setPlayable(ready) {
  const button = el("playBtn");
  if (button) button.disabled = !ready;
  const scrub = el("scrub");
  if (scrub) scrub.disabled = !ready;
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
  player.bundle = null;
  player.upcoming = round;
  player.playing = false;
  el("flagChip").hidden = true;
  buildCarLayer([]);
  clearNotices();
  rcSeen = 0;
  setTitle("map", `${round.circuit} · ${round.name}`);
  setStatus("not yet run");

  let laid = false;
  try {
    const layoutData = await api(`/layouts/${encodeURIComponent(round.circuit)}`);
    setTrack(layoutData.track);
    laid = true;
  } catch (_) {
    for (const id of ["trackGlow", "trackHalo", "trackLine", "trackSurface", "pitLine", "pitSurface"]) {
      el(id).setAttribute("d", "");
    }
    el("startLine").setAttribute("x1", 0); el("startLine").setAttribute("x2", 0);
  }

  el("cdKind").textContent = laid ? "lights out in" : "no layout yet · lights out in";
  el("cdWhen").textContent = when(round.date_start);
  el("countdown").hidden = false;
  tickCountdown(round.date_start);
  countdownTimer = setInterval(() => tickCountdown(round.date_start), 1000);

  await showProvisionalGrid(round);
  renderRaceControl(0);
  renderProjection(null);
}

function tickCountdown(iso) {
  const remaining = new Date(iso).getTime() - Date.now();
  if (remaining <= 0) { el("cdTime").textContent = "under way"; return; }
  const seconds = Math.floor(remaining / 1000);
  const days = Math.floor(seconds / 86400);
  const hours = String(Math.floor((seconds % 86400) / 3600)).padStart(2, "0");
  const minutes = String(Math.floor((seconds % 3600) / 60)).padStart(2, "0");
  const secs = String(seconds % 60).padStart(2, "0");
  el("cdTime").textContent = (days ? `${days}d ` : "") + `${hours}:${minutes}:${secs}`;
}

/** The grid as it stands before a session that has not run.
 *
 *  Nobody knows the real starting order until qualifying happens, so this is
 *  the last completed race's finishing order, said plainly rather than dressed
 *  up as a prediction. */
async function showProvisionalGrid(round) {
  el("orderLap") && (el("orderLap").textContent = "provisional");
  const container = bodies.order;
  container.innerHTML = '<div class="empty">Looking up the last result…</div>';

  const previous = (catalogue || [])
    .filter((entry) => entry.cached && new Date(entry.date_start) < new Date(round.date_start))
    .sort((a, b) => new Date(b.date_start) - new Date(a.date_start))[0];
  if (!previous) {
    container.innerHTML = '<div class="empty">No finished race is built, so there is no order to show yet.</div>';
    return;
  }
  try {
    // Only the classification is wanted here, so only the metadata is fetched.
    const bundle = await api(`/replays/${previous.session_key}/meta`);
    container.textContent = "";
    const note = document.createElement("div");
    note.className = "side-foot";
    note.style.borderTop = "none";
    note.textContent = `Provisional — the order from ${bundle.session.circuit}, `
      + `${new Date(bundle.session.date_start).toLocaleDateString()}. The grid is set in qualifying.`;
    container.append(note);
    for (const row of bundle.results) {
      const node = document.createElement("div");
      node.className = "order-row" + (row.position === 1 ? " leader" : "");
      node.innerHTML = `
        <span class="pos">${row.position}</span>
        <span class="bar" style="background:${row.colour || "#666"}"></span>
        <span class="who"><b>${row.code || row.number}</b><small>${row.team || ""}</small></span>
        <span class="gap"><span class="t">${row.laps ?? ""}</span></span>`;
      container.append(node);
    }
  } catch (_) {
    container.innerHTML = '<div class="empty">Could not read the last result.</div>';
  }
}

/** The map's heading is the race, not the word "Circuit", so it has to be
 *  remembered across a rebuild of the board. */
let mapTitle = "";

function setTitle(panelId, text) {
  if (panelId === "map") mapTitle = text;
  const widget = document.querySelector(`.widget[data-panel="${panelId}"] h2`);
  if (widget) widget.textContent = text;
}

/* -- cars --------------------------------------------------------------- */

const carNodes = new Map();
const NAME_CLEARANCE = 54;

function surnameOf(driver) {
  const parts = String(driver.name || "").trim().split(/\s+/);
  return (parts[parts.length - 1] || driver.code || "").toUpperCase();
}

function buildCarLayer(drivers) {
  const layer = el("cars");
  const labels = el("carLabels");
  layer.textContent = "";
  labels.textContent = "";
  carNodes.clear();
  for (const driver of drivers) {
    const group = document.createElementNS(svgNS, "g");
    group.setAttribute("class", "car");

    const dot = document.createElementNS(svgNS, "circle");
    dot.setAttribute("class", "car-dot");
    dot.setAttribute("fill", driver.colour);
    dot.style.filter = `drop-shadow(0 0 4px ${driver.colour}) drop-shadow(0 0 9px ${driver.colour})`;

    const label = document.createElementNS(svgNS, "text");
    label.setAttribute("class", "car-label");
    label.textContent = driver.code;

    const name = document.createElementNS(svgNS, "text");
    name.setAttribute("class", "car-name");
    name.setAttribute("dy", "26");
    name.textContent = surnameOf(driver);

    const tag = document.createElementNS(svgNS, "text");
    tag.setAttribute("class", "car-tag");
    tag.setAttribute("dy", "-19");

    group.append(dot, label);
    group.addEventListener("click", () => selectCar(driver.number));
    layer.append(group);

    const labelGroup = document.createElementNS(svgNS, "g");
    labelGroup.append(name, tag);
    labels.append(labelGroup);
    carNodes.set(String(driver.number), { group, labelGroup, dot, label, name, tag });
  }
}

function selectCar(number) {
  player.selected = player.selected === number ? null : number;
  for (const [key, node] of carNodes) {
    const chosen = key === String(player.selected);
    node.group.classList.toggle("selected", chosen);
    node.labelGroup.classList.toggle("selected", chosen);
  }
  if (player.bundle) renderFrame(player.time);
}

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
  const placed = [];
  for (const [number, node] of carNodes) {
    const here = frame.cars[number];
    if (here === undefined) {
      node.group.setAttribute("visibility", "hidden");
      node.labelGroup.setAttribute("visibility", "hidden");
      continue;
    }
    node.group.setAttribute("visibility", "visible");
    node.labelGroup.setAttribute("visibility", "visible");

    let x, y;
    const inPit = Array.isArray(here);
    if (inPit) {
      [x, y] = pointAt(player.pitPath.length ? player.pitPath : player.path, here[1]);
    } else {
      const there = next ? next.cars[number] : undefined;
      const progress = (typeof there === "number") ? blendProgress(here, there, alpha, loop) : here;
      [x, y] = pointAt(player.path, progress);
    }
    node.group.classList.toggle("pit", inPit);
    node.dot.setAttribute("cx", x); node.dot.setAttribute("cy", y);
    node.label.setAttribute("x", x); node.label.setAttribute("y", y);
    node.name.setAttribute("x", x); node.name.setAttribute("y", y);
    node.tag.setAttribute("x", x); node.tag.setAttribute("y", y);
    node.tag.textContent = inPit ? "PIT" : "";
    placed.push([node, x, y]);
  }
  hideCrowdedNames(placed);

  el("clock").textContent = `${clock(time)} / ${clock(bundle.duration_s)}`;
  const scrub = el("scrub");
  scrub.value = String(Math.round(time));
  scrub.style.setProperty("--played", (time / (bundle.duration_s || 1) * 100).toFixed(2));
  const flag = frame.flag || "green";
  player.flag = flag;
  const chip = el("flagChip");
  chip.className = `flag ${flag}`;
  chip.textContent = flag.replace("_", " ");
  el("lapCount").textContent = `lap ${frame.lap || 0} / ${bundle.total_laps}`;

  renderRaceControl(time);
  if (bundle.events) {
    renderEvents(time);
  }
  if (frame.lap !== player.lap) {
    const previous = player.lap;
    player.lap = frame.lap;
    renderOrder(frame.lap);
    // Only when the backend did not supply events: a lap completed in order
    // says something about who passed whom, a scrub jump says nothing.
    if (!bundle.events && frame.lap === previous + 1) reportOvertakes(previous, frame.lap);
    if (layout.prediction.visible) refreshProjection(frame.lap);
  }
  islandTick();   // after the lap lands, so the resting state is current
}

function hideCrowdedNames(placed) {
  for (let index = 0; index < placed.length; index += 1) {
    const [node, x, y] = placed[index];
    let crowded = false;
    for (let other = 0; other < index && !crowded; other += 1) {
      const [, ox, oy] = placed[other];
      crowded = Math.hypot(x - ox, y - oy) < NAME_CLEARANCE;
    }
    node.name.setAttribute("visibility",
      crowded && !node.labelGroup.classList.contains("selected") ? "hidden" : "visible");
  }
}

/* -- the running order -------------------------------------------------- */

function renderOrder(lap) {
  const container = bodies.order;
  if (!player.bundle) return;
  const laps = player.bundle.laps || {};
  const rows = laps[String(lap)] || laps["1"] || [];
  const drivers = new Map(player.bundle.drivers.map((d) => [d.number, d]));
  if (el("orderLap")) el("orderLap").textContent = lap ? `lap ${lap}` : "—";
  if (!rows.length) { container.innerHTML = '<div class="empty">No timing on this lap.</div>'; return; }

  container.textContent = "";
  for (const row of rows) {
    const driver = drivers.get(row.number) || {};
    const leading = row.position === 1;
    const node = document.createElement("div");
    node.className = "order-row" + (leading ? " leader" : "");
    node.innerHTML = `
      <span class="pos">${row.position ?? "–"}</span>
      <span class="bar" style="background:${driver.colour || "#666"}"></span>
      <span class="who"><b>${driver.code || row.number}</b><small>${driver.team || ""}</small></span>
      <span class="gap">
        ${row.compound ? `<span class="tyre ${row.compound}">${row.compound[0]}${row.tyre_age ?? ""}</span>` : ""}
        <span class="t">${leading ? "leader"
          : row.gap_to_leader_s != null ? `+${row.gap_to_leader_s.toFixed(1)}` : "–"}</span>
      </span>`;
    node.addEventListener("click", () => selectCar(row.number));
    container.append(node);
  }
}

/* -- race control ------------------------------------------------------- */

/** How far through the race the notifications have been caught up to. */
let rcSeen = 0;

/** Moving the playhead further than this in one step is a seek, not the race
 *  running. Everything crossed is still marked as seen -- it is history the
 *  moment you land past it -- but none of it is announced: dragging the scrub
 *  bar across half a grand prix should not queue forty things to be told. */
const SEEK_JUMP_S = 30;

/** How many calls the list is currently showing. The playhead moves sixty
 *  times a second and the list changes a few times a race, so it is rebuilt
 *  only when the count moves rather than on every frame. */
let rcShown = -1;

function renderRaceControl(upTo) {
  const container = bodies.control;
  if (!player.bundle) {
    container.innerHTML = '<div class="empty">Nothing announced — this session has not run.</div>';
    if (el("rcCount")) el("rcCount").textContent = "—";
    rcShown = -1;
    return;
  }
  const all = (player.bundle.race_control || []).filter(
    (event) => event.t >= 0 && event.t <= player.bundle.duration_s
  );
  const events = all.filter((event) => event.t <= upTo);
  if (el("rcCount")) el("rcCount").textContent = `${events.length} of ${all.length}`;

  // Anything called since the last look is announced. Scrubbing backwards
  // moves the mark rather than replaying calls already seen.
  if (upTo < rcSeen) {
    rcSeen = upTo;
    rcShown = -1;
  } else if (upTo - rcSeen > SEEK_JUMP_S) {
    rcSeen = upTo;                       // seeked past it; already history
  } else {
    for (const event of events) {
      if (event.t <= rcSeen || !worthAnnouncing(event)) continue;
      notify("control", (event.state || "note").replace("_", " ").toUpperCase(),
        event.message || "", clock(event.t));
    }
    rcSeen = upTo;
  }

  if (events.length === rcShown) return;
  rcShown = events.length;

  if (!events.length) { container.innerHTML = '<div class="empty">Nothing announced yet.</div>'; return; }
  container.textContent = "";
  for (const event of events.slice(-80).reverse()) {
    const row = document.createElement("div");
    row.className = "rc-row";
    row.innerHTML = `<span class="at">${clock(event.t)}</span>
      <span>${event.state ? `<span class="flag ${event.state}">${event.state.replace("_", " ")}</span>` : ""}</span>
      <span class="msg" title="${(event.message || "").replace(/"/g, "&quot;")}">${event.message || ""}</span>`;
    container.append(row);
  }
}

/* -- transport ---------------------------------------------------------- */

function tick(now) {
  if (player.playing && player.bundle) {
    const elapsed = (now - player.lastFrameAt) / 1000;
    player.time = Math.min(player.time + elapsed * player.speed, player.bundle.duration_s);
    renderFrame(player.time);
    if (player.time >= player.bundle.duration_s) setPlaying(false);
  }
  player.lastFrameAt = now;
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);

function setPlaying(playing) {
  player.playing = playing && !!player.bundle;
  const button = el("playBtn");
  if (button) button.textContent = player.playing ? "❚❚" : "▶";
  if (typeof setBackgroundAwake === "function") setBackgroundAwake(!player.playing);
}
el("speed").addEventListener("change", (event) => { player.speed = Number(event.target.value); });

const FLAG_COLOURS = {
  green: "#23272e", yellow: "#3f3722", vsc: "#8a6a15",
  safety_car: "#d69a20", red_flag: "#c0392b",
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
  FLAG_COLOURS,
  NAME_CLEARANCE,
  SEEK_JUMP_S,
  blendProgress,
  buildCarLayer,
  carNodes,
  countdownTimer,
  fitMap,
  fitTransform,
  flagGradient,
  hideCrowdedNames,
  loadFrames,
  mapMarkup,
  mapTitle,
  openReplay,
  openUpcoming,
  player,
  pointAt,
  rcSeen,
  rcShown,
  renderFrame,
  renderOrder,
  renderRaceControl,
  selectCar,
  setPlayable,
  setPlaying,
  setTitle,
  setTrack,
  showProvisionalGrid,
  stopCountdown,
  surnameOf,
  tick,
  tickCountdown,
  toPathData,
};
