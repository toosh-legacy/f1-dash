/* The dead channel behind it all -- the static field and the boot sequence. */

import { el, reducedMotion } from "./core.js";
import { player } from "./map.js";

/** Vintage static, rendered small and turned almost all the way down.
 *
 *  It runs at a fraction of the display resolution and is scaled up by CSS:
 *  the effect is a texture, so nobody can tell, and it costs a fraction of
 *  what a full-resolution pass would. It stops when the tab is hidden and
 *  never animates if the viewer has asked for reduced motion.
 *
 *  The same field is drawn twice: once behind the page at almost nothing, and
 *  once over the loading screen at full strength.
 *
 *  Each frame reads the whole buffer back to add the snow, which is the most
 *  expensive thing on the page. The loading screen is worth it -- there is
 *  nothing else on screen. The one behind the dashboard is a texture at seven
 *  percent opacity, and it was costing the race map a third of its frame rate,
 *  so it runs at a rate nobody can tell apart from full speed. */
function staticField(canvas, scale, fps) {
  let awake = true;
  const ctx = canvas.getContext("2d", { alpha: false, willReadFrequently: true });
  const BARS = [
    [240, 235, 220], [255, 43, 57], [61, 139, 255], [200, 30, 44],
    [40, 90, 200], [255, 90, 100], [24, 52, 120], [214, 214, 218],
  ];
  let width = 0, height = 0, seed = 42, running = true, stopped = false;
  const minGap = fps ? 1000 / fps : 0;
  let lastPaint = -1e9;

  const hash = (x) => { const s = Math.sin(x * 127.1 + 311.7) * 43758.5453; return s - Math.floor(s); };
  const noise = (x) => {
    const i = Math.floor(x), f = x - i, s = f * f * (3 - 2 * f);
    return hash(i) * (1 - s) + hash(i + 1) * s;
  };

  function size() {
    width = Math.max(2, Math.round(window.innerWidth * scale));
    height = Math.max(2, Math.round(window.innerHeight * scale));
    canvas.width = width; canvas.height = height;
  }
  size();
  window.addEventListener("resize", size);
  document.addEventListener("visibilitychange", () => { running = !document.hidden; });

  function paint(time) {
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, width, height);

    // Bars, melting and reforming on a slow cycle.
    const cycle = (time * 0.075) % 1;
    let melt = Math.min(cycle * 2, 1);
    if (cycle > 0.85) melt *= 1 - (cycle - 0.85) / 0.15;

    const barWidth = width / BARS.length;
    for (let i = 0; i < BARS.length; i += 1) {
      const colour = BARS[i];
      const barMelt = Math.min(Math.max(melt + noise(i * 3.7 + time * 0.3) * 0.3 - 0.15, 0), 1);
      const slices = Math.ceil(barWidth / 2);
      for (let s = 0; s < slices; s += 1) {
        const x = i * barWidth + s * 2;
        const drip = barMelt * height * (0.3 + noise((i * 100 + s) * 1.7 + time * 0.25) * 0.7);
        const stretch = 1 + barMelt * (0.5 + noise((i * 100 + s) * 2.3) * 1.5);
        ctx.fillStyle = `rgb(${colour[0]},${colour[1]},${colour[2]})`;
        ctx.globalAlpha = 1 - barMelt * 0.3;
        ctx.fillRect(x, drip, 2, height * stretch);
      }
    }
    ctx.globalAlpha = 1;

    // A rolling band, the way a mistuned set drifts.
    const rollY = ((time * 0.15 * height) % (height * 1.4)) - height * 0.2;
    const roll = ctx.createLinearGradient(0, rollY - 30, 0, rollY + 30);
    roll.addColorStop(0, "rgba(255,240,240,0)");
    roll.addColorStop(0.5, "rgba(255,240,240,0.18)");
    roll.addColorStop(1, "rgba(255,240,240,0)");
    ctx.fillStyle = roll;
    ctx.fillRect(0, rollY - 30, width, 60);

    // Snow, sampled rather than swept: a full pass per frame is not worth it.
    const image = ctx.getImageData(0, 0, width, height);
    const data = image.data;
    const pixels = width * height;
    seed = Math.floor(time * 60) * 9973;
    for (let p = 0; p < pixels * 0.06; p += 1) {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      const idx = (seed % pixels) * 4;
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      const brightness = seed & 0xff;
      data[idx] = Math.min(255, data[idx] + brightness);
      data[idx + 1] = Math.min(255, data[idx + 1] + ((brightness * 0.8) | 0));
      data[idx + 2] = Math.min(255, data[idx + 2] + ((brightness * 0.9) | 0));
    }
    ctx.putImageData(image, 0, 0);

    // Scan lines, at this scale one row in two.
    ctx.fillStyle = "rgba(0,0,0,0.22)";
    for (let y = 0; y < height; y += 2) ctx.fillRect(0, y, width, 1);
  }

  if (reducedMotion) {
    paint(0.4);   // one frame, held: the texture without the movement
    return { stop() {} };
  }

  function draw(now) {
    if (stopped) return;
    requestAnimationFrame(draw);
    if (!running || !awake || now - lastPaint < minGap) return;
    lastPaint = now;
    paint(now / 1000);
  }
  requestAnimationFrame(draw);

  return {
    stop() { stopped = true; window.removeEventListener("resize", size); },
    wake(on) { awake = on; if (on) lastPaint = -1e9; },
  };
}

const backgroundField = staticField(el("tube"), 0.22, 12);   // a texture; twelve a second is plenty

/** The dead channel goes quiet while a race is running.
 *
 *  A full-viewport canvas costs a compositing pass and a full buffer read on
 *  every frame -- together about a third of the frame budget -- and it is
 *  wallpaper at seven percent opacity. Nobody watching cars move is looking at
 *  it, so it fades out when the race plays and comes back when it stops. */
function setBackgroundAwake(on) {
  const canvas = el("tube");
  if (!canvas) return;
  if (on) {
    canvas.style.display = "";
    backgroundField.wake(true);
    requestAnimationFrame(() => { canvas.style.opacity = ""; });
  } else {
    canvas.style.opacity = "0";
    backgroundField.wake(false);
    setTimeout(() => { if (player.playing) canvas.style.display = "none"; }, 400);
  }
}
let bootField = staticField(el("bootCanvas"), 0.30);   // the whole screen; full rate

/** The loading screen: what the page is doing, and then getting out of the way. */
function bootStage(text) {
  const node = el("bootStage");
  if (node) node.textContent = text;
}

function bootDone() {
  const boot = el("boot");
  if (!boot || boot.classList.contains("done")) return;
  boot.classList.add("done");
  setTimeout(() => {
    if (bootField) { bootField.stop(); bootField = null; }
    boot.remove();
  }, 800);
}

// However badly the start goes, the page is not left behind the static.
setTimeout(bootDone, 12000);

export {
  backgroundField,
  bootDone,
  bootField,
  bootStage,
  setBackgroundAwake,
  staticField,
};
