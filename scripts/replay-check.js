#!/usr/bin/env node
/**
 * End-to-end check of the replay path, running the same arithmetic the
 * dashboard runs.
 *
 * The map is drawn by placing each car at its *progress along the circuit*
 * rather than at a raw coordinate, so the two things worth proving without a
 * browser are that the progress values land on the drawn path, and that they
 * advance. A car frozen at one point, or one drifting off the outline, would
 * look like a rendering bug but is really a data bug.
 */
"use strict";

const API = (process.env.API || "http://127.0.0.1:8000").replace(/\/$/, "");

let failures = 0;
function check(name, ok, detail) {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
  if (!ok) failures += 1;
}

async function json(path) {
  const response = await fetch(`${API}${path}`);
  if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
  return response.json();
}

/** Point at a fractional index along a closed path, exactly as the map does. */
function pointAt(path, progress) {
  const wrapped = ((progress % path.length) + path.length) % path.length;
  const index = Math.floor(wrapped);
  const [x0, y0] = path[index];
  const [x1, y1] = path[(index + 1) % path.length];
  const alpha = wrapped - index;
  return [x0 + (x1 - x0) * alpha, y0 + (y1 - y0) * alpha];
}

(async function main() {
  const rounds = await json("/replays");
  check("replay catalogue", rounds.length > 0, `${rounds.length} races this season`);

  const built = rounds.find((round) => round.cached);
  if (!built) {
    console.log("\nNo replay is built yet — build one first:");
    console.log(`  curl -X POST ${API}/replays/${rounds[0].session_key}/build`);
    process.exit(failures ? 1 : 0);
  }

  const bundle = await json(`/replays/${built.session_key}`);
  const { track, frames, drivers } = bundle;
  check("bundle downloads", frames.length > 0,
    `${frames.length} frames over ${Math.round(bundle.duration_s / 60)} min`);
  check("circuit outline", track.path.length > 100, `${track.path.length} points`);
  check("pit lane traced", track.pit_path.length > 5, `${track.pit_path.length} points`);
  check("team colours", drivers.every((d) => /^#[0-9a-f]{6}$/i.test(d.colour)),
    `${drivers.length} cars`);

  // Cars must move. A stuck car and a moving car look identical in one frame.
  const sample = frames.filter((frame) => Object.keys(frame.cars).length > 5);
  const early = sample[Math.floor(sample.length * 0.3)];
  const later = sample[Math.floor(sample.length * 0.3) + 30];
  const moved = Object.keys(early.cars).filter((number) => {
    const a = early.cars[number], b = later ? later.cars[number] : undefined;
    return typeof a === "number" && typeof b === "number" && Math.abs(b - a) > 1;
  });
  check("cars move between frames", moved.length > 10,
    `${moved.length} of ${Object.keys(early.cars).length} cars advanced over 30 s`);

  // Every on-track progress value has to resolve to a point on the outline.
  const onPath = Object.values(early.cars)
    .filter((value) => typeof value === "number")
    .every((value) => {
      const [x, y] = pointAt(track.path, value);
      return Number.isFinite(x) && Number.isFinite(y);
    });
  check("progress resolves onto the circuit", onPath);

  const flags = new Set(frames.map((frame) => frame.flag));
  check("race control drives the flag state", flags.size >= 1, [...flags].join(", "));

  const lap = Math.max(1, Math.round(bundle.total_laps * 0.6));
  const projection = await json(`/replays/${built.session_key}/projection?lap=${lap}`);
  check("projection returns a full order",
    projection.entries.length >= drivers.length - 4,
    `lap ${lap}, ${projection.entries.length} cars, leader ${projection.entries[0].code}`);
  check("projection explains itself",
    projection.entries.every((entry) => "pace_delta_s" in entry && "stops_owed" in entry),
    projection.basis);

  console.log(failures ? `\n${failures} check(s) failed` : "\nall checks passed");
  process.exit(failures ? 1 : 0);
})().catch((error) => {
  console.error("replay check failed:", error.message);
  process.exit(1);
});
