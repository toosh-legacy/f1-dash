/* The prediction: who finishes where from here, summoned over the map.
 *
 * Not a panel. The page keeps one static panel, the standings; this is a
 * sheet of glass called up from the top bar (or with P) that floats over the
 * right of the circuit, follows the lap under the playhead while it is open,
 * and goes away again.
 *
 * The numbers come from GET /replays/{key}/projection?lap=N: a projection
 * built from track position nudged by pace, tyres and stops still owed, and
 * the trained model's order beside it. They are deliberately not blended.
 */

import { api, el } from "./core.js";
import { player, selectCar } from "./map.js";

const cache = new Map();   // `${session}:${lap}` -> Promise<projection>
let open = false;
let shownLap = null;

function cacheKey(lap) { return `${player.sessionKey}:${lap}`; }

function fetchProjection(lap) {
  const key = cacheKey(lap);
  if (!cache.has(key)) {
    cache.set(key, api(`/replays/${player.sessionKey}/projection?lap=${lap}`)
      .catch((error) => { cache.delete(key); throw error; }));
  }
  return cache.get(key);
}

function setOpen(want) {
  open = want && !!player.bundle;
  const sheet = el("predict");
  el("predictBtn").setAttribute("aria-pressed", String(open));
  if (open) {
    sheet.hidden = false;
    requestAnimationFrame(() => { if (open) sheet.classList.add("open"); });
    shownLap = null;
    refreshPrediction();
  } else {
    sheet.classList.remove("open");
    setTimeout(() => { if (!open) sheet.hidden = true; }, 260);
  }
}

/** Follow the playhead: called by the map whenever the lap changes. */
async function refreshPrediction() {
  if (!open || !player.bundle) return;
  const lap = Math.max(player.lap || 1, 1);
  if (lap === shownLap) return;
  shownLap = lap;
  el("predictLap").textContent = `after lap ${lap}`;
  const body = el("predictRows");
  if (!body.children.length) body.innerHTML = '<div class="predict-empty">Asking the model…</div>';
  try {
    const projection = await fetchProjection(lap);
    if (lap !== shownLap) return;   // the playhead moved on while it was asked
    render(projection);
  } catch (error) {
    body.innerHTML = `<div class="predict-empty">No projection for this lap.</div>`;
  }
}

function render(projection) {
  const entries = (projection.entries || []).slice()
    .sort((a, b) => a.projected_position - b.projected_position);
  const body = el("predictRows");
  if (!entries.length) {
    body.innerHTML = '<div class="predict-empty">No timing on this lap.</div>';
    return;
  }
  const hasModel = entries.some((e) => e.model_position != null);
  el("predictHead").innerHTML = `<span>Proj</span><span></span><span>Driver</span><span>Now</span>
    <span>Stops</span>${hasModel ? "<span>Model</span>" : ""}`;
  el("predict").classList.toggle("has-model", hasModel);

  const before = new Map([...body.querySelectorAll(".predict-row")].map((r) => [r.dataset.number, r.offsetTop]));
  const fragment = document.createDocumentFragment();
  for (const entry of entries) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "predict-row";
    row.dataset.number = String(entry.number);
    row.style.setProperty("--team", entry.colour || "#888");
    const change = (entry.position ?? entry.projected_position) - entry.projected_position;
    const trend = change > 0 ? `<i class="up">▲${change}</i>` : change < 0 ? `<i class="down">▼${-change}</i>` : "<i></i>";
    row.innerHTML = `
      <span class="p">${entry.projected_position}</span>
      <span class="team"></span>
      <span class="who"><b></b></span>
      <span class="now">P${entry.position ?? "–"} ${trend}</span>
      <span class="stops">${entry.stops ?? 0}${entry.stops_owed ? `<em>+${entry.stops_owed}</em>` : ""}</span>
      ${hasModel ? `<span class="model">${entry.model_position ?? "–"}</span>` : ""}`;
    row.querySelector(".who b").textContent = entry.code || entry.number;
    row.addEventListener("click", (event) => { event.stopPropagation(); selectCar(entry.number); });
    fragment.append(row);
  }
  body.replaceChildren(fragment);

  // Rows slide to where the new lap's projection puts them.
  if (before.size && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    for (const row of body.querySelectorAll(".predict-row")) {
      const was = before.get(row.dataset.number);
      if (was == null) continue;
      const dy = was - row.offsetTop;
      if (Math.abs(dy) > 1) {
        row.animate([{ transform: `translateY(${dy}px)` }, { transform: "none" }],
          { duration: 420, easing: "cubic-bezier(.34,1.3,.5,1)" });
      }
    }
  }
  el("predictFoot").textContent = summary(projection, entries);
}

/** One line on what the projection is saying, in words. */
function summary(projection, entries) {
  const left = projection.laps_remaining;
  const movers = entries.filter((e) => e.position != null && e.position !== e.projected_position);
  const biggest = movers.sort((a, b) =>
    Math.abs(b.position - b.projected_position) - Math.abs(a.position - a.projected_position))[0];
  const parts = [];
  if (left != null) parts.push(`${left} laps to go`);
  if (biggest) {
    const gain = biggest.position - biggest.projected_position;
    parts.push(`${biggest.code} ${gain > 0 ? "up" : "down"} ${Math.abs(gain)} by the flag`);
  } else {
    parts.push("order holds");
  }
  return parts.join(" · ");
}

/** A new race: forget the old one's answers and close. */
function resetPrediction() {
  cache.clear();
  shownLap = null;
  el("predictRows").textContent = "";
  if (open) setOpen(false);
  el("predictBtn").disabled = true;
}

function predictionReady() {
  el("predictBtn").disabled = false;
}

el("predictBtn").addEventListener("click", (event) => { event.stopPropagation(); setOpen(!open); });
el("predict").addEventListener("click", (event) => event.stopPropagation());
el("predictClose").addEventListener("click", () => setOpen(false));
document.addEventListener("keydown", (event) => {
  const target = event.target;
  if (target && target.matches && target.matches("input, select, textarea")) return;
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key === "p" || event.key === "P") { event.preventDefault(); setOpen(!open); }
  else if (event.key === "Escape" && open && player.selected == null) setOpen(false);
});

export { predictionReady, refreshPrediction, resetPrediction };
