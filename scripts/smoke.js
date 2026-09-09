#!/usr/bin/env node
/**
 * End-to-end smoke check against a running stack.
 *
 * Verifies the three things that have to work before the dashboard is usable:
 * the API answers, the static dashboard is served, and the WebSocket accepts a
 * subscriber and delivers its handshake frame.
 *
 * Usage:  node scripts/smoke.js [--api http://localhost:8000] [--web http://localhost:3000]
 */

"use strict";

const args = process.argv.slice(2);
const argOf = (flag, fallback) => {
  const index = args.indexOf(flag);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
};

const API = argOf("--api", process.env.API_TARGET || "http://127.0.0.1:8000").replace(/\/$/, "");
const WEB = argOf("--web", process.env.WEB_TARGET || "http://127.0.0.1:3000").replace(/\/$/, "");

let failures = 0;

function report(name, ok, detail) {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
  if (!ok) failures += 1;
}

async function checkHealth() {
  try {
    const response = await fetch(`${API}/health`);
    const body = await response.json();
    report(
      "api /health",
      response.ok && body.status === "ok",
      `regime ${body.regs_regime}, season ${body.season}, models ${Object.keys(body.active_models || {}).length}`
    );
    return body;
  } catch (error) {
    report("api /health", false, error.message);
    return null;
  }
}

async function checkSessions() {
  try {
    const response = await fetch(`${API}/sessions`);
    const sessions = await response.json();
    report("api /sessions", response.ok, `${sessions.length} sessions`);
    if (!sessions.length) {
      console.log("      hint: seed reference data first — curl -X POST " + API + "/seed -H 'content-type: application/json' -d '{}'");
    }
    return sessions;
  } catch (error) {
    report("api /sessions", false, error.message);
    return [];
  }
}

async function checkDashboard() {
  try {
    const response = await fetch(`${WEB}/`);
    const html = await response.text();
    // The markup the page actually ships: a search that reaches every race, a
    // board of panels, and the module that fills them. The circuit is drawn
    // by that module rather than written here, so it is checked below by
    // fetching it -- not by looking for its id in the HTML, which passed for
    // years only because the whole script used to be inline.
    const parts = ['id="search"', 'id="board"', 'js/boot.js'];
    report("dashboard html", response.ok && parts.every((part) => html.includes(part)));
  } catch (error) {
    report("dashboard html", false, `${error.message} (is \`npm start\` running?)`);
  }
}

async function checkAssets() {
  // A module graph fails quietly in a browser: one 404 and the page is blank
  // with nothing in the log worth reading. Better to ask here.
  for (const asset of ["dashboard.css", "js/boot.js", "js/core.js", "js/map.js"]) {
    try {
      const response = await fetch(`${WEB}/${asset}`);
      report(`asset ${asset}`, response.ok, response.ok ? undefined : `HTTP ${response.status}`);
    } catch (error) {
      report(`asset ${asset}`, false, error.message);
    }
  }
}

function checkWebSocket(sessionId) {
  return new Promise((resolve) => {
    const url = `${API.replace(/^http/, "ws")}/sessions/${sessionId}/live`;
    const socket = new WebSocket(url);
    const timer = setTimeout(() => {
      report("websocket handshake", false, "timed out after 5s");
      socket.close();
      resolve();
    }, 5000);

    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      clearTimeout(timer);
      report("websocket handshake", message.type === "connected", `first frame: ${message.type}`);
      socket.close();
      resolve();
    });
    socket.addEventListener("error", () => {
      clearTimeout(timer);
      report("websocket handshake", false, `cannot connect to ${url}`);
      resolve();
    });
  });
}

(async () => {
  console.log(`api ${API}\nweb ${WEB}\n`);
  await checkHealth();
  const sessions = await checkSessions();
  await checkDashboard();
  await checkAssets();
  await checkWebSocket(sessions[0]?.id ?? 1);
  console.log(`\n${failures ? failures + " check(s) failed" : "all checks passed"}`);
  process.exit(failures ? 1 : 0);
})();
