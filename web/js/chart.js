// Elevation (area) + speed (line) profile against ridden distance, drawn on a canvas.
// No chart library: one profile, fully controlled, no dependency to keep updated.
import { t } from './i18n.js';
import * as fmt from './format.js';

const PAD = { l: 48, r: 52, t: 26, b: 24 };

function css(el, name) {
  return getComputedStyle(el).getPropertyValue(name).trim();
}

function niceStep(range, targetTicks) {
  const raw = range / targetTicks;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  return (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
}

export class ProfileChart {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.data = null;
    this.cursor = null; // hovered point
    this.pinned = null; // clicked point, kept until another click or another ride
    this.hoverHandlers = new Set();
    this.selectHandlers = new Set();
    new ResizeObserver(() => this.draw()).observe(canvas);
    matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => this.draw());
    canvas.addEventListener('pointermove', (e) => this._pointer(e));
    canvas.addEventListener('pointerleave', () => { this.setCursor(null); this._emit(null); });
    // Click (or tap) = select that point; the app pans the map to it.
    canvas.addEventListener('click', (e) => {
      const i = this._indexAt(e);
      if (i == null) return;
      this.setPinned(i);
      for (const fn of this.selectHandlers) fn(i);
    });
  }

  onHover(fn) { this.hoverHandlers.add(fn); }
  onSelect(fn) { this.selectHandlers.add(fn); }
  _emit(i) { for (const fn of this.hoverHandlers) fn(i); }

  setData(pts) {
    this.data = pts;
    this.cursor = null;
    this.pinned = null;
    const eles = pts.ele.filter((v) => v != null);
    this.hasEle = eles.length > 1;
    this.eleMin = this.hasEle ? Math.min(...eles) : 0;
    this.eleMax = this.hasEle ? Math.max(...eles) : 1;
    this.distMax = pts.dist[pts.length - 1] || 1;
    this.speedMax = Math.max(10, ...pts.speed.map(fmt.kmh));
    this.draw();
  }

  setCursor(i) {
    this.cursor = i;
    this.draw();
  }

  setPinned(i) {
    this.pinned = i;
    this.draw();
  }

  _geom() {
    const w = this.canvas.clientWidth;
    const h = this.canvas.clientHeight;
    return { w, h, x0: PAD.l, x1: w - PAD.r, y0: PAD.t, y1: h - PAD.b };
  }

  _pointer(e) {
    const i = this._indexAt(e);
    if (i == null) return;
    this.setCursor(i);
    this._emit(i);
  }

  /** Track point index under a pointer event (by ridden distance along the x axis). */
  _indexAt(e) {
    if (!this.data) return null;
    const { x0, x1 } = this._geom();
    const rect = this.canvas.getBoundingClientRect();
    const x = Math.min(Math.max(e.clientX - rect.left, x0), x1);
    const target = ((x - x0) / (x1 - x0)) * this.distMax;
    const d = this.data.dist;
    let lo = 0, hi = d.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (d[mid] < target) lo = mid + 1; else hi = mid;
    }
    return lo;
  }

  draw() {
    const c = this.canvas;
    const dpr = window.devicePixelRatio || 1;
    const { w, h, x0, x1, y0, y1 } = this._geom();
    if (!w || !h) return;
    if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
      c.width = Math.round(w * dpr);
      c.height = Math.round(h * dpr);
    }
    const g = this.ctx;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    if (!this.data || this.data.length < 2) return;

    const col = {
      grid: css(c, '--chart-grid'), text: css(c, '--muted'), ele: css(c, '--chart-ele'),
      eleLine: css(c, '--chart-ele-line'), speed: css(c, '--chart-speed'), cursor: css(c, '--text'),
    };
    const p = this.data;
    const X = (dist) => x0 + (dist / this.distMax) * (x1 - x0);
    const eleSpan = Math.max(this.eleMax - this.eleMin, 20);
    const eleBase = this.eleMin - eleSpan * 0.1;
    const Ye = (e) => y1 - ((e - eleBase) / (eleSpan * 1.2)) * (y1 - y0);
    const Ys = (kmh) => y1 - (kmh / (this.speedMax * 1.1)) * (y1 - y0);

    g.font = '11px system-ui, sans-serif';
    g.lineWidth = 1;

    // x grid (distance)
    const xStep = niceStep(this.distMax, Math.max(2, Math.floor((x1 - x0) / 90)));
    g.strokeStyle = col.grid; g.fillStyle = col.text; g.textAlign = 'center'; g.textBaseline = 'top';
    for (let d = 0; d <= this.distMax + 1e-6; d += xStep) {
      const x = X(d);
      g.beginPath(); g.moveTo(x, y0); g.lineTo(x, y1); g.stroke();
      g.fillText(fmt.distance(d), x, y1 + 6);
    }

    // elevation area + left axis
    if (this.hasEle) {
      const yStep = niceStep(eleSpan * 1.2, 4);
      g.textAlign = 'right'; g.textBaseline = 'middle';
      for (let e = Math.ceil(eleBase / yStep) * yStep; e <= eleBase + eleSpan * 1.2; e += yStep) {
        const y = Ye(e);
        g.strokeStyle = col.grid; g.beginPath(); g.moveTo(x0, y); g.lineTo(x1, y); g.stroke();
        g.fillStyle = col.text; g.fillText(fmt.elevation(e), x0 - 6, y);
      }
      g.beginPath();
      let started = false;
      for (let i = 0; i < p.length; i++) {
        if (p.ele[i] == null) continue;
        const x = X(p.dist[i]), y = Ye(p.ele[i]);
        if (!started) { g.moveTo(x, y1); g.lineTo(x, y); started = true; } else g.lineTo(x, y);
      }
      g.lineTo(X(this.distMax), y1); g.closePath();
      g.fillStyle = col.ele; g.fill();
      g.beginPath(); started = false;
      for (let i = 0; i < p.length; i++) {
        if (p.ele[i] == null) continue;
        const x = X(p.dist[i]), y = Ye(p.ele[i]);
        if (!started) { g.moveTo(x, y); started = true; } else g.lineTo(x, y);
      }
      g.strokeStyle = col.eleLine; g.lineWidth = 1.5; g.stroke(); g.lineWidth = 1;
    }

    // speed line + right axis
    const sStep = niceStep(this.speedMax * 1.1, 4);
    g.textAlign = 'left'; g.textBaseline = 'middle'; g.fillStyle = col.speed;
    for (let s = 0; s <= this.speedMax * 1.1; s += sStep) g.fillText(new Intl.NumberFormat().format(s), x1 + 6, Ys(s));
    g.beginPath();
    for (let i = 0; i < p.length; i++) {
      const x = X(p.dist[i]), y = Ys(fmt.kmh(p.speed[i]));
      if (i === 0 || p.segment[i] !== p.segment[i - 1]) g.moveTo(x, y); else g.lineTo(x, y);
    }
    g.strokeStyle = col.speed; g.lineWidth = 1.25; g.stroke(); g.lineWidth = 1;

    // series labels
    g.textBaseline = 'top'; g.textAlign = 'left'; g.fillStyle = col.eleLine;
    if (this.hasEle) g.fillText(t('chart.elevation'), x0, 6);
    g.textAlign = 'right'; g.fillStyle = col.speed;
    g.fillText(t('legend.title'), x1, 6);

    // pinned point: dashed accent line, always visible
    if (this.pinned != null) {
      const x = X(p.dist[this.pinned]);
      g.save(); g.setLineDash([4, 3]); g.lineWidth = 1.5; g.strokeStyle = css(c, '--accent');
      g.beginPath(); g.moveTo(x, y0); g.lineTo(x, y1); g.stroke(); g.restore();
    }

    // label: the hovered point, or the pinned one when the pointer is elsewhere
    const shown = this.cursor ?? this.pinned;
    if (shown != null) {
      const i = shown;
      const x = X(p.dist[i]);
      if (this.cursor != null) { g.strokeStyle = col.cursor; g.beginPath(); g.moveTo(x, y0); g.lineTo(x, y1); g.stroke(); }
      const label = [fmt.distance(p.dist[i]), p.ele[i] != null ? fmt.elevation(p.ele[i]) : null, fmt.speed(p.speed[i])]
        .filter(Boolean).join(' · ');
      g.font = '600 12px system-ui, sans-serif';
      const tw = g.measureText(label).width;
      const lx = Math.min(Math.max(x - tw / 2, x0), x1 - tw);
      g.fillStyle = css(c, '--surface'); g.fillRect(lx - 4, y0 + 2, tw + 8, 18);
      g.fillStyle = col.cursor; g.textAlign = 'left'; g.textBaseline = 'top'; g.fillText(label, lx, y0 + 5);
    }
  }
}
