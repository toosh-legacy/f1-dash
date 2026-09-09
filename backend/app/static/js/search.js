/* The finder: one list of races, past and future.
 *
 * A race that has run and been built plays; one that has run and has not is
 * built first; one still to come shows its circuit and a countdown.
 */

import { api, el, setStatus } from "./core.js";
import { openReplay, openUpcoming } from "./map.js";

let catalogue = null;
let upcoming = [];
let highlighted = -1;

async function loadRaces() {
  const [built, now] = await Promise.all([
    api("/replays").catch(() => []),
    api("/now").catch(() => null),
  ]);
  catalogue = built;
  if (now) {
    el("regime").textContent = `regime ${now.regs_regime} · season ${now.season}`;
  }

  // Sessions still to come are read from the calendar, not from the replay
  // catalogue, which only knows about sessions that have started.
  try {
    const sessions = await api("/sessions?year=" + (now ? now.season : new Date().getFullYear()));
    const moment = Date.now();
    upcoming = sessions
      .filter((s) => s.start_time && new Date(s.start_time).getTime() > moment)
      .map((s) => ({
        upcoming: true,
        session_id: s.id,
        circuit: s.circuit_id,
        name: s.session_type,
        country: "",
        date_start: s.start_time,
        cached: false,
      }))
      .sort((a, b) => new Date(a.date_start) - new Date(b.date_start));
  } catch (_) { upcoming = []; }

  el("seasonPill").textContent =
    `${(catalogue || []).filter((r) => r.cached).length} ready · ${upcoming.length} to come`;
  return { built, now };
}

function everyRace() {
  return [...upcoming, ...(catalogue || [])];
}

function matches(round, query) {
  if (!query) return true;
  const haystack = [round.circuit, round.country, round.location, round.name,
    round.date_start ? new Date(round.date_start).toLocaleDateString(undefined,
      { month: "long", year: "numeric" }) : ""].join(" ").toLowerCase();
  return query.toLowerCase().split(/\s+/).every((word) => haystack.includes(word));
}

function renderResults() {
  const query = el("search").value.trim();
  const box = el("results");
  const rounds = everyRace().filter((round) => matches(round, query)).slice(0, 40);
  highlighted = -1;
  if (!rounds.length) {
    box.innerHTML = '<div class="empty">Nothing matches that.</div>';
    box.hidden = false;
    return;
  }
  box.textContent = "";
  for (const round of rounds) {
    const row = document.createElement("div");
    row.className = "result";
    const date = new Date(round.date_start).toLocaleDateString(undefined,
      { day: "numeric", month: "short", year: "numeric" });
    const badge = round.upcoming ? '<span class="badge soon">to come</span>'
      : round.cached ? '<span class="badge ready">ready</span>'
      : '<span class="badge">not built</span>';
    row.innerHTML = `<span><b>${round.circuit}</b><small>${round.name}${round.country ? " · " + round.country : ""}</small></span>
      <span class="badge" style="border:none;background:none">${date}</span>${badge}`;
    row.addEventListener("click", () => choose(round));
    box.append(row);
  }
  box.hidden = false;
}

async function choose(round) {
  el("results").hidden = true;
  el("search").value = "";
  if (round.upcoming) return openUpcoming(round);
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

el("search").addEventListener("input", renderResults);
el("search").addEventListener("focus", renderResults);
el("search").addEventListener("keydown", (event) => {
  const rows = [...el("results").querySelectorAll(".result")];
  if (event.key === "Escape") { el("results").hidden = true; return; }
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
document.addEventListener("click", (event) => {
  if (!event.target.closest(".finder")) el("results").hidden = true;
});

export {
  buildThenOpen,
  catalogue,
  choose,
  everyRace,
  highlighted,
  loadRaces,
  matches,
  renderResults,
  upcoming,
};
