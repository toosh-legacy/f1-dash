/* Notifications, the island that shows them, and the event feed.
 *
 * Two things are worth interrupting a viewer for: a call from race control,
 * and a car changing hands. Both land in the panel, which keeps them, and in
 * a card in the corner, which does not.
 */

import { bodies } from "./board.js";
import { el, when } from "./core.js";
import { SEEK_JUMP_S, player } from "./map.js";

const notices = [];
const MAX_NOTICES = 80;

/** Race control talks constantly, and most of it is housekeeping: a blue flag
 *  for a car about to be lapped, and the per-sector yellow-then-clear chatter
 *  that follows every incident. A Zandvoort race carries 333 calls, of which
 *  248 are these. They all stay in the race control panel, which is the full
 *  log; none of them earns an interruption.
 *
 *  Which is which is decided by the backend and carried on the message, so a
 *  live call and a replayed one are judged by the same rule. The pattern below
 *  is only for a bundle built before that field existed. */
const RC_HOUSEKEEPING = /WAVED BLUE FLAG|IN TRACK SECTOR/i;
const worthAnnouncing = (event) => (
  event.severity
    ? event.severity !== "housekeeping"
    : !RC_HOUSEKEEPING.test(event.message || "")
);

function notify(kind, title, detail, stamp) {
  // The same call twice running is one thing that happened, not two.
  const newest = notices[0];
  if (newest && newest.kind === kind && newest.title === title && newest.detail === detail) return;
  notices.unshift({ kind, title, detail, stamp: stamp || "" });
  while (notices.length > MAX_NOTICES) notices.pop();
  renderNotices();
  toast(kind, title, detail, stamp);
}

function clearNotices() {
  notices.length = 0;
  renderNotices();
}

function renderNotices() {
  const container = bodies.notify;
  if (el("notifCount")) el("notifCount").textContent = String(notices.length);
  if (!container) return;
  if (!notices.length) {
    container.innerHTML = '<div class="empty">Nothing yet. Overtakes and race control land here.</div>';
    return;
  }
  container.textContent = "";
  for (const notice of notices) {
    const row = document.createElement("div");
    row.className = `notif ${notice.kind}`;
    row.innerHTML = `<span class="edge"></span>
      <span class="txt">
        <span class="when">${notice.stamp}</span>
        <b>${notice.title}</b>
        <small title="${(notice.detail || "").replace(/"/g, "&quot;")}">${notice.detail || ""}</small>
      </span>`;
    container.append(row);
  }
}

/* -- the island -----------------------------------------------------------
   Everything the page announces comes through here: the replay's own events,
   race control, and whatever a live session pushes. One object, one place, so
   a viewer learns to read it once and never has to dismiss anything.

   How long it speaks depends on what it says. A call from race control changes
   how the race is being run and holds for ten seconds; a car changing hands is
   worth five. Anything arriving while it is already speaking queues behind. */

const ISLAND_LIFE_MS = { control: 10000, drop: 8000, pass: 5000 };

const island = {
  queue: [],
  speaking: false,
  timer: null,
  held: false,
};

function islandIdleContent() {
  if (!player.bundle || !player.bundle.frames || !player.bundle.frames.length) {
    return '<span class="isl-dot"></span><span class="isl-idle">F1 2026</span>';
  }
  const flag = player.flag || "green";
  const total = player.bundle.total_laps || 0;
  const lap = player.lap || 0;
  return `<span class="isl-dot ${flag}"></span>`
    + `<span class="isl-idle">${flag.replace("_", " ")} · `
    + `<b>lap ${lap}${total ? ` / ${total}` : ""}</b></span>`;
}

/** Measure what the island would have to become, then become it. */
function islandSet(html, { kind, life } = {}) {
  const node = el("island");
  const inner = el("islandInner");
  const sizer = el("islandSizer");
  if (!node || !inner || !sizer) return;

  sizer.innerHTML = html;
  const width = Math.ceil(sizer.offsetWidth) + 2;
  const height = Math.max(34, Math.ceil(sizer.offsetHeight) + 2);

  inner.classList.add("swap");
  setTimeout(() => {
    inner.innerHTML = html;
    node.style.width = `${width}px`;
    node.style.height = `${height}px`;
    node.classList.toggle("speaking", Boolean(kind));
    inner.classList.remove("swap");

    const life = node.querySelector("#islandLife");
    if (life) life.remove();
  }, 130);

  if (kind && life) {
    const bar = document.createElement("i");
    bar.id = "islandLife";
    bar.className = kind;
    node.append(bar);
    requestAnimationFrame(() => {
      bar.style.transition = "none";
      bar.style.transform = "scaleX(1)";
      requestAnimationFrame(() => {
        bar.style.transition = `transform ${life}ms linear`;
        bar.style.transform = "scaleX(0)";
      });
    });
  }
}

function islandIdle() {
  island.speaking = false;
  islandSet(islandIdleContent());
}

/** Refresh the resting state without disturbing anything being said. */
function islandTick() {
  if (island.speaking || island.queue.length) return;
  const inner = el("islandInner");
  if (!inner || inner.classList.contains("swap")) return;
  const next = islandIdleContent();
  if (inner.dataset.idle === next) return;
  inner.dataset.idle = next;
  islandSet(next);
}

function toast(kind, title, detail, stamp) {
  island.queue.push({ kind, title, detail, stamp });
  if (!island.speaking) islandNext();
}

function islandNext() {
  const item = island.queue.shift();
  if (!item) { islandIdle(); return; }

  island.speaking = true;
  const life = ISLAND_LIFE_MS[item.kind] || 6000;
  const detail = item.detail
    ? `<small>${escapeText(item.detail)}</small>` : "";
  islandSet(
    `<span class="isl-dot ${item.kind}"></span>`
    + `<span class="isl-text"><b>${escapeText(item.title)}</b>${detail}</span>`
    + (item.stamp ? `<span class="isl-stamp">${escapeText(item.stamp)}</span>` : ""),
    { kind: item.kind, life },
  );

  clearTimeout(island.timer);
  island.timer = setTimeout(() => {
    if (island.held) return;              // the pointer is resting on it
    islandNext();
  }, life);
}

function escapeText(value) {
  const node = document.createElement("span");
  node.textContent = String(value ?? "");
  return node.innerHTML;
}

/* The resting state is also refreshed on a slow timer, not only from the
   render loop: a tick that lands while the island is mid-transition has to be
   skipped, and when the race is paused there is no next frame to retry on. */
setInterval(islandTick, 500);

/* Resting a pointer on the island holds whatever it is saying. */
el("island").addEventListener("pointerenter", () => {
  island.held = true;
  const bar = document.getElementById("islandLife");
  if (bar) {
    const current = getComputedStyle(bar).transform;
    bar.style.transition = "none";
    bar.style.transform = current;
  }
});
el("island").addEventListener("pointerleave", () => {
  island.held = false;
  if (island.speaking) { clearTimeout(island.timer); island.timer = setTimeout(islandNext, 1400); }
});
el("island").addEventListener("click", () => {
  if (island.speaking) { clearTimeout(island.timer); island.held = false; islandNext(); }
});

/** Overtakes, as the playhead reaches them.
 *
 *  These are no longer inferred in the browser. The backend reads them out of
 *  the position feed at one-second resolution -- which per-lap order rows
 *  cannot do -- excludes the pit cycle using the stop count, and folds "the
 *  whole field went past one car" into the one thing it actually was: a car in
 *  trouble. Every viewer therefore sees the same events, at the moment they
 *  happened rather than at the lap boundary that followed. */
let eventsSeen = 0;

/** Mark the feed as read up to a moment in the race.
 *
 * The map calls this when a viewer jumps somewhere: everything before the new
 * position has, as far as the feed is concerned, already been seen.
 */
export function seenUpTo(time) {
  eventsSeen = time;
}

function renderEvents(upTo) {
  const bundle = player.bundle;
  const events = (bundle && bundle.events) || [];
  if (upTo < eventsSeen) { eventsSeen = upTo; return; }   // scrubbed backwards
  if (upTo - eventsSeen > SEEK_JUMP_S) { eventsSeen = upTo; return; }   // seeked past
  if (!events.length) { eventsSeen = upTo; return; }

  const drivers = new Map((bundle.drivers || []).map((driver) => [driver.number, driver]));
  const name = (number) => {
    const driver = drivers.get(number);
    return (driver && driver.code) || number;
  };

  for (const event of events) {
    if (event.t <= eventsSeen || event.t > upTo) continue;
    if (event.kind === "drop") {
      notify("drop", `${name(event.car)} drops to P${event.position}`,
        `passed by ${event.passed_by} cars, from P${event.from_position}`, `L${event.lap}`);
    } else {
      notify("pass", `${name(event.car)} takes P${event.position}`,
        `past ${name(event.over)}`, `L${event.lap}`);
    }
  }
  eventsSeen = upTo;
}

/** Who gained places between one lap and the next, inferred from the per-lap
 *  order alone.
 *
 *  Superseded by the bundle's own events, which the backend reads out of the
 *  position feed with real timestamps. This remains for a bundle built before
 *  that existed, which carries no ``events`` array: a car that finished the lap
 *  ahead of one it started behind, with the pit cycle excluded by stop count. */
function reportOvertakes(fromLap, toLap) {
  const laps = (player.bundle && player.bundle.laps) || {};
  const before = laps[String(fromLap)], after = laps[String(toLap)];
  if (!before || !after) return;

  const was = new Map(before.map((row) => [row.number, row.position]));
  const stopsWas = new Map(before.map((row) => [row.number, row.stops || 0]));
  const pitted = (row) => (row.stops || 0) > (stopsWas.get(row.number) || 0);
  const drivers = new Map(player.bundle.drivers.map((driver) => [driver.number, driver]));

  const gained = after
    .filter((row) => was.has(row.number) && was.get(row.number) > row.position && !pitted(row))
    .sort((a, b) => (was.get(b.number) - b.position) - (was.get(a.number) - a.position))
    .slice(0, 3);

  for (const row of gained) {
    const startedAt = was.get(row.number);
    // The car it passed: the closest one it started behind, finished ahead of,
    // and which did not simply hand the place over on its way into the pits.
    const passed = after
      .filter((other) => was.has(other.number) && !pitted(other)
        && was.get(other.number) < startedAt && other.position > row.position)
      .sort((a, b) => a.position - b.position)[0];
    if (!passed) continue;

    const who = drivers.get(row.number) || {};
    const victim = drivers.get(passed.number) || {};
    notify("pass", `${who.code || row.number} takes P${row.position}`,
      `past ${victim.code || passed.number}, up from P${startedAt}`, `L${toLap}`);
  }
}

export {
  ISLAND_LIFE_MS,
  MAX_NOTICES,
  RC_HOUSEKEEPING,
  clearNotices,
  escapeText,
  island,
  islandIdle,
  islandIdleContent,
  islandNext,
  islandSet,
  islandTick,
  notices,
  notify,
  renderEvents,
  renderNotices,
  reportOvertakes,
  toast,
  worthAnnouncing,
};
