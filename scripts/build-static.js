#!/usr/bin/env node
/**
 * Assembles the Vercel output directory.
 *
 * The dashboard has no build step (guide section 3), so "building" is a copy
 * of the page into `public/`, which Vercel serves from its CDN. The API is a
 * separate Python function (api/index.py) answering under /api on the same
 * origin; the page finds it by itself (same origin /api first, then
 * http://localhost:8000 for a copy pointed at a laptop), so nothing about the
 * API is baked in. Only the page goes into `public/`: no Python, ever.
 *
 * DASHBOARD_API_URL overrides that for a build that should point elsewhere.
 * The old API_BASE variable is deliberately ignored: it named the retired
 * AWS host, and a leftover copy in Vercel's settings must not resurrect it.
 */
"use strict";

const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..");
const SOURCE = path.join(ROOT, "backend", "app", "static");
const OUT = path.join(ROOT, "public");

const apiBase = (process.env.DASHBOARD_API_URL || "").replace(/\/$/, "");

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

// Empty is the normal case: the page then looks for the API itself.
fs.writeFileSync(
  path.join(OUT, "config.js"),
  `window.API_BASE = ${JSON.stringify(apiBase)};\n`
);

// The output is public: refuse to publish anything that is not the page.
const PAGE_FILES = /\.(html|css|js|mjs|json|svg|png|jpe?g|webp|ico|woff2?|txt|map)$/i;
(function check(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const file = path.join(dir, entry.name);
    if (entry.isDirectory()) check(file);
    else if (!PAGE_FILES.test(entry.name)) {
      throw new Error(`refusing to publish ${path.relative(ROOT, file)}: not part of the page`);
    }
  }
})(OUT);

console.log(apiBase ? `static built, API at ${apiBase}` : "static built, API found at runtime (same origin /api, then localhost:8000)");
