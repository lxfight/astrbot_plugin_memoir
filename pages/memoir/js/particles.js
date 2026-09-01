import { $ } from "./state.js";
import { REDUCED_MOTION } from "./utils.js";

/* ---------- constellation particles ---------- */
export function initParticles() {
  const cv = $("fx");
  if (REDUCED_MOTION) {
    cv.remove();
    return;
  }
  const ctx = cv.getContext("2d");
  let W = 0, H = 0, dpr = 1, raf = 0, running = false;
  const N = 36;
  const P = [];
  const mouse = { x: -9999, y: -9999 };
  let palette = [];
  const readPalette = () => {
    const dark = document.documentElement.dataset.theme === "dark";
    palette = dark
      ? ["45,212,191", "96,165,250", "100,116,139"]
      : ["13,148,136", "37,99,235", "100,116,139"];
  };

  function resize() {
    dpr = window.devicePixelRatio || 1;
    W = cv.width = Math.floor(innerWidth * dpr);
    H = cv.height = Math.floor(innerHeight * dpr);
    cv.style.width = innerWidth + "px";
    cv.style.height = innerHeight + "px";
  }

  function spawn(p) {
    p.x = Math.random() * W;
    p.y = Math.random() * H;
    p.r = (0.8 + Math.random() * 1.4) * dpr;
    p.vx = (Math.random() - 0.5) * 0.1 * dpr;
    p.vy = (-0.04 - Math.random() * 0.12) * dpr;
    p.phase = Math.random() * Math.PI * 2;
    p.tw = 0.4 + Math.random() * 0.8;
    p.c = palette[Math.floor(Math.random() * palette.length)];
  }

  function frame(t) {
    if (!running) return;
    ctx.clearRect(0, 0, W, H);
    const link = 100 * dpr;
    for (const p of P) {
      p.x += p.vx + Math.sin(t / 4000 + p.phase) * 0.04 * dpr;
      p.y += p.vy;
      const dx = p.x - mouse.x * dpr, dy = p.y - mouse.y * dpr;
      const d2 = dx * dx + dy * dy;
      const rr = 80 * dpr;
      if (d2 < rr * rr && d2 > 1) {
        const d = Math.sqrt(d2);
        const f = ((rr - d) / rr) * 0.3 * dpr;
        p.x += (dx / d) * f;
        p.y += (dy / d) * f;
      }
      if (p.y < -10) { p.y = H + 10; p.x = Math.random() * W; }
      if (p.x < -10) p.x = W + 10;
      if (p.x > W + 10) p.x = -10;
    }
    ctx.lineWidth = dpr * 0.6;
    for (let i = 0; i < N; i++) {
      for (let j = i + 1; j < N; j++) {
        const a = P[i], b = P[j];
        const dx = a.x - b.x, dy = a.y - b.y;
        const d = Math.hypot(dx, dy);
        if (d < link) {
          const alpha = (1 - d / link) * 0.07;
          ctx.strokeStyle = `rgba(${a.c},${alpha})`;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }
    }
    for (const p of P) {
      const tw = 0.28 + 0.26 * Math.sin(t / 900 * p.tw + p.phase);
      ctx.fillStyle = `rgba(${p.c},${tw})`;
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fill();
    }
    raf = requestAnimationFrame(frame);
  }

  const start = () => {
    if (running || REDUCED_MOTION) return;
    running = true;
    raf = requestAnimationFrame(frame);
  };
  const stop = () => {
    running = false;
    cancelAnimationFrame(raf);
  };

  readPalette();
  resize();
  for (let i = 0; i < N; i++) {
    const p = {};
    spawn(p);
    P.push(p);
  }
  window.addEventListener("resize", resize);
  window.addEventListener("pointermove", (e) => { mouse.x = e.clientX; mouse.y = e.clientY; });
  window.addEventListener("pointerleave", () => { mouse.x = -9999; mouse.y = -9999; });
  document.addEventListener("visibilitychange", () => (document.hidden ? stop() : start()));
  window.__fxRecolor = () => {
    readPalette();
    for (const p of P) p.c = palette[Math.floor(Math.random() * palette.length)];
  };
  start();
}
