/* The projection panel: where the race is heading, and how well that has held up.
 *
 * The projection is computed server-side and cached per lap, so scrubbing asks
 * for laps that have usually already been answered.
 */

import { bodies, layout, place } from "./board.js";
import { api, el } from "./core.js";
import { player } from "./map.js";

const projectionCache = new Map();
let accuracyCache = null;

async function refreshProjection(lap, options = {}) {
  if (!layout.prediction.visible) return;
  if (!player.bundle || !lap) return renderProjection(null);
  if (options.fresh) projectionCache.delete(lap);
  if (projectionCache.has(lap)) return renderProjection(projectionCache.get(lap));

  if (bodies.prediction) bodies.prediction.innerHTML = '<div class="empty">Asking the model…</div>';
  try {
    const projection = await api(
      `/replays/${player.sessionKey}/projection?lap=${lap}${options.fresh ? "&fresh=true" : ""}`);
    projectionCache.set(lap, projection);
    if (player.lap === lap || !player.playing) renderProjection(projection);
  } catch (error) {
    if (bodies.prediction) bodies.prediction.innerHTML = `<div class="empty">${error.message}</div>`;
  }
}

function summarise(projection) {
  const entries = projection.entries;
  const lines = [];
  const winner = entries[0];
  lines.push(`<b>${winner.name || winner.code}</b> takes it, with ${projection.laps_remaining}
    lap${projection.laps_remaining === 1 ? "" : "s"} left to run.`);

  const movers = entries
    .filter((e) => e.position_change != null && Math.abs(e.position_change) >= 2)
    .sort((a, b) => Math.abs(b.position_change) - Math.abs(a.position_change))
    .slice(0, 2);
  for (const mover of movers) {
    const up = mover.position_change > 0;
    lines.push(`<b>${mover.code}</b> ${up ? "gains" : "loses"} ${Math.abs(mover.position_change)}
      place${Math.abs(mover.position_change) === 1 ? "" : "s"} from P${mover.position} —
      ${up ? "quicker than the cars around it" : "it still owes the pit lane"}.`);
  }

  const owing = entries.filter((e) => e.stops_owed > 0).length;
  lines.push(owing
    ? `${owing} car${owing === 1 ? " has" : "s have"} a stop still to make, worth about
       ${projection.pit_loss_s.toFixed(0)}s each here.`
    : "Everyone is on the tyres they intend to finish on.");

  const model = projection.model || {};
  if (model.available) {
    const disagreements = entries.filter(
      (e) => e.model_position && Math.abs(e.model_position - e.projected_position) >= 4).length;
    lines.push(disagreements
      ? `The trained model disagrees sharply on ${disagreements} car${disagreements === 1 ? "" : "s"}.`
      : "The trained model broadly agrees with this order.");
  }
  return `<h3>What the model is thinking</h3><ul>${lines.map((l) => `<li>${l}</li>`).join("")}</ul>`;
}

function renderProjection(projection) {
  const body = bodies.prediction;
  if (!body) return;   // the panel is closed; nothing to draw into
  if (!projection || !projection.entries || !projection.entries.length) {
    body.innerHTML = '<div class="empty">'
      + (player.upcoming ? "Nothing to project until the cars run." : "No timing data on this lap.")
      + "</div>";
    return;
  }
  const modelLive = projection.model && projection.model.available;
  const rows = projection.entries.map((entry) => {
    const change = entry.position_change;
    const arrow = change > 0 ? `<span class="up">▲${change}</span>`
      : change < 0 ? `<span class="down">▼${-change}</span>`
      : '<span style="color:var(--dim)">–</span>';
    return `<tr>
      <td class="num">${entry.projected_position}</td>
      <td><span style="display:inline-block;width:4px;height:14px;border-radius:2px;background:${entry.colour};vertical-align:-2px"></span>
        <b>${entry.code}</b></td>
      <td class="num">${entry.position ?? "–"} ${arrow}</td>
      <td>${entry.compound ? `<span class="tyre ${entry.compound}">${entry.compound[0]}${entry.tyre_age ?? ""}</span>` : "–"}</td>
      <td class="num">${entry.stops}${entry.stops_owed ? ` +${entry.stops_owed}` : ""}</td>
      ${modelLive ? `<td class="num">${entry.model_position ?? "–"}</td>` : ""}
    </tr>`;
  }).join("");

  body.innerHTML = `<div class="thinking">${summarise(projection)}</div>
    <table><thead><tr>
      <th class="num">Proj</th><th>Driver</th><th class="num">Now</th><th>Tyre</th>
      <th class="num">Stops</th>${modelLive ? '<th class="num">Model</th>' : ""}
    </tr></thead><tbody>${rows}</tbody></table>
    <div class="side-foot" id="panelFoot"></div>`;
  showAccuracy(projection);
}

async function showAccuracy(projection) {
  const node = el("panelFoot");
  if (!node) return;
  try { if (!accuracyCache) accuracyCache = await api("/predictions/accuracy"); }
  catch (_) { return; }
  if (!accuracyCache.scored_laps) {
    node.innerHTML = "<b>Not yet scored.</b> Evaluate a finished race to measure this projection.";
    return;
  }
  const stage = stageOf(player.lap, player.bundle ? player.bundle.total_laps : 0);
  const row = (accuracyCache.by_stage || []).find((s) => s.stage === stage) || accuracyCache.overall;
  const places = (value) => (value == null ? "–" : `${value.toFixed(1)} places`);
  const verdict = row.best === "projection" ? "the projection is ahead"
    : row.best === "model" ? "the model is ahead" : "reading the running order is still better";
  const model = projection.model || {};
  node.innerHTML = `<div class="formula">order = track position, nudged by pace, tyres and stops owed</div>
    <div style="margin-top:6px">Measured on ${accuracyCache.scored_laps} scored laps —
      typical error in the ${stage || "race"}: projection ${places(row.projection)},
      model ${places(row.model)}, track position ${places(row.track_position)}. ${verdict}.</div>
    ${model.available ? `<div style="margin-top:6px">Model column: <b>${model.model_type}</b> v${model.version}.</div>` : ""}`;
}

function stageOf(lap, totalLaps) {
  if (!lap || !totalLaps) return null;
  const fraction = lap / totalLaps;
  if (fraction < 0.25) return "opening quarter";
  if (fraction < 0.5) return "second quarter";
  if (fraction < 0.75) return "third quarter";
  return "final quarter";
}

export {
  accuracyCache,
  projectionCache,
  refreshProjection,
  renderProjection,
  showAccuracy,
  stageOf,
  summarise,
};
