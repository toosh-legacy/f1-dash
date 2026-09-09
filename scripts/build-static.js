#!/usr/bin/env node
/**
 * Assembles the Vercel output directory.
 *
 * The dashboard has no build step (guide section 3), so "building" is two
 * moves: copy the page into `public/`, and write the one thing that differs
 * between a laptop and production -- where the API lives.
 */
"use strict";

const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..");
const SOURCE = path.join(ROOT, "backend", "app", "static");
const OUT = path.join(ROOT, "public");

const apiBase = (process.env.API_BASE || "").replace(/\/$/, "");

fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(OUT, { recursive: true });

// The dashboard is now a page, a stylesheet and a directory of ES modules, so
// the copy has to walk rather than list.
function copyInto(from, to) {
  fs.mkdirSync(to, { recursive: true });
  for (const entry of fs.readdirSync(from, { withFileTypes: true })) {
    const source = path.join(from, entry.name);
    const target = path.join(to, entry.name);
    if (entry.isDirectory()) copyInto(source, target);
    else fs.copyFileSync(source, target);
  }
}
copyInto(SOURCE, OUT);
fs.copyFileSync(path.join(OUT, "dashboard.html"), path.join(OUT, "index.html"));

// Empty is legitimate: the page then falls back to same-origin detection.
fs.writeFileSync(
  path.join(OUT, "config.js"),
  `window.API_BASE = ${JSON.stringify(apiBase)};\n`
);

console.log(apiBase ? `static built, API_BASE=${apiBase}` : "static built, API_BASE unset (same-origin fallback)");
