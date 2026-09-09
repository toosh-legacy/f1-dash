/* Start.
 *
 * The order matters: find the API, put the board up, draw whatever the server
 * says is worth opening on, and only then wire the socket.
 */

import { bodies, buildBoard, buildPanelsMenu, setPanelVisible } from "./board.js";
import { bootDone, bootStage } from "./chrome.js";
import { api, detectApi, setEndpoints, setStatus } from "./core.js";
import { connectSocket, logLine } from "./live.js";
import { mapMarkup, openReplay, openUpcoming } from "./map.js";
import { renderNotices } from "./notices.js";
import { catalogue, loadRaces, upcoming } from "./search.js";

(async function start() {
  bootStage("finding the api");
  setEndpoints(await detectApi());

  buildBoard();
  buildPanelsMenu();
  bodies.map.innerHTML = mapMarkup();
  bodies.map.style.overflow = "hidden";
  bodies.map.style.display = "flex";
  renderNotices();

  bootStage("reading the calendar");
  const { now } = await loadRaces();

  // The page opens on the next race, which is what a viewer wants on a normal
  // day; if the calendar is finished, it opens on the last one that ran.
  if (upcoming.length) {
    bootStage("drawing the circuit");
    await openUpcoming(upcoming[0]);
  } else {
    const ready = (catalogue || []).find((round) => round.cached);
    if (ready) {
      bootStage("downloading the replay");
      await openReplay(ready.session_key, ready);
    }
  }

  bootDone();

  if (now && now.live && now.live.session_id) {
    // A session is running: show it, and follow it.
    setPanelVisible("log", true);
    connectSocket(now.live.session_id);
    if (now.live.openf1_session_key) {
      await openReplay(now.live.openf1_session_key, now.live).catch(() => {});
    }
  } else {
    logLine("no session is running — showing the calendar");
  }
})().catch((error) => {
  bootStage(error.message);
  setStatus(error.message, "error");
  setTimeout(bootDone, 1400);
});
