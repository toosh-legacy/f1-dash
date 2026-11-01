/* The race picker: one sheet, three kinds of race.
 *
 *   Live     a session running now -- followed over the socket
 *   Next     sessions still to come -- the circuit and a countdown
 *   Replay   every race that has run -- played back, built first if need be
 *
 * The sheet opens from the session title in the top bar, so the thing that
 * says what you are watching is the thing you press to watch something else.
 */

import { api, el, setStatus } from "./core.js";
import { connectSocket } from "./live.js";
import { openReplay, openUpcoming } from "./map.js";

let catalogue = null;
let upcoming = [];
let live = null;
let highlighted = -1;

async function loadRaces() {
  const [built, now] = await Promise.all([
    api("/replays").catch(() => []),
    api("/now").catch(() => null),
  ]);
  catalogue = [...built].sort((a, b) => new Date(b.date_start) - new Date(a.date_start));
  live = now && now.live && now.live.session_id ? now.live : null;

  // Sessions still to come are read from OpenF1's published schedule, which
  // runs to the end of the season; the local calendar is seeded and can stop
  // short of it.
  try {
    const sessions = await api("/schedule");
    upcoming = sessions.map((s) => ({
      upcoming: true,
      running: s.running,
      session_key: s.session_key,
      circuit: s.circuit,
      name: s.name,
      country: s.country,
      date_start: s.date_start,
      cached: false,
    }));
  } catch (_) { upcoming = []; }

  return { built, now };
}

function matches(round, query) {
  if (!query) return true;
  const haystack = [round.circuit, round.country, round.location, round.name,
    round.date_start ? new Date(round.date_start).toLocaleDateString(undefined,
      { month: "long", year: "numeric" }) : ""].join(" ").toLowerCase();
  return query.toLowerCase().split(/\s+/).every((word) => haystack.includes(word));
}

function countdownText(iso) {
  const remaining = Math.max(0, new Date(iso).getTime() - Date.now());
  const s = Math.floor(remaining / 1000);
  const d = Math.floor(s / 86400);
  const hh = String(Math.floor((s % 86400) / 3600)).padStart(2, "0");
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return `${d ? `${d}d ` : ""}${hh}:${mm}:${ss}`;
}

function renderResults() {
  const query = el("search").value.trim();
  const box = el("results");
  const groups = [];

  if (live && matches(live, query)) {
    groups.push({ title: "Live now", rows: [{ ...live, live: true }] });
  }
  const next = upcoming.filter((round) => matches(round, query)).slice(0, query ? 20 : 4);
  if (next.length) groups.push({ title: "Next up", rows: next });
  const ran = (catalogue || []).filter((round) => matches(round, query)).slice(0, 40);
  if (ran.length) groups.push({ title: "Replays", rows: ran });

  highlighted = -1;
  box.textContent = "";
  if (!groups.length) {
    box.innerHTML = '<div class="empty">Nothing matches that.</div>';
    return;
  }
  let order = 0;
  for (const group of groups) {
    const heading = document.createElement("div");
    heading.className = "group";
    heading.textContent = group.title;
    box.append(heading);
    for (const round of group.rows) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "result" + (round.live ? " is-live" : round.upcoming ? " is-next" : "");
      row.style.setProperty("--i", String(order++));
      const date = round.date_start ? new Date(round.date_start).toLocaleDateString(undefined,
        { day: "numeric", month: "short" }) : "";
      const badge = round.live ? '<span class="badge live"><i></i>Live</span>'
        : round.running ? '<span class="badge live"><i></i>On now</span>'
        : round.upcoming ?`<span class="badge soon" data-countdown="${round.date_start}">${countdownText(round.date_start)}</span>`
        : round.cached ? '<span class="badge ready">Replay</span>'
        : '<span class="badge">Build</span>';
      row.innerHTML = `<span class="r-main"><b></b><small></small></span><span class="r-date">${date}</span>${badge}`;
      row.querySelector("b").textContent = round.circuit || round.location || "Session";
      row.querySelector("small").textContent =
        `${round.name || ""}${round.country ? ` · ${round.country}` : ""}`;
      row.addEventListener("click", () => choose(round));
      box.append(row);
    }
  }
}

/* The countdowns in the sheet tick while it is open. */
setInterval(() => {
  if (el("picker").hidden) return;
  document.querySelectorAll("[data-countdown]").forEach((node) => {
    node.textContent = countdownText(node.dataset.countdown);
  });
}, 1000);

function setMode(mode) {
  const tag = el("modeTag");
  tag.textContent = mode;
  tag.className = `mode-tag ${mode.toLowerCase()}`;
}

async function choose(round) {
  closePicker();
  el("search").value = "";
  if (round.live) {
    setMode("Live");
    connectSocket(round.session_id);
    if (round.openf1_session_key) await openReplay(round.openf1_session_key, round).catch(() => {});
    return;
  }
  if (round.upcoming) { setMode("Next"); return openUpcoming(round); }
  setMode("Replay");
  if (round.cached) return openReplay(round.session_key, round);
  return buildThenOpen(round);
}

async function buildThenOpen(round) {
  setStatus(`building ${round.circuit}…`);
  try {
    const job = await api(`/replays/${round.session_key}/build`, { method: "POST" });
    for (;;) {
      const record = await api(`/jobs/${job.job_id}`);
      setStatus(`${record.stages[record.stages.length - 1] || "working"}…`);
      if (record.status === "succeeded") break;
      if (record.status === "failed") throw new Error(record.error || "build failed");
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
    round.cached = true;
    await openReplay(round.session_key, round);
    loadRaces();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

/* -- opening and closing the sheet ------------------------------------- */

/** Whether the sheet is meant to be open. The class and `hidden` trail it by
 *  a frame (in) or a transition (out), so they cannot be asked. */
let wantOpen = false;

function openPicker() {
  const sheet = el("picker");
  wantOpen = true;
  renderResults();
  sheet.hidden = false;
  el("pickerBtn").setAttribute("aria-expanded", "true");
  requestAnimationFrame(() => { if (wantOpen) sheet.classList.add("open"); });
  el("search").focus({ preventScroll: true });
}

function closePicker() {
  const sheet = el("picker");
  if (!wantOpen) return;
  wantOpen = false;
  sheet.classList.remove("open");
  el("pickerBtn").setAttribute("aria-expanded", "false");
  setTimeout(() => { if (!wantOpen) sheet.hidden = true; }, 220);
}

el("pickerBtn").addEventListener("click", (event) => {
  event.stopPropagation();
  if (wantOpen) closePicker(); else openPicker();
});
el("picker").addEventListener("click", (event) => event.stopPropagation());
document.addEventListener("click", closePicker);

el("search").addEventListener("input", renderResults);
el("search").addEventListener("keydown", (event) => {
  const rows = [...el("results").querySelectorAll(".result")];
  if (event.key === "Escape") { closePicker(); el("pickerBtn").focus(); return; }
  if (!rows.length) return;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    highlighted = (highlighted + (event.key === "ArrowDown" ? 1 : rows.length - 1)) % rows.length;
    rows.forEach((row, index) => row.classList.toggle("active", index === highlighted));
    rows[highlighted].scrollIntoView({ block: "nearest" });
  } else if (event.key === "Enter") {
    (rows[highlighted] || rows[0]).click();
  }
});

export { catalogue, choose, loadRaces, setMode, upcoming };
