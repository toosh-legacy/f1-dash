/* The live session, when there is one.
 *
 * Watching live is opening the server's snapshot of the session -- drivers,
 * circuit, the last few minutes of frames, timing -- and then keeping it
 * growing from one socket: car positions every few seconds, timing every
 * twenty, the running order once a lap, race control as it happens.
 *
 * The Live button in the top bar says whether any of that is possible right
 * now: following a session, nothing on, or a session on that OpenF1 will not
 * show without an account.
 */

import { api, el, endpoint, setStatus } from "./core.js";
import { appendLiveFrames, openLive, player, setLiveTrack, updateLiveTiming } from "./map.js";
import { renderTower } from "./tower.js";
import { notify, worthAnnouncing } from "./notices.js";

let socket = null;
let socketFor = null;
let retryTimer = null;

function logLine(message) {
  // The feed panel is gone from the page; the console keeps the log.
  console.debug(`[live] ${new Date().toLocaleTimeString()}  ${message}`);
}

/** Open a session that is running now and follow it. */
async function watchLive(sessionId) {
  setStatus("joining the session…");
  let state;
  try {
    state = await api(`/sessions/${sessionId}/live/state`);
  } catch (error) {
    setStatus("the live feed is not running", "error");
    return false;
  }
  openLive(state);
  connectSocket(sessionId);
  paintButton(liveStatus);
  return true;
}

/** Follow a session's socket. Reconnects on its own while the session is the
 *  one on screen: a dropped socket mid-race should not need a reload. */
function connectSocket(sessionId) {
  clearTimeout(retryTimer);
  if (socket) { socketFor = null; socket.close(); }
  setStatus("connecting…");
  socketFor = sessionId;
  const ws = new WebSocket(`${endpoint.ws}/sessions/${sessionId}/live`);
  socket = ws;
  ws.addEventListener("open", () => setStatus("live", "live"));
  ws.addEventListener("close", () => {
    if (socket !== ws || socketFor !== sessionId) return;
    setStatus("reconnecting…");
    retryTimer = setTimeout(() => {
      if (player.live && player.sessionId === sessionId) connectSocket(sessionId);
    }, 3000);
  });
  ws.addEventListener("error", () => setStatus("socket error", "error"));
  ws.addEventListener("message", (event) => handle(JSON.parse(event.data)));
}

function handle(message) {
  switch (message.type) {
    case "connected":
      logLine(`connected · regime ${message.regs_regime}`);
      break;
    case "live_frames":
      appendLiveFrames(message.frames || []);
      break;
    case "live_timing":
      updateLiveTiming(message);
      break;
    case "live_track":
      setLiveTrack(message.track);
      break;
    case "live_session":
      logLine(`session ${message.session && message.session.name}`);
      break;
    case "qualifying_update":
      logLine(`qualifying ${message.period} · ${message.predictions.length} cars`);
      break;
    case "race_update": {
      logLine(`race lap ${message.lap_number}${message.is_gated ? " (gated)" : ""}`);
      // The order comes in the same shape the bundle stores a lap in, so a
      // live race draws through the replay's own path.
      if (message.order && message.order.length && player.bundle) {
        player.bundle.laps = player.bundle.laps || {};
        player.bundle.laps[String(message.lap_number)] = message.order;
        renderTower(Math.max(player.lap || message.lap_number, 1));
      }
      break;
    }
    case "race_control": {
      logLine(`race control: ${message.event_type} (lap ${message.lap_number ?? "–"})`);
      // Live, the timing feed carries race control onto the playhead with
      // everything else; announcing it here as well would say it twice.
      if (!player.live && worthAnnouncing(message)) {
        notify("control", message.event_type.replace("_", " ").toUpperCase(),
          message.message || `lap ${message.lap_number ?? "–"}`, "now");
      }
      break;
    }
    case "training_progress":
      logLine(`training: ${message.stage}`);
      break;
    default:
      logLine(`message: ${message.type}`);
  }
}

/* -- the Live button ------------------------------------------------------ */

let liveStatus = null;
let onLive = null;   // set by search.js: how to open the live session as a choice

const LABELS = {
  live: "Live",
  locked: "Live · login needed",
  idle: "Live",
  error: "Live",
};

function describe(status) {
  if (!status) return "checking for a live session…";
  const following = status.following && status.following.row;
  if (status.state === "live" && following) {
    return `Watch ${following.circuit} ${following.name} live`;
  }
  if (status.state === "locked") {
    return "A session is on now, but this server cannot read it. Copy the formula1.com "
      + "login-session cookie into backend/.env as F1TV_LOGIN_SESSION and restart the API.";
  }
  if (status.state === "error") return `Live check failed: ${status.message || "unknown"}`;
  const latest = status.latest_session;
  return latest
    ? `Nothing live right now. Last session: ${latest.circuit} ${latest.name}.`
    : "Nothing live right now.";
}

function paintButton(status) {
  const button = el("liveBtn");
  if (!button) return;
  const state = (status && status.state) || "idle";
  const watching = player.live && status && status.following
    && status.following.session_id === player.sessionId;
  button.dataset.state = state;
  button.classList.toggle("watching", !!watching);
  button.querySelector(".lbl").textContent = LABELS[state] || "Live";
  button.title = describe(status);
  button.setAttribute("aria-label", describe(status));
}

async function refreshLiveStatus({ check = false } = {}) {
  try {
    liveStatus = await api(check ? "/live/check" : "/live/status", check ? { method: "POST" } : undefined);
  } catch (error) {
    // A read-only deployment refuses the check; the plain status still answers.
    liveStatus = check && error.status === 403 ? await api("/live/status").catch(() => null) : null;
  }
  paintButton(liveStatus);
  return liveStatus;
}

/** What the button does: watch the live session, or say why there is none. */
async function pressLive() {
  const button = el("liveBtn");
  button.disabled = true;
  const status = await refreshLiveStatus({ check: true });
  button.disabled = false;
  const following = status && status.following;
  if (status && status.state === "live" && following) {
    if (onLive) onLive(following);
    else await watchLive(following.session_id);
    paintButton(status);
    return;
  }
  showHint(status);
}

let hintTimer = null;
function showHint(status) {
  const hint = el("liveHint");
  const locked = status && status.state === "locked";
  hint.dataset.state = (status && status.state) || "idle";
  hint.innerHTML = "<b></b><span></span>";
  hint.querySelector("b").textContent = locked ? "F1 TV login needed" : "Nothing live right now";
  const text = hint.querySelector("span");
  if (locked) {
    // The server says why: no token, or one that has run out.
    text.innerHTML = "A session is on, but this server cannot read it. Copy the "
      + "<code>login-session</code> cookie from formula1.com into <code>backend/.env</code> as "
      + "<code>F1TV_LOGIN_SESSION</code> and restart the API.";
    if (status.message) text.append(" (" + status.message + ")");
  } else {
    text.textContent = describe(status);
  }
  hint.hidden = false;
  requestAnimationFrame(() => hint.classList.add("in"));
  clearTimeout(hintTimer);
  hintTimer = setTimeout(hideHint, locked ? 12000 : 5000);
}
function hideHint() {
  const hint = el("liveHint");
  hint.classList.remove("in");
  setTimeout(() => { if (!hint.classList.contains("in")) hint.hidden = true; }, 200);
}
document.addEventListener("click", (event) => {
  if (!event.target.closest || !event.target.closest("#liveHint, #liveBtn")) hideHint();
});

function setLiveHandler(handler) { onLive = handler; }

el("liveBtn").addEventListener("click", (event) => { event.stopPropagation(); pressLive(); });
// The server checks OpenF1 once a minute; the button keeps up with it.
setInterval(() => { if (!document.hidden) refreshLiveStatus(); }, 30000);

export {
  connectSocket,
  liveStatus,
  logLine,
  refreshLiveStatus,
  setLiveHandler,
  socket,
  watchLive,
};
