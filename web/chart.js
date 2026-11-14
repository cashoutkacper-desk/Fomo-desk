/* STEP 8 · charts drawn by hand on <canvas>. No chart library.
   Home line chart, Coin candles, Analyst score ring. devicePixelRatio aware. */
(function () {
  "use strict";
  const C = { green: "#00d35a", red: "#ff453a", orange: "#ff9f0a", blue: "#0a84ff",
              text2: "rgba(235,235,245,0.6)", text3: "rgba(235,235,245,0.3)", card2: "#2c2c2e" };
  const MARK_COLORS = { blue: C.blue, green: C.green, red: C.red };
  const reduced = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function setup(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const r = canvas.getBoundingClientRect();
    const w = Math.max(1, Math.round(r.width)), h = Math.max(1, Math.round(r.height));
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }
  const lerp = (a, b, t) => a + (b - a) * t;
  const ease = (t) => 1 - Math.pow(1 - t, 3);
  const font = (px, w) => `${w || 600} ${px}px -apple-system, "SF Pro Text", BlinkMacSystemFont, sans-serif`;

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }

  /* ---------------- Home line chart */
  class LineChart {
    constructor(canvas) { this.canvas = canvas; this.view = null; this.seen = new Set(); this.fade = {}; this.raf = 0; }
    // data: {points:[{ts,value}], marks:[{ts,color,text}], start}
    set(data, rangeKey) {
      const pts = (data.points || []).map(p => ({ t: Date.parse(p.ts), v: +p.value })).filter(p => isFinite(p.t) && isFinite(p.v));
      const start = isFinite(+data.start) ? +data.start : (pts[0] ? pts[0].v : 0);
      const target = this.domain(pts, start);
      const sameRange = this.rangeKey === rangeKey;
      const from = (sameRange && this.view && !reduced()) ? this.view : target;
      this.rangeKey = rangeKey;
      this.pts = pts; this.start = start; this.marks = data.marks || [];
      const now = performance.now();
      for (const m of this.marks) {
        const k = m.ts + "|" + m.text;
        if (!this.seen.has(k)) { this.seen.add(k); this.fade[k] = sameRange && !reduced() ? now : 0; }
      }
      cancelAnimationFrame(this.raf);
      const t0 = performance.now();
      const step = () => {
        const p = Math.min(1, (performance.now() - t0) / 250);   // new point slides in over 250ms
        const e = ease(p);
        this.view = { x0: lerp(from.x0, target.x0, e), x1: lerp(from.x1, target.x1, e),
                      y0: lerp(from.y0, target.y0, e), y1: lerp(from.y1, target.y1, e) };
        const fading = this.draw();
        if (p < 1 || fading) this.raf = requestAnimationFrame(step);
      };
      step();
    }
    domain(pts, start) {
      if (!pts.length) return { x0: 0, x1: 1, y0: start - 1, y1: start + 1 };
      let y0 = Math.min(start, ...pts.map(p => p.v)), y1 = Math.max(start, ...pts.map(p => p.v));
      const pad = Math.max((y1 - y0) * 0.12, Math.abs(start) * 0.002, 0.01);
      const x0 = pts[0].t, x1 = pts.length > 1 ? pts[pts.length - 1].t : pts[0].t + 60000;
      return { x0, x1, y0: y0 - pad, y1: y1 + pad };
    }
    draw() {
      const { ctx, w, h } = setup(this.canvas);
      const v = this.view; if (!v) return false;
      const top = 58, bottom = h - 8, left = 16, right = w - 16;  // room for 3 rows of marks
      const X = (t) => left + (v.x1 === v.x0 ? 0 : (t - v.x0) / (v.x1 - v.x0)) * (right - left);
      const Y = (val) => bottom - (val - v.y0) / (v.y1 - v.y0 || 1) * (bottom - top);
      // dashed line at the start value
      ctx.save(); ctx.setLineDash([3, 4]); ctx.strokeStyle = C.text3; ctx.lineWidth = 1;
      const ys = Y(this.start); ctx.beginPath(); ctx.moveTo(0, ys); ctx.lineTo(w, ys); ctx.stroke(); ctx.restore();
      const pts = this.pts;
      if (pts.length < 2) return false;     // empty state text is HTML over the canvas
      // gradient fill under the line
      const g = ctx.createLinearGradient(0, top, 0, bottom);
      g.addColorStop(0, "rgba(0,211,90,0.28)"); g.addColorStop(1, "rgba(0,211,90,0)");
      ctx.beginPath(); ctx.moveTo(X(pts[0].t), Y(pts[0].v));
      for (const p of pts) ctx.lineTo(X(p.t), Y(p.v));
      ctx.lineTo(X(pts[pts.length - 1].t), bottom); ctx.lineTo(X(pts[0].t), bottom); ctx.closePath();
      ctx.fillStyle = g; ctx.fill();
      ctx.beginPath(); ctx.moveTo(X(pts[0].t), Y(pts[0].v));
      for (const p of pts) ctx.lineTo(X(p.t), Y(p.v));
      ctx.strokeStyle = C.green; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.stroke();
      const last = pts[pts.length - 1];
      ctx.beginPath(); ctx.arc(X(last.t), Y(last.v), 4, 0, Math.PI * 2); ctx.fillStyle = C.green; ctx.fill();
      // marks above the line; never overlap: a later mark moves up a row
      ctx.font = font(12);
      const placed = []; let fading = false; const now = performance.now();
      const valueAt = (t) => { let best = pts[0]; for (const p of pts) { if (p.t <= t) best = p; } return best.v; };
      const marks = this.marks.filter(m => isFinite(Date.parse(m.ts))).sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
      for (const m of marks) {
        const t = Date.parse(m.ts); if (t < v.x0 - 1 || t > v.x1 + 1) continue;
        const tw = ctx.measureText(m.text).width + 14, th = 20;
        let x = Math.min(Math.max(X(t) - tw / 2, 2), w - tw - 2);
        const lineY = Y(valueAt(t));
        let row = 0;
        const hit = (r) => placed.some(p => p.row === r && x < p.x + p.w + 4 && p.x < x + tw + 4);
        while (hit(row)) row++;
        placed.push({ x, w: tw, row });
        const y = Math.max(2, Math.min(lineY - 14 - th, top - th - 4) - row * (th + 4));
        const k = m.ts + "|" + m.text; const f0 = this.fade[k] || 0;
        const a = f0 ? Math.min(1, (now - f0) / 200) : 1;                // marks fade in over 200ms
        if (a < 1) fading = true;
        ctx.globalAlpha = a;
        const col = MARK_COLORS[m.color] || C.blue;
        ctx.strokeStyle = col; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(X(t), y + th); ctx.lineTo(X(t), lineY); ctx.stroke();
        roundRect(ctx, x, y, tw, th, 10); ctx.fillStyle = col; ctx.fill();
        ctx.fillStyle = m.color === "blue" || m.color === "red" ? "#fff" : "#000";
        ctx.textBaseline = "middle"; ctx.fillText(m.text, x + 7, y + th / 2 + 0.5);
        ctx.globalAlpha = 1;
      }
      return fading;
    }
  }

  /* ---------------- Coin candles with volume + Risk exit line */
  function candles(canvas, list, opts) {
    opts = opts || {};
    const { ctx, w, h } = setup(canvas);
    const left = 12, right = w - 64, top = 12, volH = h * 0.2, bottom = h - volH - 10;
    if (!list || !list.length) {
      ctx.fillStyle = C.text2; ctx.font = font(15, 400); ctx.textAlign = "center";
      ctx.fillText("No price data yet", w / 2, h / 2); ctx.textAlign = "left"; return;
    }
    let lo = Math.min(...list.map(c => c.l)), hi = Math.max(...list.map(c => c.h));
    if (opts.exitLine) { lo = Math.min(lo, opts.exitLine); hi = Math.max(hi, opts.exitLine); }
    const pad = (hi - lo) * 0.08 || hi * 0.05 || 1e-9; lo -= pad; hi += pad;
    const Y = (p) => bottom - (p - lo) / (hi - lo) * (bottom - top);
    const n = Math.max(list.length, 30), slot = (right - left) / n, bw = Math.max(1.5, slot * 0.62);
    const vmax = Math.max(...list.map(c => c.v || 0)) || 1;
    const off = n - list.length;
    list.forEach((c, i) => {
      const cx = left + (i + off + 0.5) * slot, up = c.c >= c.o, col = up ? C.green : C.red;
      ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(cx, Y(c.h)); ctx.lineTo(cx, Y(c.l)); ctx.stroke();
      const y1 = Y(Math.max(c.o, c.c)), y2 = Y(Math.min(c.o, c.c));
      ctx.fillRect(cx - bw / 2, y1, bw, Math.max(1, y2 - y1));
      const vh = (c.v || 0) / vmax * volH;                 // volume bar = change of volume.m5
      ctx.globalAlpha = 0.45; ctx.fillRect(cx - bw / 2, h - vh, bw, vh); ctx.globalAlpha = 1;
    });
    // price labels
    ctx.fillStyle = C.text3; ctx.font = font(11, 400);
    ctx.fillText(opts.fmt ? opts.fmt(hi - pad) : "", right + 6, Y(hi - pad) + 4);
    ctx.fillText(opts.fmt ? opts.fmt(lo + pad) : "", right + 6, Y(lo + pad) + 4);
    if (opts.exitLine) {                                   // orange dashed "Risk trailing stop"
      const y = Y(opts.exitLine);
      ctx.save(); ctx.setLineDash([5, 4]); ctx.strokeStyle = C.orange; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(w - 4, y); ctx.stroke(); ctx.restore();
      ctx.fillStyle = C.orange; ctx.font = font(11);
      ctx.fillText("Risk trailing stop", left + 4, y - 6);
    }
  }

  /* ---------------- Analyst score ring 0-100, green from greenFrom */
  function ring(canvas, score, greenFrom) {
    const { ctx, w, h } = setup(canvas);
    const r = Math.min(w, h) / 2 - 8, cx = w / 2, cy = h / 2;
    ctx.lineWidth = 10; ctx.lineCap = "round";
    ctx.strokeStyle = C.card2; ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();
    const has = score !== null && score !== undefined && isFinite(+score);
    if (has) {
      const s = Math.max(0, Math.min(100, +score));
      ctx.strokeStyle = s >= (greenFrom || 70) ? C.green : "#ffd60a";
      ctx.beginPath(); ctx.arc(cx, cy, r, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * s / 100); ctx.stroke();
    }
    ctx.fillStyle = has ? "#fff" : C.text3; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.font = `700 ${Math.round(r * 0.7)}px -apple-system, "SF Pro Display", sans-serif`;
    ctx.fillText(has ? String(Math.round(+score)) : "–", cx, cy + 1);
    ctx.textAlign = "left";
  }

  window.DeskChart = { LineChart, candles, ring };
})();
