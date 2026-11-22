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

/** Where the API runs when nothing else says: the developer's own machine. */
const LOCAL_API = "http://localhost:8000";
const RETRY_MS = 2500;

async function answers(base) {
  try {
    const response = await fetch(`${base}/health`, { cache: "no-store" });
    if (!response.ok) return false;
    const body = await response.json();
    return body && typeof body === "object";
  } catch (_) {
    return false;
  }
}

/** Find the API, waiting for it to start if it is not up yet.
 *
 * `?api=` or `window.API_BASE` names it outright. Otherwise the same origin
 * is tried first (FastAPI serving the page, or the Node dev proxy at /api),
 * then the API on localhost -- which is how the copy hosted on Vercel finds
 * the API running on the viewer's machine. Nothing answering is not an
 * error: `onWaiting` is told, and the search repeats until one does.
 */
async function detectApi({ onWaiting } = {}) {
  const named = (endpoint.api || "").replace(/\/$/, "");
  const candidates = named
    ? [named]
    : [...new Set([location.origin + "/api", location.origin, LOCAL_API])];
  for (;;) {
    for (const base of candidates) {
      if (await answers(base)) return base;
    }
    if (onWaiting) onWaiting(candidates[candidates.length - 1]);
    await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
  }
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
