/* The board: a grid, and the panels that live in cells of it.
 *
 * Free placement gave the viewer a pixel to land on and no reason to prefer
 * any particular one, so arrangements came out almost-aligned. A grid removes
 * the question: a panel occupies whole cells, every edge lands on a line, and
 * two panels put side by side are exactly side by side.
 *
 * The cell is small -- a twenty-fourth of the width, a twelfth of the height --
 * so the grid guides rather than dictates, the way an icon grid does. Panels
 * are then described the way a phone describes a widget: a size in cells, with
 * a handful of named sizes for the common ones.
 */

import { clock, el } from "./core.js";
import { fitMap, flagGradient, mapTitle, player, renderFrame, renderOrder, renderRaceControl, setPlayable, setPlaying, setTitle } from "./map.js";
import { notices, notify, renderNotices } from "./notices.js";
import { refreshProjection } from "./prediction.js";

const GRID_COLS = 24;
const GRID_ROWS = 12;

//: Below this a panel has a heading and no room for anything under it.
const MIN_CW = 4, MIN_CH = 2;

const PANELS = [
  // The circuit is the page, so it does not move. It is pinned to the left for
  // its whole height and the only thing the viewer changes is how wide it is --
  // which is the same gesture as making the column beside it narrower.
  { id: "map",        title: "Circuit",       c: 0,  r: 0, cw: 14, ch: 12, always: true, pinned: true },
  { id: "order",      title: "Running order", c: 14, r: 0, cw: 5,  ch: 12 },
  { id: "notify",     title: "Notifications", c: 19, r: 0, cw: 5,  ch: 6 },
  { id: "control",    title: "Race control",  c: 19, r: 6, cw: 5,  ch: 6 },
  { id: "prediction", title: "Prediction",    c: 4,  r: 1, cw: 11, ch: 9, off: true, float: true },
  { id: "log",        title: "Live feed",     c: 14, r: 7, cw: 5,  ch: 5, off: true },
];

/** The sizes a panel is usually wanted at, named. Same idea as a phone's
 *  small / medium / large: most of the time somebody wants one of a few
 *  shapes, and picking one from a list beats dragging to it. */
const SIZES = [
  { key: "small",  label: "Small",  cw: 5,  ch: 4 },
  { key: "wide",   label: "Wide",   cw: 10, ch: 4 },
  { key: "tall",   label: "Tall",   cw: 5,  ch: 8 },
  { key: "large",  label: "Large",  cw: 10, ch: 8 },
  { key: "column", label: "Column", cw: 5,  ch: 12 },
];

/** Arrangements worth starting from, in cells.
 *
 *  The useful ones are not arbitrary -- they follow what somebody is doing.
 *  Watching a race wants the order beside the track; reading strategy wants the
 *  projection open and large; arguing about a call wants race control given
 *  real room. The circuit is pinned left in all of them; only its width moves. */
const PRESETS = {
  broadcast: {
    label: "Broadcast",
    hint: "the track, the order, and what just happened",
    panels: {
      map:        { c: 0,  r: 0, cw: 14, ch: 12, visible: true },
      order:      { c: 14, r: 0, cw: 5,  ch: 12, visible: true },
      notify:     { c: 19, r: 0, cw: 5,  ch: 6,  visible: true },
      control:    { c: 19, r: 6, cw: 5,  ch: 6,  visible: true },
      prediction: { c: 4,  r: 1, cw: 11, ch: 9,  visible: false },
      log:        { c: 14, r: 7, cw: 5,  ch: 5,  visible: false },
    },
  },
  strategy: {
    label: "Strategy",
    hint: "the projection, given room to be read",
    panels: {
      map:        { c: 0,  r: 0, cw: 10, ch: 12, visible: true },
      prediction: { c: 10, r: 0, cw: 9,  ch: 12, visible: true },
      order:      { c: 19, r: 0, cw: 5,  ch: 8,  visible: true },
      notify:     { c: 19, r: 8, cw: 5,  ch: 4,  visible: true },
      control:    { c: 10, r: 7, cw: 9,  ch: 5,  visible: false },
      log:        { c: 14, r: 7, cw: 5,  ch: 5,  visible: false },
    },
  },
  stewards: {
    label: "Stewards",
    hint: "every call, in full, next to the order it changed",
    panels: {
      map:        { c: 0,  r: 0, cw: 9,  ch: 12, visible: true },
      control:    { c: 9,  r: 0, cw: 9,  ch: 12, visible: true },
      order:      { c: 18, r: 0, cw: 6,  ch: 7,  visible: true },
      notify:     { c: 18, r: 7, cw: 6,  ch: 5,  visible: true },
      prediction: { c: 4,  r: 1, cw: 11, ch: 9,  visible: false },
      log:        { c: 14, r: 7, cw: 5,  ch: 5,  visible: false },
    },
  },
  wall: {
    label: "Pit wall",
    hint: "everything at once, for a second screen",
    panels: {
      map:        { c: 0,  r: 0, cw: 11, ch: 12, visible: true },
      order:      { c: 11, r: 0, cw: 5,  ch: 12, visible: true },
      prediction: { c: 16, r: 0, cw: 8,  ch: 6,  visible: true },
      notify:     { c: 16, r: 6, cw: 4,  ch: 6,  visible: true },
      control:    { c: 20, r: 6, cw: 4,  ch: 6,  visible: true },
      log:        { c: 14, r: 7, cw: 5,  ch: 5,  visible: false },
    },
  },
  focus: {
    label: "Focus",
    hint: "the circuit, and nothing competing with it",
    panels: {
      map:        { c: 0,  r: 0, cw: 18, ch: 12, visible: true },
      order:      { c: 18, r: 0, cw: 6,  ch: 12, visible: true },
      notify:     { c: 19, r: 0, cw: 5,  ch: 6,  visible: false },
      control:    { c: 19, r: 6, cw: 5,  ch: 6,  visible: false },
      prediction: { c: 4,  r: 1, cw: 11, ch: 9,  visible: false },
      log:        { c: 14, r: 7, cw: 5,  ch: 5,  visible: false },
    },
  },
};

const DEFAULT_PRESET = "broadcast";
let currentPreset = DEFAULT_PRESET;

function applyPreset(name) {
  const preset = PRESETS[name];
  if (!preset) return;
  for (const [id, box] of Object.entries(preset.panels)) {
    if (!layout[id]) continue;
    Object.assign(layout[id], box, { restore: null });
  }
  layout.map.visible = true;
  currentPreset = name;
  zTop = 10;
  PANELS.forEach((panel, index) => { layout[panel.id].z = 10 + index; });
  saveLayout();
  buildBoard();
  buildPanelsMenu();
  if (layout.prediction.visible) refreshProjection(player.lap || 1);
  if (player.bounds) { fitMap(); renderFrame(player.time); }
}

const LAYOUT_KEY = "f1dash.layout.v4";
const BY_ID = Object.fromEntries(PANELS.map((panel) => [panel.id, panel]));

/** A floating panel is one the viewer summoned over the board rather than one
 *  docked on it, so it sits above the lot -- otherwise clicking the map (its
 *  play button counts) would bring the map over the prediction that the map's
 *  own button had just opened. */
const FLOAT_LIFT = 1000;   /* matches --z-float */
const zOf = (id) => layout[id].z + (BY_ID[id].float ? FLOAT_LIFT : 0);

let layout = loadLayout();
let zTop = 10;

function loadLayout() {
  const base = {};
  PANELS.forEach((panel, index) => {
    base[panel.id] = {
      c: panel.c, r: panel.r, cw: panel.cw, ch: panel.ch,
      z: 10 + index, visible: !panel.off,
    };
  });
  try {
    const saved = JSON.parse(localStorage.getItem(LAYOUT_KEY) || "null");
    if (saved && saved.panels) {
      currentPreset = saved.preset || DEFAULT_PRESET;
      for (const id of Object.keys(base)) Object.assign(base[id], saved.panels[id] || {});
    }
  } catch (_) { /* a corrupt layout is not worth a broken page */ }
  base.map.visible = true;  // the circuit is the page
  return base;
}

function saveLayout() {
  try {
    localStorage.setItem(LAYOUT_KEY,
      JSON.stringify({ preset: currentPreset, panels: layout }));
  } catch (_) {}
}

/** Panel bodies are built once and moved around, never re-rendered on layout
 *  changes: a re-render would lose scroll position and the playing map. */
const bodies = {};
const widgets = {};

function boardSize() {
  const wrap = el("boardWrap");
  return { W: Math.max(wrap.clientWidth, 1), H: Math.max(wrap.clientHeight, 1) };
}

/** One cell, in pixels. */
function cell() {
  const { W, H } = boardSize();
  return { w: W / GRID_COLS, h: H / GRID_ROWS };
}

const clampCell = (value, span, max) => Math.max(0, Math.min(value, max - span));

/** Write a panel's cell rectangle out as pixels. A gutter is taken out of the
 *  cell rather than added between panels, so two panels in adjacent cells are
 *  exactly one gutter apart however they are placed. */
const GUTTER = 10;

function place(id) {
  const widget = widgets[id];
  const state = layout[id];
  if (!widget || !state) return;
  const { w, h } = cell();

  if (BY_ID[id].pinned) { state.c = 0; state.r = 0; state.ch = GRID_ROWS; }
  state.cw = Math.max(MIN_CW, Math.min(state.cw, GRID_COLS));
  state.ch = Math.max(MIN_CH, Math.min(state.ch, GRID_ROWS));
  state.c = clampCell(state.c, state.cw, GRID_COLS);
  state.r = clampCell(state.r, state.ch, GRID_ROWS);

  widget.style.left = `${(state.c * w + GUTTER / 2).toFixed(1)}px`;
  widget.style.top = `${(state.r * h + GUTTER / 2).toFixed(1)}px`;
  widget.style.width = `${(state.cw * w - GUTTER).toFixed(1)}px`;
  widget.style.height = `${(state.ch * h - GUTTER).toFixed(1)}px`;
  widget.style.zIndex = zOf(id);
}

function placeAll() {
  for (const id of Object.keys(widgets)) place(id);
  if (player.bounds) { fitMap(); renderFrame(player.time); }
}

function bringToFront(id) {
  layout[id].z = ++zTop;
  if (widgets[id]) widgets[id].style.zIndex = zOf(id);
  for (const [other, node] of Object.entries(widgets)) node.classList.toggle("active", other === id);
}

/** Fill the board with one panel, or give it back the cells it came from. */
function toggleFull(id) {
  const state = layout[id];
  if (state.restore) {
    Object.assign(state, state.restore);
    state.restore = null;
  } else {
    state.restore = { c: state.c, r: state.r, cw: state.cw, ch: state.ch };
    Object.assign(state, { c: 0, r: 0, cw: GRID_COLS, ch: GRID_ROWS });
  }
  bringToFront(id);
  place(id);
  saveLayout();
  if (id === "map" && player.bounds) { fitMap(); renderFrame(player.time); }
}

/** Give a panel one of the named sizes, keeping its top-left corner. */
function setSize(id, cw, ch) {
  const state = layout[id];
  state.cw = Math.max(MIN_CW, Math.min(cw, GRID_COLS));
  state.ch = Math.max(MIN_CH, Math.min(ch, GRID_ROWS));
  state.restore = null;
  bringToFront(id);
  place(id);
  saveLayout();
  if (id === "map" && player.bounds) { fitMap(); renderFrame(player.time); }
}

function setPanelVisible(id, visible) {
  layout[id].visible = visible;
  if (visible) layout[id].z = ++zTop;
  saveLayout();
  buildBoard();
  buildPanelsMenu();
}

function buildBoard() {
  const board = el("board");
  board.textContent = "";
  for (const key of Object.keys(widgets)) delete widgets[key];

  zTop = Math.max(zTop, ...PANELS.map((panel) => layout[panel.id].z || 10));

  for (const panel of PANELS) {
    const state = layout[panel.id];
    if (!state.visible) continue;

    const widget = document.createElement("section");
    widget.className = "widget"
      + (panel.float ? " floating" : "")
      + (panel.pinned ? " pinned" : "");
    widget.dataset.panel = panel.id;

    const head = document.createElement("div");
    head.className = "w-head";
    head.innerHTML = `<span class="grip">&#10287;</span><h2>${panel.title}</h2><span class="spacer"></span>`;
    head.append(headExtras(panel.id));
    if (!panel.pinned) head.append(sizeMenu(panel.id));
    if (!panel.always) {
      const close = document.createElement("button");
      close.className = "ghost";
      close.textContent = "✕";
      close.title = "put this panel away";
      close.addEventListener("click", (event) => {
        event.stopPropagation();
        setPanelVisible(panel.id, false);
      });
      head.append(close);
    }

    const body = bodies[panel.id] || (bodies[panel.id] = document.createElement("div"));
    body.className = "w-body";

    widget.append(head, body);
    if (panel.id === "map") widget.append(transport());

    // A pinned panel keeps only the edge that divides it from its neighbour.
    const grips = panel.pinned ? ["e"] : ["n", "s", "w", "e", "nw", "ne", "sw", "se"];
    for (const direction of grips) {
      const handle = document.createElement("div");
      handle.className = `w-handle ${direction}`;
      widget.append(handle);
      makeResizable(widget, handle, panel.id, direction);
    }

    board.append(widget);
    widgets[panel.id] = widget;
    place(panel.id);

    if (!panel.pinned) makeDraggable(widget, head, panel.id);
    head.addEventListener("dblclick", (event) => {
      if (event.target.closest("button") || panel.pinned) return;
      toggleFull(panel.id);
    });
    widget.addEventListener("pointerdown", () => bringToFront(panel.id), true);
  }
  if (bodies.notify) renderNotices();
  restoreChrome();
}

/** buildBoard() replaces every header and the transport bar with fresh markup,
 *  which drops the title, the flag, the lap, the clock and the scrub with it.
 *  The panel bodies are carried over untouched; this puts the chrome back. */
function restoreChrome() {
  if (mapTitle) setTitle("map", mapTitle);
  if (typeof player === "undefined") return;

  if (player.bundle) {
    const scrub = el("scrub");
    if (scrub) scrub.max = String(Math.round(player.bundle.duration_s));
    if (el("flagStrip")) el("flagStrip").style.background = flagGradient(player.bundle);
    if (el("flagChip")) el("flagChip").hidden = false;
    renderOrder(player.lap || 1);
    setPlayable(player.framesReady !== false);
    renderFrame(player.time);      // clock, scrub, lap, flag, and both pills
    setPlaying(player.playing);
  } else {
    if (el("flagChip")) el("flagChip").hidden = true;
    if (el("orderLap")) el("orderLap").textContent = player.upcoming ? "provisional" : "—";
    renderRaceControl(0);
  }
}

/** The size control: the panel's own dimensions, and the sizes it can be. */
function sizeMenu(id) {
  const wrap = document.createElement("span");
  wrap.className = "size-menu";

  const button = document.createElement("button");
  button.className = "ghost size-btn";
  button.title = "resize this panel";
  button.textContent = `${layout[id].cw}×${layout[id].ch}`;

  const list = document.createElement("div");
  list.className = "size-list";
  list.hidden = true;
  for (const size of SIZES) {
    const option = document.createElement("button");
    const current = layout[id].cw === size.cw && layout[id].ch === size.ch;
    option.className = current ? "on" : "";
    option.innerHTML = `<span></span><em>${size.cw}×${size.ch}</em>`;
    option.querySelector("span").textContent = size.label;
    option.addEventListener("click", (event) => {
      event.stopPropagation();
      list.hidden = true;
      setSize(id, size.cw, size.ch);
      button.textContent = `${layout[id].cw}×${layout[id].ch}`;
    });
    list.append(option);
  }

  button.addEventListener("click", (event) => {
    event.stopPropagation();
    const opening = list.hidden;
    document.querySelectorAll(".size-list").forEach((other) => { other.hidden = true; });
    list.hidden = !opening;
  });
  wrap.append(button, list);
  return wrap;
}

document.addEventListener("click", () => {
  document.querySelectorAll(".size-list").forEach((list) => { list.hidden = true; });
});

/** Per-panel controls that live in the panel's own header. */
function headExtras(id) {
  const wrap = document.createElement("span");
  wrap.style.cssText = "display:flex;align-items:center;gap:8px";
  if (id === "map") {
    // The prediction is asked for from the top right of the circuit itself.
    wrap.innerHTML = '<span class="flag green" id="flagChip" hidden>green</span>';
    const ask = document.createElement("button");
    ask.id = "predictionBtn";
    ask.className = "accent" + (layout.prediction.visible ? " on" : "");
    ask.textContent = "Prediction";
    ask.title = "what the model makes of this lap";
    ask.addEventListener("click", (event) => {
      event.stopPropagation();
      const showing = !layout.prediction.visible;
      setPanelVisible("prediction", showing);
      if (showing) refreshProjection(player.lap || 1);
    });
    wrap.append(ask);
  } else if (id === "prediction") {
    const again = document.createElement("button");
    again.className = "ghost";
    again.textContent = "Ask again";
    again.addEventListener("click", (event) => {
      event.stopPropagation();
      refreshProjection(player.lap || 1, { fresh: true });
    });
    wrap.append(again);
  } else if (id === "control") {
    wrap.innerHTML = '<span class="pill" id="rcCount">&mdash;</span>';
  } else if (id === "order") {
    wrap.innerHTML = '<span class="pill" id="orderLap">&mdash;</span>';
  } else if (id === "notify") {
    wrap.innerHTML = '<span class="pill" id="notifCount">0</span>';
    const clear = document.createElement("button");
    clear.className = "ghost";
    clear.textContent = "Clear";
    clear.addEventListener("click", (event) => {
      event.stopPropagation();
      notices.length = 0;
      renderNotices();
    });
    wrap.append(clear);
  }
  return wrap;
}

function transport() {
  const bar = document.createElement("div");
  bar.className = "transport";
  bar.innerHTML = `
    <button id="playBtn" class="primary">&#9654;</button>
    <span class="lapcount" id="lapCount">lap &mdash;</span>
    <div class="scrubwrap">
      <input type="range" id="scrub" min="0" max="100" value="0" step="1" />
      <div id="flagStrip" title="flag state through the race"></div>
    </div>
    <span class="clock" id="clock">0:00 / 0:00</span>`;
  bar.querySelector("#playBtn").addEventListener("click", () => setPlaying(!player.playing));
  bar.querySelector("#scrub").addEventListener("input", (event) => {
    player.time = Number(event.target.value);
    renderFrame(player.time);
  });
  // Dragging the transport should scrub, not pick the panel up.
  bar.addEventListener("pointerdown", (event) => event.stopPropagation());
  return bar;
}

/* -- the grid, while it is being used ------------------------------------
   The grid is invisible until somebody is placing something against it, which
   is the only time it is information rather than decoration. */

function showGrid(on) {
  el("board").classList.toggle("gridded", on);
}

/** Where a panel would land, shown as an outline in the cells it would take. */
function ghost(id, c, r, cw, ch) {
  let node = el("ghost");
  if (!node) {
    node = document.createElement("div");
    node.id = "ghost";
    el("board").append(node);
  }
  const { w, h } = cell();
  node.style.left = `${(c * w + GUTTER / 2).toFixed(1)}px`;
  node.style.top = `${(r * h + GUTTER / 2).toFixed(1)}px`;
  node.style.width = `${(cw * w - GUTTER).toFixed(1)}px`;
  node.style.height = `${(ch * h - GUTTER).toFixed(1)}px`;
  node.hidden = false;
}

function hideGhost() {
  const node = el("ghost");
  if (node) node.hidden = true;
}

/* -- move -----------------------------------------------------------------
   The panel follows the pointer exactly, because anything else feels like
   lag, while an outline shows the cells it will actually take. On release it
   settles into them. That split -- the thing under your finger is free, the
   thing it will become is snapped -- is what makes an icon grid feel good
   rather than sticky. */

function makeDraggable(widget, handle, id) {
  handle.addEventListener("pointerdown", (event) => {
    if (event.target.closest("button")) return;
    if (window.innerWidth <= 900) return;   // stacked on narrow screens
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    bringToFront(id);
    widget.classList.add("dragging");
    showGrid(true);

    const { w, h } = cell();
    const startX = event.clientX, startY = event.clientY;
    const originLeft = layout[id].c * w + GUTTER / 2;
    const originTop = layout[id].r * h + GUTTER / 2;
    let target = { c: layout[id].c, r: layout[id].r };

    const move = (moveEvent) => {
      const left = originLeft + moveEvent.clientX - startX;
      const top = originTop + moveEvent.clientY - startY;
      widget.style.left = `${left.toFixed(1)}px`;
      widget.style.top = `${top.toFixed(1)}px`;
      target = {
        c: clampCell(Math.round(left / w), layout[id].cw, GRID_COLS),
        r: clampCell(Math.round(top / h), layout[id].ch, GRID_ROWS),
      };
      ghost(id, target.c, target.r, layout[id].cw, layout[id].ch);
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
      widget.classList.remove("dragging");
      widget.classList.add("settling");
      setTimeout(() => widget.classList.remove("settling"), 340);
      showGrid(false);
      hideGhost();
      layout[id].c = target.c;
      layout[id].r = target.r;
      place(id);
      saveLayout();
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  });
}

/* -- resize ---------------------------------------------------------------
   Whole cells only, so an edge cannot come to rest a few pixels off a line.
   The edge being dragged is shared with whatever is on the other side of it:
   two panels either side of a gutter are reading as a split, not as two things
   that happen to be near each other, so widening one narrows the other. */

function neighboursOn(id, direction) {
  const mine = layout[id];
  const found = [];
  for (const [other, state] of Object.entries(layout)) {
    if (other === id || !state.visible || BY_ID[other].pinned) continue;
    const overlapRows = Math.min(mine.r + mine.ch, state.r + state.ch) - Math.max(mine.r, state.r);
    const overlapCols = Math.min(mine.c + mine.cw, state.c + state.cw) - Math.max(mine.c, state.c);
    let touching = false;
    if (direction.includes("e")) touching = state.c === mine.c + mine.cw && overlapRows > 0;
    else if (direction.includes("w")) touching = state.c + state.cw === mine.c && overlapRows > 0;
    else if (direction.includes("s")) touching = state.r === mine.r + mine.ch && overlapCols > 0;
    else if (direction.includes("n")) touching = state.r + state.ch === mine.r && overlapCols > 0;
    if (touching) found.push({ id: other, c: state.c, r: state.r, cw: state.cw, ch: state.ch });
  }
  return found;
}

function makeResizable(widget, handle, id, direction) {
  handle.addEventListener("pointerdown", (event) => {
    if (window.innerWidth <= 900) return;
    event.preventDefault();
    event.stopPropagation();
    handle.setPointerCapture(event.pointerId);
    bringToFront(id);
    widget.classList.add("sizing");
    showGrid(true);

    const { w, h } = cell();
    const startX = event.clientX, startY = event.clientY;
    const from = { c: layout[id].c, r: layout[id].r, cw: layout[id].cw, ch: layout[id].ch };
    const neighbours = neighboursOn(id, direction);
    // How far the edge may travel before a neighbour is squeezed out.
    const room = neighbours.length
      ? Math.min(...neighbours.map((n) => (
          direction.includes("e") || direction.includes("w") ? n.cw - MIN_CW : n.ch - MIN_CH)))
      : Infinity;

    const move = (moveEvent) => {
      const dc = Math.round((moveEvent.clientX - startX) / w);
      const dr = Math.round((moveEvent.clientY - startY) / h);
      let { c, r, cw, ch } = from;

      if (direction.includes("e")) {
        cw = Math.max(MIN_CW, Math.min(GRID_COLS - c, from.cw + Math.min(dc, room)));
      }
      if (direction.includes("w")) {
        const edge = Math.max(0, Math.min(from.c + from.cw - MIN_CW, from.c + Math.max(dc, -room)));
        cw = from.c + from.cw - edge; c = edge;
      }
      if (direction.includes("s")) {
        ch = Math.max(MIN_CH, Math.min(GRID_ROWS - r, from.ch + Math.min(dr, room)));
      }
      if (direction.includes("n")) {
        const edge = Math.max(0, Math.min(from.r + from.ch - MIN_CH, from.r + Math.max(dr, -room)));
        ch = from.r + from.ch - edge; r = edge;
      }

      Object.assign(layout[id], { c, r, cw, ch });
      place(id);
      ghost(id, c, r, cw, ch);

      // The edge belongs to the panel on the other side of it too.
      const shiftC = direction.includes("e") ? cw - from.cw
        : direction.includes("w") ? from.c - c : 0;
      const shiftR = direction.includes("s") ? ch - from.ch
        : direction.includes("n") ? from.r - r : 0;

      for (const other of neighbours) {
        const state = layout[other.id];
        if (direction.includes("e")) { state.c = other.c + shiftC; state.cw = other.cw - shiftC; }
        else if (direction.includes("w")) { state.cw = other.cw - shiftC; }
        else if (direction.includes("s")) { state.r = other.r + shiftR; state.ch = other.ch - shiftR; }
        else if (direction.includes("n")) { state.ch = other.ch - shiftR; }
        place(other.id);
      }

      if ((id === "map" || neighbours.some((n) => n.id === "map")) && player.bounds) fitMap();
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
      widget.classList.remove("sizing");
      showGrid(false);
      hideGhost();
      saveLayout();
      const button = widget.querySelector(".size-btn");
      if (button) button.textContent = `${layout[id].cw}×${layout[id].ch}`;
      if (player.bundle) renderFrame(player.time);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  });
}

window.addEventListener("resize", placeAll);

/* -- the panels menu --------------------------------------------------- */

function buildPanelsMenu() {
  const menu = el("panelsMenu");
  menu.textContent = "";
  for (const panel of PANELS) {
    if (panel.always) continue;
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = layout[panel.id].visible;
    box.addEventListener("change", () => {
      setPanelVisible(panel.id, box.checked);
      if (panel.id === "prediction" && box.checked) refreshProjection(player.lap || 1);
    });
    label.append(box, document.createTextNode(panel.title));
    menu.append(label);
  }
  const heading = document.createElement("div");
  heading.className = "menu-heading";
  heading.textContent = "Arrangements";
  menu.append(document.createElement("hr"), heading);

  for (const [name, preset] of Object.entries(PRESETS)) {
    const option = document.createElement("button");
    option.className = "preset" + (name === currentPreset ? " on" : "");
    option.innerHTML = `<b></b><small></small>`;
    option.querySelector("b").textContent = preset.label;
    option.querySelector("small").textContent = preset.hint;
    option.addEventListener("click", (event) => {
      event.stopPropagation();
      applyPreset(name);
      el("panelsMenu").hidden = true;
    });
    menu.append(option);
  }

  const hint = document.createElement("div");
  hint.style.cssText = "padding:8px 9px 4px;color:var(--dim);font-size:11.5px;line-height:1.45";
  hint.textContent = "Drag a panel by its header to move it; drag an edge to resize it and the "
    + "panel beside it. The circuit stays on the left.";
  menu.append(document.createElement("hr"), hint);
}

el("panelsBtn").addEventListener("click", (event) => {
  event.stopPropagation();
  el("panelsMenu").hidden = !el("panelsMenu").hidden;
});
document.addEventListener("click", () => { el("panelsMenu").hidden = true; });
el("panelsMenu").addEventListener("click", (event) => event.stopPropagation());

el("resetLayout").addEventListener("click", () => applyPreset(currentPreset));

/* -- keyboard ------------------------------------------------------------
   The transport, without having to find the scrub bar with a pointer. Typing
   in the search box is still typing, so the keys are ignored there. */

document.addEventListener("keydown", (event) => {
  const target = event.target;
  if (target && target.matches && target.matches("input, select, textarea")) return;
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (!player.bundle) return;

  const step = event.shiftKey ? 60 : 10;
  const seek = (to) => {
    player.time = Math.min(Math.max(to, 0), player.bundle.duration_s);
    renderFrame(player.time);
  };

  switch (event.key) {
    case " ":       event.preventDefault(); setPlaying(!player.playing); break;
    case "ArrowLeft":  event.preventDefault(); seek(player.time - step); break;
    case "ArrowRight": event.preventDefault(); seek(player.time + step); break;
    case "Home":    event.preventDefault(); seek(0); break;
    case "End":     event.preventDefault(); seek(player.bundle.duration_s); break;
    case "p": case "P": {
      event.preventDefault();
      const showing = !layout.prediction.visible;
      setPanelVisible("prediction", showing);
      if (showing) refreshProjection(player.lap || 1);
      break;
    }
    default: break;
  }
});

export {
  BY_ID,
  DEFAULT_PRESET,
  FLOAT_LIFT,
  GRID_COLS,
  GRID_ROWS,
  GUTTER,
  LAYOUT_KEY,
  MIN_CW,
  PANELS,
  PRESETS,
  SIZES,
  applyPreset,
  boardSize,
  bodies,
  bringToFront,
  buildBoard,
  buildPanelsMenu,
  cell,
  clampCell,
  currentPreset,
  ghost,
  headExtras,
  hideGhost,
  layout,
  loadLayout,
  makeDraggable,
  makeResizable,
  neighboursOn,
  place,
  placeAll,
  restoreChrome,
  saveLayout,
  setPanelVisible,
  setSize,
  showGrid,
  sizeMenu,
  toggleFull,
  transport,
  widgets,
  zOf,
  zTop,
};
