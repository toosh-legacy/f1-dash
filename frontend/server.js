#!/usr/bin/env node
/**
 * Static host + dev proxy for the dashboard.
 *
 * The dashboard itself is plain HTML/JS with no build step (guide section 3),
 * so this server exists for two reasons only:
 *
 *   1. serving it on its own origin/port, the way it will be served in
 *      production behind a CDN or reverse proxy;
 *   2. proxying `/api/*` and the WebSocket upgrade through to FastAPI in
 *      development, so the browser never deals with cross-origin rules.
 *
 * Zero dependencies -- Node's standard library covers all of it.
 */

"use strict";

const http = require("node:http");
const fs = require("node:fs");
const fsp = require("node:fs/promises");
const path = require("node:path");
const net = require("node:net");
const { URL } = require("node:url");

const PORT = Number(process.env.PORT || 3000);
const HOST = process.env.HOST || "0.0.0.0";
const API_TARGET = process.env.API_TARGET || "http://127.0.0.1:8000";
const STATIC_ROOT = path.resolve(
  process.env.STATIC_ROOT || path.join(__dirname, "..", "backend", "app", "static")
);

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".woff2": "font/woff2",
};

const target = new URL(API_TARGET);

/** Resolve a request path inside STATIC_ROOT, refusing traversal. */
function resolveStatic(requestPath) {
  const decoded = decodeURIComponent(requestPath.split("?")[0]);
  const relative = decoded === "/" ? "dashboard.html" : decoded.replace(/^\/+/, "");
  const resolved = path.resolve(STATIC_ROOT, relative);
  if (resolved !== STATIC_ROOT && !resolved.startsWith(STATIC_ROOT + path.sep)) {
    return null;
  }
  return resolved;
}

async function serveStatic(req, res) {
  const filePath = resolveStatic(req.url);
  if (!filePath) {
    res.writeHead(403, { "content-type": "text/plain" }).end("forbidden");
    return;
  }
  try {
    const stat = await fsp.stat(filePath);
    if (stat.isDirectory()) throw Object.assign(new Error("is a directory"), { code: "EISDIR" });
    res.writeHead(200, {
      "content-type": MIME[path.extname(filePath)] || "application/octet-stream",
      "content-length": stat.size,
      "cache-control": "no-cache",
    });
    fs.createReadStream(filePath).pipe(res);
  } catch (error) {
    if (error.code === "ENOENT" || error.code === "EISDIR") {
      res.writeHead(404, { "content-type": "text/plain" }).end("not found");
      return;
    }
    console.error("static error:", error);
    res.writeHead(500, { "content-type": "text/plain" }).end("internal error");
  }
}

/** Proxy an HTTP request to the FastAPI backend, stripping the /api prefix. */
function proxyHttp(req, res) {
  const upstreamPath = req.url.replace(/^\/api/, "") || "/";
  const upstream = http.request(
    {
      host: target.hostname,
      port: target.port || 80,
      path: upstreamPath,
      method: req.method,
      headers: { ...req.headers, host: target.host },
    },
    (upstreamRes) => {
      res.writeHead(upstreamRes.statusCode || 502, upstreamRes.headers);
      upstreamRes.pipe(res);
    }
  );
  upstream.on("error", (error) => {
    console.error(`proxy error for ${upstreamPath}:`, error.message);
    res.writeHead(502, { "content-type": "application/json" });
    res.end(JSON.stringify({ detail: `backend unreachable at ${API_TARGET}` }));
  });
  req.pipe(upstream);
}

const server = http.createServer((req, res) => {
  if (req.url === "/healthz") {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ status: "ok", static_root: STATIC_ROOT, api_target: API_TARGET }));
    return;
  }
  if (req.url.startsWith("/api/")) {
    proxyHttp(req, res);
    return;
  }
  serveStatic(req, res);
});

/**
 * WebSocket upgrades are proxied at the TCP level: the live dashboard connects
 * to /api/sessions/{id}/live on this origin and gets a transparent tunnel to
 * FastAPI's socket, so the browser sees one origin for both.
 */
server.on("upgrade", (req, socket, head) => {
  if (!req.url.startsWith("/api/")) {
    socket.destroy();
    return;
  }
  const upstreamPath = req.url.replace(/^\/api/, "") || "/";
  const upstream = net.connect(Number(target.port || 80), target.hostname, () => {
    const headers = Object.entries(req.headers)
      .filter(([key]) => key.toLowerCase() !== "host")
      .map(([key, value]) => `${key}: ${value}`)
      .join("\r\n");
    upstream.write(
      `GET ${upstreamPath} HTTP/1.1\r\nHost: ${target.host}\r\n${headers}\r\n\r\n`
    );
    if (head && head.length) upstream.write(head);
    upstream.pipe(socket);
    socket.pipe(upstream);
  });
  upstream.on("error", (error) => {
    console.error("websocket proxy error:", error.message);
    socket.destroy();
  });
  socket.on("error", () => upstream.destroy());
});

server.listen(PORT, HOST, () => {
  console.log(`dashboard  http://localhost:${PORT}`);
  console.log(`  static   ${STATIC_ROOT}`);
  console.log(`  api      ${API_TARGET} (proxied at /api)`);
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    console.log(`\n${signal} received, shutting down`);
    server.close(() => process.exit(0));
  });
}
