/* Flags on the circuit itself: the white edge of the track takes the colour
 * of whatever flag covers that part of it, and a marshal stands by a yellow.
 *
 * Race control says where its flags are in words, so this reads the words:
 *
 *   "YELLOW IN TRACK SECTOR 7", "DOUBLE YELLOW IN TRACK SECTOR 3"
 *        that marshal sector goes yellow, with a marshal waving one flag (or two)
 *   "CLEAR IN TRACK SECTOR 7"         that sector goes back to white
 *   "TRACK CLEAR"                     every sector does
 *   "WAVED BLUE FLAG FOR CAR 18 ..."  the track around car 18 goes blue for a few seconds
 *   "MARSHALS ON TRACK AT TURN 9"     a marshal walks out by the yellow
 *
 * and a red flag or a safety car (the frame's flag state) colours the lot.
 *
 * Where a marshal sector is: the feed numbers them along the lap but does
 * not say where each begins, so the lap is shared evenly between as many
 * sectors as the session ever mentions. That puts a yellow in the right part
 * of the circuit, not on the exact marshal post.
 */

import { svgNS } from "./core.js";

/** How long a waved blue flag stays painted around the car it was for. */
const BLUE_S = 8;
/** How long marshals stay out after they are called on, unless cleared. */
const ON_TRACK_S = 150;
/** The fewest marshal sectors a lap is divided into. */
const MIN_SECTORS = 12;
/** How much of the lap a blue flag covers either side of its car, in path points. */
const BLUE_REACH = 14;

const flags = {
  events: [],       // { t, kind, sector?, car? }, by time
  sectors: MIN_SECTORS,
  path: [],         // the racing line on screen
  trackWidth: 18,
  centre: [0, 0],
  key: "",
  blueNodes: [],
};

function parse(control) {
  const events = [];
  let most = 0;
  for (const entry of control) {
    // Flags waved before the session -- installation laps, the build-up --
    // were all cleared before it started; carrying them in would start the
    // race under yellows that are not there.
    if (!(entry.t >= 0)) continue;
    const text = String(entry.message || "").toUpperCase();
    let m;
    if ((m = text.match(/(DOUBLE )?YELLOW IN TRACK SECTOR (\d+)/))) {
      events.push({ t: entry.t, kind: m[1] ? "double" : "yellow", sector: +m[2] });
      most = Math.max(most, +m[2]);
    } else if ((m = text.match(/CLEAR IN TRACK SECTOR (\d+)/))) {
      events.push({ t: entry.t, kind: "clear", sector: +m[1] });
      most = Math.max(most, +m[1]);
    } else if (/^TRACK CLEAR/.test(text)) {
      events.push({ t: entry.t, kind: "allclear" });
    } else if ((m = text.match(/WAVED BLUE FLAG FOR CAR (\d+)/))) {
      events.push({ t: entry.t, kind: "blue", car: +m[1] });
    } else if (/MARSHALS? ON TRACK/.test(text)) {
      events.push({ t: entry.t, kind: "marshals" });
    }
  }
  events.sort((a, b) => a.t - b.t);
  return { events, sectors: Math.max(most, MIN_SECTORS) };
}

/** A new session's race control. */
function setFlagTimeline(control) {
  Object.assign(flags, parse(control));
  flags.key = "";
  draw({ sectors: new Map(), onTrack: false, global: "green" });
}

function clearFlags() { setFlagTimeline([]); }

/** The racing line moved (a new circuit, or the page resized). */
function layoutFlags(path, trackWidth) {
  flags.path = path;
  flags.trackWidth = trackWidth;
  const n = path.length || 1;
  flags.centre = path.reduce((acc, [x, y]) => [acc[0] + x / n, acc[1] + y / n], [0, 0]);
  flags.key = "";
}

/** What is out at `time`. */
function stateAt(time) {
  const sectors = new Map();
  let onTrackUntil = -1;
  const blue = [];
  for (const event of flags.events) {
    if (event.t > time) break;
    switch (event.kind) {
      case "yellow": case "double": sectors.set(event.sector, event.kind); break;
      case "clear": sectors.delete(event.sector); break;
      case "allclear": sectors.clear(); onTrackUntil = -1; break;
      case "marshals": onTrackUntil = event.t + ON_TRACK_S; break;
      case "blue": if (time - event.t <= BLUE_S && !blue.includes(event.car)) blue.push(event.car); break;
      default: break;
    }
  }
  return { sectors, onTrack: time < onTrackUntil && sectors.size > 0, blue };
}

/** Called every frame. Rebuilds only when what is out changes; the blue
 *  flags follow their cars, so those are moved every frame. */
function renderFlags(time, global, progressOf) {
  if (!flags.path.length) return;
  const state = stateAt(time);
  const g = global === "red_flag" ? "red" : global === "safety_car" || global === "vsc" ? "neutralised" : "green";
  const key = `${g}|${[...state.sectors].join(",")}|${state.onTrack}|${state.blue.length}`;
  if (key !== flags.key) {
    flags.key = key;
    draw({ sectors: state.sectors, onTrack: state.onTrack, global: g, blue: state.blue.length });
  }
  state.blue.forEach((car, index) => {
    const node = flags.blueNodes[index];
    const at = progressOf(car);
    if (!node) return;
    if (at == null) { node.setAttribute("d", ""); return; }
    node.setAttribute("d", segment(at - BLUE_REACH, at + BLUE_REACH / 2));
  });
}

/* -- drawing ---------------------------------------------------------------- */

function segment(from, to) {
  const path = flags.path;
  const n = path.length;
  if (!n) return "";
  const points = [];
  for (let i = Math.floor(from); i <= Math.ceil(to); i += 1) {
    points.push(path[((i % n) + n) % n]);
  }
  return points.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
}

function sectorRange(sector) {
  const n = flags.path.length;
  const share = n / flags.sectors;
  return [(sector - 1) * share, sector * share];
}

function edgePath(cls, d) {
  const node = document.createElementNS(svgNS, "path");
  node.setAttribute("class", `flag-edge ${cls}`);
  node.setAttribute("d", d);
  node.setAttribute("stroke-width", (flags.trackWidth + 9).toFixed(1));
  return node;
}

function draw({ sectors, onTrack, global, blue = 0 }) {
  const edges = document.getElementById("flagEdges");
  const marshals = document.getElementById("marshals");
  if (!edges || !marshals) return;
  edges.textContent = "";
  marshals.textContent = "";
  flags.blueNodes = [];
  if (!flags.path.length) return;

  if (global !== "green") {
    edges.append(edgePath(global, segment(0, flags.path.length)));
  }
  for (const [sector, kind] of sectors) {
    const [from, to] = sectorRange(sector);
    edges.append(edgePath(kind === "double" ? "yellow double" : "yellow", segment(from, to)));
    const mid = (from + to) / 2;
    marshals.append(marshal(mid, kind === "double" ? 2 : 1, false));
    if (onTrack) marshals.append(marshal(mid + (to - from) * 0.22, 0, true));
  }
  for (let i = 0; i < blue; i += 1) {
    const node = edgePath("blue", "");
    edges.append(node);
    flags.blueNodes.push(node);
  }
}

/** A marshal: an orange figure by the track at `progress` along the lap,
 *  waving `waving` yellow flags (0, 1 or 2); `onTrack` stands them at the
 *  edge of the circuit instead of behind the barrier. */
function marshal(progress, waving, onTrack) {
  const path = flags.path;
  const n = path.length;
  const i = ((Math.round(progress) % n) + n) % n;
  const [x, y] = path[i];
  const [ax, ay] = path[(i + n - 2) % n];
  const [bx, by] = path[(i + 2) % n];
  let nx = -(by - ay), ny = bx - ax;
  const len = Math.hypot(nx, ny) || 1;
  nx /= len; ny /= len;
  // Outside the loop, away from its middle.
  if ((x - flags.centre[0]) * nx + (y - flags.centre[1]) * ny < 0) { nx = -nx; ny = -ny; }
  const away = flags.trackWidth * (onTrack ? 0.9 : 1.9);
  const scale = Math.max(1, flags.trackWidth / 9);

  const g = document.createElementNS(svgNS, "g");
  g.setAttribute("class", `marshal${onTrack ? " on-track" : ""}`);
  g.setAttribute("transform",
    `translate(${(x + nx * away).toFixed(1)} ${(y + ny * away).toFixed(1)}) scale(${scale.toFixed(2)})`);
  const arm = (side) => `
    <g transform="translate(0 -6)"><g class="m-arm ${side < 0 ? "left" : "right"}">
      <line x1="0" y1="0" x2="${3.6 * side}" y2="-3.8" />
      <line class="m-pole" x1="${3.6 * side}" y1="-3.8" x2="${3.6 * side}" y2="-10.5" />
      <path class="m-flag" d="M${3.6 * side} -10.5 h${6 * side} v4.2 h${-6 * side} z" />
    </g></g>`;
  const restArm = (side) => `<line x1="0" y1="-6" x2="${2.6 * side}" y2="-1.5" />`;
  g.innerHTML = `
    <g class="m-body">
      <circle class="m-head" cx="0" cy="-10" r="2.3" />
      <line x1="0" y1="-7.6" x2="0" y2="0" />
      <line x1="0" y1="0" x2="-2.6" y2="5.5" /><line x1="0" y1="0" x2="2.6" y2="5.5" />
      ${waving >= 1 ? arm(1) : restArm(1)}
      ${waving >= 2 ? arm(-1) : restArm(-1)}
    </g>`;
  return g;
}

export { clearFlags, layoutFlags, renderFlags, setFlagTimeline };
