/* Start.
 *
 * The order matters: find the API, draw whatever the server says is worth
 * opening on, and only then wire the socket.
 */

import { bootDone, bootStage } from "./chrome.js";
import { detectApi, setEndpoints, setStatus } from "./core.js";
import { logLine, watchLive } from "./live.js";
import { openReplay, openUpcoming } from "./map.js";
import { catalogue, loadRaces, setMode, upcoming } from "./search.js";

(async function start() {
  bootStage("finding the api");
  setEndpoints(await detectApi());

  bootStage("reading the calendar");
  const { now } = await loadRaces();

  // The page opens on the next race, which is what a viewer wants on a normal
  // day; if the calendar is finished, it opens on the last one that ran.
  // ?session=<key> opens a particular replay instead.
  const asked = Number(new URLSearchParams(location.search).get("session"));
  const ready = (catalogue || []).filter((round) => round.cached);
  const chosen = ready.find((round) => round.session_key === asked);
  if (chosen) {
    bootStage("downloading the replay");
    setMode("Replay");
    await openReplay(chosen.session_key, chosen);
  } else if (upcoming.length) {
    bootStage("drawing the circuit");
    setMode("Next");
    await openUpcoming(upcoming[0]);
  } else if (ready.length) {
    bootStage("downloading the replay");
    setMode("Replay");
    await openReplay(ready[0].session_key, ready[0]);
  }

  bootDone();

  if (now && now.live && now.live.session_id) {
    // A session is running: show it, and follow it.
    setMode("Live");
    await watchLive(now.live.session_id);
  } else {
    logLine("no session is running — showing the calendar");
  }
})().catch((error) => {
  bootStage(error.message);
  setStatus(error.message, "error");
  setTimeout(bootDone, 1400);
});
