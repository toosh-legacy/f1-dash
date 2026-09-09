/* The live session, when there is one.
 *
 * A socket per session, carrying one message per period boundary or per lap,
 * plus race-control calls as they happen.
 */

import { bodies } from "./board.js";
import { endpoint, setStatus } from "./core.js";
import { player, renderOrder } from "./map.js";
import { notify, worthAnnouncing } from "./notices.js";

let socket = null;

function logLine(message) {
  const body = bodies.log;
  if (!body) return;
  if (!body.id) body.id = "log";
  const line = document.createElement("div");
  line.textContent = `${new Date().toLocaleTimeString()}  ${message}`;
  body.prepend(line);
  while (body.children.length > 150) body.lastChild.remove();
}

/** Follow a session that is running now.
 *
 *  The replay player and this share one page deliberately: a race being run
 *  and a race being replayed are the same thing to a viewer, and the only
 *  difference is where the next update comes from. */
function connectSocket(sessionId) {
  if (socket) socket.close();
  setStatus("connecting…");
  socket = new WebSocket(`${endpoint.ws}/sessions/${sessionId}/live`);
  socket.addEventListener("open", () => setStatus(`following session ${sessionId}`, "live"));
  socket.addEventListener("close", () => setStatus("disconnected"));
  socket.addEventListener("error", () => setStatus("socket error", "error"));
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    switch (message.type) {
      case "connected":
        logLine(`connected · regime ${message.regs_regime}`);
        break;
      case "qualifying_update":
        logLine(`qualifying ${message.period} · ${message.predictions.length} cars`);
        break;
      case "race_update": {
        logLine(`race lap ${message.lap_number}${message.is_gated ? " (gated)" : ""}`);
        // The socket sends the order in the same shape the bundle stores a lap
        // in, so a live race draws through the replay's own path.
        if (message.order && message.order.length && player.bundle) {
          player.bundle.laps = player.bundle.laps || {};
          player.bundle.laps[String(message.lap_number)] = message.order;
          player.lap = message.lap_number;
          renderOrder(message.lap_number);
        }
        break;
      }
      case "race_control": {
        logLine(`race control: ${message.event_type} (lap ${message.lap_number ?? "–"})`);
        if (worthAnnouncing(message)) {
          notify("control", message.event_type.replace("_", " ").toUpperCase(),
            message.message || `lap ${message.lap_number ?? "–"}`, "now");
        }
        break;
      }
      case "training_progress":
        logLine(`training: ${message.stage}`);
        break;
      default:
        logLine(`message: ${message.type}`);
    }
  });
}

export {
  connectSocket,
  logLine,
  socket,
};
