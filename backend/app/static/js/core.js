/* Talking to the API, and the handful of helpers everything uses.
 *
 * Deliberately the one module with no imports of its own. Everything else
 * depends on it, so anything that grew opinions here would reach the whole
 * page.
 */

const params = new URLSearchParams(location.search);
const el = (id) => document.getElementById(id);
const svgNS = "http://www.w3.org/2000/svg";
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Where the API is, and the socket beside it.
 *
 * An object rather than two bindings: a module cannot assign to something it
 * imported, and boot has to set these once it has found the API. Reading
 * `endpoint.api` gets whatever boot settled on, from anywhere.
 */
export const endpoint = { api: params.get("api") || window.API_BASE || "", ws: "" };

/** Called once, by boot, as soon as the API answers. */
export function setEndpoints(base) {
  endpoint.api = base;
  endpoint.ws = base.replace(/^http/, "ws");
}

async function detectApi() {
  const candidates = endpoint.api ? [endpoint.api] : [location.origin + "/api", location.origin];
  for (const base of candidates) {
    try {
      const response = await fetch(`${base.replace(/\/$/, "")}/health`);
      if (response.ok) return base.replace(/\/$/, "");
    } catch (_) { /* try the next one */ }
  }
  return location.origin;
}

async function api(path, options) {
  const response = await fetch(`${endpoint.api}${path}`, options);
  if (!response.ok) {
    const body = await response.text();
    // The API's own explanation when it gives one ({detail: {message}}),
    // so a refusal reads as a sentence rather than a status line.
    let message = `${response.status} ${body.slice(0, 200)}`;
    try {
      const detail = JSON.parse(body).detail;
      if (detail && detail.message) message = detail.message;
      else if (typeof detail === "string") message = detail;
    } catch (_) { /* not JSON: keep the raw text */ }
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function setStatus(text, state) {
  el("statusText").textContent = text;
  el("dot").className = "dot" + (state ? " " + state : "");
}

const clock = (seconds) => {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};
const when = (iso) => (iso ? new Date(iso).toLocaleString(undefined,
  { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "");

export {
  api,
  clock,
  detectApi,
  el,
  params,
  reducedMotion,
  setStatus,
  svgNS,
  when,
};
