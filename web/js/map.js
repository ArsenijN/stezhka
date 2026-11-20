// Ride map: speed-coloured track, stops, and a cursor shared with the profile chart.
//
// Basemap modes (from /api/config):
//   local  - self-hosted OpenFreeMap extract (PMTiles via HTTP range requests) + local fonts/sprites
//   remote - tiles.openfreemap.org (opt-in fallback on the server)
//   none   - plain background
// The basemap follows the system light/dark theme; ride layers are re-added after each style change.
import * as maplibregl from '../vendor/maplibre-gl/maplibre-gl.mjs';
import { t } from './i18n.js';
import * as fmt from './format.js';
import { MARK_COLORS, markKind } from './feelings.js';

// km/h -> colour. Shared with the legend. Dark red (crawling) to violet (fast descent).
export const SPEED_STOPS = [
  [0, '#7a1020'],
  [8, '#d7301f'],
  [14, '#f28e2b'],
  [19, '#e9c62b'],
  [24, '#59a14f'],
  [30, '#1f8fbf'],
  [40, '#5b3fa8'],
];

export const COMPARE_COLOR = '#7c3aed';
// this ride's speed / usual speed on a segment -> colour: orange = slower, grey = as usual, blue = faster
export const USUAL_STOPS = [
  [0.7, '#c2410c'],
  [0.9, '#f59e0b'],
  [1.0, '#a3a3a3'],
  [1.1, '#38bdf8'],
  [1.3, '#1d4ed8'],
];
const STOP_COLORS = { stop: '#d7301f', break: '#6b7280', pause: '#1f2937', slowdown: '#f59e0b' };
const UZHHOROD = [22.2879, 48.6208];
const EMPTY = { type: 'FeatureCollection', features: [] };
const darkQuery = matchMedia('(prefers-color-scheme: dark)');

// PMTiles protocol (vendor/pmtiles/pmtiles.js is a classic script exposing `pmtiles`).
if (globalThis.pmtiles) {
  const protocol = new globalThis.pmtiles.Protocol();
  maplibregl.addProtocol('pmtiles', protocol.tile);
}

const theme = () => (darkQuery.matches ? 'dark' : 'light');
const appBase = () => new URL('.', location.href).href;

function blankStyle(th) {
  return {
    version: 8,
    sources: {},
    layers: [{ id: 'bg', type: 'background', paint: { 'background-color': th === 'dark' ? '#1d2320' : '#e9ece9' } }],
  };
}

function speedColorExpr() {
  const expr = ['interpolate', ['linear'], ['get', 'kmh']];
  for (const [v, c] of SPEED_STOPS) expr.push(v, c);
  return expr;
}

/**
 * Group consecutive track intervals with the same speed colour (2 km/h steps) into one LineString.
 * One feature per 1 s interval made each piece ~4 m long; MapLibre drops features smaller than about
 * a pixel when zoomed out, so dense (iPhone) tracks showed gaps or vanished until zoomed in.
 */
export function speedRuns(pts, stepKmh = 2) {
  const runs = [];
  let run = null;
  for (let i = 0; i < pts.length - 1; i++) {
    if (pts.segment[i] !== pts.segment[i + 1]) { run = null; continue; }
    const v = fmt.kmh((pts.speed[i] + pts.speed[i + 1]) / 2);
    const bucket = Math.round(v / stepKmh);
    if (run && run.bucket === bucket && run.i1 === i) {
      run.coords.push([pts.lon[i + 1], pts.lat[i + 1]]);
      run.i1 = i + 1;
      run.sum += v;
      run.n += 1;
    } else {
      run = { bucket, i0: i, i1: i + 1, sum: v, n: 1, coords: [[pts.lon[i], pts.lat[i]], [pts.lon[i + 1], pts.lat[i + 1]]] };
      runs.push(run);
    }
  }
  return runs.map((r) => ({
    type: 'Feature',
    properties: { i0: r.i0, i1: r.i1, kmh: r.sum / r.n },
    geometry: { type: 'LineString', coordinates: r.coords },
  }));
}

export class RideMap {
  constructor(container, basemap, attributions = []) {
    this.basemap = basemap || { mode: 'none', styles: {} };
    this.track = EMPTY;
    this.stops = EMPTY;
    this.cursor = EMPTY;
    this.compare = EMPTY;
    this.marks = EMPTY;
    this.roads = EMPTY;
    this.roadmarks = EMPTY;
    this.usual = EMPTY; // this ride against the usual speed per segment (null-free FeatureCollection)
    this.usualClickHandlers = new Set();
    this.hiddenMarksRide = -1; // the selected ride: its own marks are drawn from the ride, not twice
    this.roadClickHandlers = new Set();
    this.roadMarkClickHandlers = new Set();
    this.points = null;
    this.hoverHandlers = new Set();
    this.selectHandlers = new Set();
    this.map = new maplibregl.Map({
      container,
      style: blankStyle(theme()),
      center: UZHHOROD,
      zoom: 12,
      // Style line widths are in screen pixels and stop growing at z20, while tiles hold detail only
      // to z14: beyond ~19 roads look absurdly thin next to a GPS track (seen on a bridge).
      maxZoom: 19,
      attributionControl: { compact: true, customAttribution: attributions },
    });
    this.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
    this.map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-right');
    this.map.on('style.load', () => this._addLayers());
    // OFM's dark style references sprite images its own sprite sheet lacks ("wood-pattern"):
    // supply a transparent placeholder instead of a console warning per tile.
    this.map.on('styleimagemissing', (e) => {
      if (!this.map.hasImage(e.id)) this.map.addImage(e.id, { width: 1, height: 1, data: new Uint8Array(4) });
    });
    this._bindEvents();
    this.ready = new Promise((resolve) => this.map.once('load', resolve));
    // On a narrow map MapLibre opens the compact attribution at start, covering the legend; keep it
    // behind its (i) button like it is after the first drag.
    this.ready.then(() => container.querySelector('.maplibregl-compact-show')?.classList.remove('maplibregl-compact-show'));
    darkQuery.addEventListener('change', () => this._applyBasemap());
    this._applyBasemap();
  }

  async _styleFor(th) {
    const bm = this.basemap;
    if (bm.mode === 'local') {
      const r = await fetch(bm.styles[th], { credentials: 'same-origin' });
      if (!r.ok) throw new Error(`style HTTP ${r.status}`);
      return JSON.parse((await r.text()).replaceAll(bm.base_token, appBase()));
    }
    if (bm.mode === 'remote') return bm.styles[th];
    return blankStyle(th);
  }

  async _applyBasemap() {
    const th = theme();
    let style;
    try {
      style = await this._styleFor(th);
    } catch (e) {
      console.warn('basemap unavailable, using plain background', e);
      style = blankStyle(th);
    }
    this.map.setStyle(style);
  }

  _addLayers() {
    const m = this.map;
    const dark = theme() === 'dark';
    if (m.getSource('track')) return;
    // tolerance 0: no simplification, so short pieces of a dense (1 s) track are never dropped
    m.addSource('track', { type: 'geojson', data: this.track, tolerance: 0 });
    m.addSource('stops', { type: 'geojson', data: this.stops });
    m.addSource('cursor', { type: 'geojson', data: this.cursor });
    m.addSource('compare', { type: 'geojson', data: this.compare });
    m.addSource('marks', { type: 'geojson', data: this.marks, tolerance: 0 });
    m.addSource('roads', { type: 'geojson', data: this.roads, tolerance: 0 });
    m.addSource('roadmarks', { type: 'geojson', data: this.roadmarks, tolerance: 0 });
    m.addSource('usual', { type: 'geojson', data: this.usual, tolerance: 0 });
    // "My roads": every ridden segment, coloured by typical speed, thicker when ridden more often
    const ridden = ['min', ['get', 'rides'], 8];
    m.addLayer({
      id: 'roads', type: 'line', source: 'roads',
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: {
        'line-color': ['case', ['==', ['get', 'kmh'], null], '#9ca3af', speedColorExpr()],
        'line-width': ['interpolate', ['linear'], ['zoom'], 12, ['+', 1.5, ['*', 0.3, ridden]], 17, ['+', 4, ['*', 0.8, ridden]]],
        'line-opacity': 0.85,
      },
    });
    const markColor = ['match', ['get', 'kind'], 'bad', MARK_COLORS.bad, 'good', MARK_COLORS.good, 'draft', MARK_COLORS.draft, MARK_COLORS.note];
    // marks of every ride on "My roads": a halo along the road, problems drawn on top of good things
    m.addLayer({
      id: 'roadmarks-line', type: 'line', source: 'roadmarks', filter: this._roadMarksFilter('LineString'),
      layout: { 'line-join': 'round', 'line-cap': 'round', 'line-sort-key': ['match', ['get', 'kind'], 'bad', 2, 'good', 1, 0] },
      paint: { 'line-color': markColor, 'line-width': ['interpolate', ['linear'], ['zoom'], 12, 7, 17, 18], 'line-opacity': 0.35 },
    });
    // marked stretches: a wide halo under the track
    m.addLayer({
      id: 'marks-line', type: 'line', source: 'marks', filter: ['==', ['geometry-type'], 'LineString'],
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: { 'line-color': markColor, 'line-width': ['interpolate', ['linear'], ['zoom'], 10, 10, 16, 22], 'line-opacity': 0.4 },
    });
    // Second ride for comparison, drawn under the main track.
    m.addLayer({
      id: 'compare', type: 'line', source: 'compare',
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: { 'line-color': COMPARE_COLOR, 'line-width': ['interpolate', ['linear'], ['zoom'], 10, 2.5, 16, 5], 'line-dasharray': [1.5, 1.5], 'line-opacity': 0.9 },
    });
    m.addLayer({
      id: 'track-casing', type: 'line', source: 'track',
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: {
        'line-color': dark ? '#f2f4f1' : '#1b1f1d',
        'line-width': ['interpolate', ['linear'], ['zoom'], 10, 4, 16, 9],
        'line-opacity': dark ? 0.35 : 0.55,
      },
    });
    m.addLayer({
      id: 'track', type: 'line', source: 'track',
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: { 'line-color': speedColorExpr(), 'line-width': ['interpolate', ['linear'], ['zoom'], 10, 2.5, 16, 6] },
    });
    // "vs usual": the ride's matched passes over the faded track
    const ratio = ['interpolate', ['linear'], ['get', 'ratio']];
    for (const [v, c] of USUAL_STOPS) ratio.push(v, c);
    m.addLayer({
      id: 'usual', type: 'line', source: 'usual',
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: { 'line-color': ratio, 'line-width': ['interpolate', ['linear'], ['zoom'], 10, 3, 16, 7] },
    });
    if (this.usual.features.length) m.setPaintProperty('track', 'line-opacity', 0.25);
    // Invisible wide line: makes the track easy to hover with a mouse or finger.
    m.addLayer({
      id: 'track-hit', type: 'line', source: 'track',
      paint: { 'line-color': '#000', 'line-width': 18, 'line-opacity': 0 },
    });
    m.addLayer({
      id: 'stops', type: 'circle', source: 'stops',
      paint: {
        'circle-radius': ['case', ['==', ['get', 'kind'], 'slowdown'], 5, ['interpolate', ['linear'], ['get', 'duration'], 5, 4, 60, 7, 300, 10]],
        'circle-color': ['match', ['get', 'kind'], 'stop', STOP_COLORS.stop, 'break', STOP_COLORS.break, 'slowdown', STOP_COLORS.slowdown, STOP_COLORS.pause],
        'circle-stroke-color': '#fff',
        'circle-stroke-width': 2,
      },
    });
    // marked spots: rings, unlike the filled stop dots
    m.addLayer({
      id: 'marks-spot', type: 'circle', source: 'marks', filter: ['==', ['geometry-type'], 'Point'],
      paint: { 'circle-radius': 9, 'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-color': markColor, 'circle-stroke-width': 4 },
    });
    m.addLayer({
      id: 'roadmarks-spot', type: 'circle', source: 'roadmarks', filter: this._roadMarksFilter('Point'),
      paint: { 'circle-radius': 7, 'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-color': markColor, 'circle-stroke-width': 3, 'circle-stroke-opacity': 0.8 },
    });
    m.addLayer({
      id: 'cursor', type: 'circle', source: 'cursor',
      paint: { 'circle-radius': 7, 'circle-color': '#fff', 'circle-stroke-color': '#111', 'circle-stroke-width': 3 },
    });
  }

  _bindEvents() {
    const m = this.map;
    m.on('mousemove', 'track-hit', (e) => {
      m.getCanvas().style.cursor = 'crosshair';
      const props = e.features?.[0]?.properties;
      if (props?.i0 != null) this._emitHover(this._nearestIndex(props.i0, props.i1, e.lngLat));
    });
    m.on('mouseleave', 'track-hit', () => {
      m.getCanvas().style.cursor = '';
      this._emitHover(null);
    });
    // Click / tap on the track = pin that point (shared with the chart).
    m.on('click', 'track-hit', (e) => {
      const props = e.features?.[0]?.properties;
      if (props?.i0 == null) return;
      const i = this._nearestIndex(props.i0, props.i1, e.lngLat);
      for (const fn of this.selectHandlers) fn(i);
    });
    m.on('click', 'stops', (e) => {
      const p = e.features?.[0]?.properties;
      if (!p) return;
      const box = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = t(`stop_kind.${p.kind}`);
      const body = document.createElement('div');
      body.textContent = p.kind === 'slowdown'
        ? `${fmt.time(p.start_t)} · ${t('stops.slowed', { from: fmt.speed(p.v0), to: fmt.speed(p.v1) })}`
        : `${fmt.time(p.start_t)} · ${fmt.duration(p.duration)}`;
      box.append(title, body);
      if (p.kind === 'stop' || p.kind === 'slowdown') {
        const why = document.createElement('div');
        why.className = 'muted';
        why.textContent = p.reason ? t(`stop_reason.${p.reason}`) : t('stops.reason_unknown');
        box.append(why);
      }
      new maplibregl.Popup({ closeButton: false }).setLngLat(e.lngLat).setDOMContent(box).addTo(m);
    });
    m.on('mouseenter', 'stops', () => { m.getCanvas().style.cursor = 'pointer'; });
    m.on('click', 'roads', (e) => {
      // a mark or a stop under the finger has its own popup: one popup per tap
      if (m.queryRenderedFeatures(e.point, { layers: ['marks-line', 'marks-spot', 'roadmarks-line', 'roadmarks-spot', 'stops'] }).length) return;
      const p = e.features?.[0]?.properties;
      if (p) for (const fn of this.roadClickHandlers) fn(p.id, e.lngLat);
    });
    m.on('mouseenter', 'roads', () => { m.getCanvas().style.cursor = 'pointer'; });
    m.on('mouseleave', 'roads', () => { m.getCanvas().style.cursor = ''; });
    for (const layer of ['marks-line', 'marks-spot']) {
      m.on('click', layer, (e) => {
        const p = e.features?.[0]?.properties;
        if (!p || p.kind === 'draft') return;
        const box = document.createElement('div');
        const tags = JSON.parse(p.tags || '[]');
        if (tags.length) {
          const title = document.createElement('strong');
          title.textContent = tags.map((tg) => t(`tag.${tg}`)).join(', ');
          box.append(title);
        }
        if (p.note) {
          const body = document.createElement('div');
          body.textContent = p.note;
          box.append(body);
        }
        if (box.childElementCount) new maplibregl.Popup({ closeButton: false }).setLngLat(e.lngLat).setDOMContent(box).addTo(m);
      });
    }
    m.on('mouseleave', 'stops', () => { m.getCanvas().style.cursor = ''; });
    m.on('click', 'usual', (e) => {
      const p = e.features?.[0]?.properties;
      if (p) for (const fn of this.usualClickHandlers) fn(p, e.lngLat);
    });
    // one popup per tap: every road mark under the finger (several rides can mark one place)
    m.on('click', (e) => {
      if (!m.getLayer('roadmarks-line')) return;
      if (m.queryRenderedFeatures(e.point, { layers: ['marks-line', 'marks-spot', 'stops'] }).length) return;
      const hits = m.queryRenderedFeatures(e.point, { layers: ['roadmarks-spot', 'roadmarks-line'] });
      const ids = [...new Set(hits.map((f) => f.properties.id))];
      if (ids.length) for (const fn of this.roadMarkClickHandlers) fn(ids, e.lngLat);
    });
    for (const layer of ['roadmarks-line', 'roadmarks-spot']) {
      m.on('mouseenter', layer, () => { m.getCanvas().style.cursor = 'pointer'; });
      m.on('mouseleave', layer, () => { m.getCanvas().style.cursor = ''; });
    }
  }

  _roadMarksFilter(type) {
    const geom = type === 'Point' ? ['==', ['geometry-type'], 'Point'] : ['in', ['geometry-type'], ['literal', ['LineString', 'MultiLineString']]];
    return ['all', geom, ['!=', ['get', 'ride_id'], this.hiddenMarksRide]];
  }

  onHover(fn) { this.hoverHandlers.add(fn); }
  onSelect(fn) { this.selectHandlers.add(fn); }
  _emitHover(i) { for (const fn of this.hoverHandlers) fn(i); }

  _setData(id, fc) {
    this[id] = fc;
    this.map.getSource(id)?.setData(fc);
  }

  /** @param pts columnar points from api.columns(); @param stops ride.stops */
  async showRide(pts, stops) {
    await this.ready;
    this.points = pts;
    this.pinned = null;
    this._setData('track', { type: 'FeatureCollection', features: speedRuns(pts) });
    this._setData('stops', {
      type: 'FeatureCollection',
      features: stops.map((s) => ({
        type: 'Feature',
        properties: {
          kind: s.kind, duration: s.end_t - s.start_t, start_t: s.start_t, reason: s.reason || '',
          // slowdowns: the speed before and after, from the track
          v0: pts.speed?.[s.start_idx] ?? null, v1: pts.speed?.[s.end_idx] ?? null,
        },
        geometry: { type: 'Point', coordinates: [s.lon, s.lat] },
      })),
    });
    this._setData('marks', EMPTY); // the app sends this ride's marks after showRide
    this.setCursor(null);
    if (pts.length) {
      const b = new maplibregl.LngLatBounds();
      for (let i = 0; i < pts.length; i++) b.extend([pts.lon[i], pts.lat[i]]);
      this.map.fitBounds(b, { padding: 40, duration: 0, maxZoom: 16 });
    }
  }

  /** Overlay another ride's track (columnar points), or remove it with null. */
  showCompare(pts) {
    if (!pts) return this._setData('compare', EMPTY);
    const lines = [];
    let cur = [];
    for (let i = 0; i < pts.length; i++) {
      if (i > 0 && pts.segment[i] !== pts.segment[i - 1]) { if (cur.length > 1) lines.push(cur); cur = []; }
      cur.push([pts.lon[i], pts.lat[i]]);
    }
    if (cur.length > 1) lines.push(cur);
    this._setData('compare', {
      type: 'FeatureCollection',
      features: [{ type: 'Feature', properties: {}, geometry: { type: 'MultiLineString', coordinates: lines } }],
    });
  }

  /** Ridden segments from /api/roads, or null to hide the layer. */
  showRoads(segments) {
    this._setData('roads', !segments ? EMPTY : {
      type: 'FeatureCollection',
      features: segments.map((s) => ({
        type: 'Feature',
        properties: { id: s.id, rides: s.rides, kmh: s.speed_mps == null ? null : fmt.kmh(s.speed_mps) },
        geometry: { type: 'LineString', coordinates: s.geom },
      })),
    });
  }

  onRoadClick(fn) { this.roadClickHandlers.add(fn); }
  onRoadMarkClick(fn) { this.roadMarkClickHandlers.add(fn); }
  onUsualClick(fn) { this.usualClickHandlers.add(fn); }

  /** This ride's passes against the usual speed (/api/rides/{id}/usual `passes`), or null. */
  showUsual(passes) {
    this._setData('usual', !passes?.length ? EMPTY : {
      type: 'FeatureCollection',
      features: passes.map((p) => ({
        type: 'Feature',
        properties: { ratio: p.speed_mps / p.usual_mps, speed: p.speed_mps, usual: p.usual_mps, delta: p.delta_s, samples: p.samples },
        geometry: { type: 'LineString', coordinates: p.geom },
      })),
    });
    if (this.map.getLayer('track')) this.map.setPaintProperty('track', 'line-opacity', passes?.length ? 0.25 : 1);
  }

  /** Marks of every ride placed on the roads (/api/roads `marks`), or null to hide them. */
  showRoadMarks(marks) {
    this._setData('roadmarks', !marks ? EMPTY : {
      type: 'FeatureCollection',
      features: marks.map((mk) => ({
        type: 'Feature',
        properties: { id: mk.id, ride_id: mk.ride_id, kind: markKind(mk.tags) },
        geometry: mk.geom,
      })),
    });
  }

  /** Leave out one ride's road marks (the selected ride shows its own); null shows all. */
  hideRoadMarksOf(rideId) {
    this.hiddenMarksRide = rideId ?? -1;
    for (const [id, type] of [['roadmarks-line', 'LineString'], ['roadmarks-spot', 'Point']]) {
      if (this.map.getLayer(id)) this.map.setFilter(id, this._roadMarksFilter(type));
    }
  }

  /** No ride selected: remove its track, stops, marks, cursor and comparison. */
  clearRide() {
    this.points = null;
    this.pinned = null;
    for (const id of ['track', 'stops', 'marks', 'cursor', 'compare']) this._setData(id, EMPTY);
    this.showUsual(null);
  }

  /** Zoom to show every ridden segment. */
  fitRoads(segments) {
    if (!segments?.length) return;
    const b = new maplibregl.LngLatBounds();
    for (const s of segments) for (const c of s.geom) b.extend(c);
    this.map.fitBounds(b, { padding: 40, duration: 400, maxZoom: 16 });
  }

  popup(lngLat, node) {
    new maplibregl.Popup({ closeButton: false, maxWidth: '260px' }).setLngLat(lngLat).setDOMContent(node).addTo(this.map);
  }

  /** Marks: [{i0, i1, kind, tags, note}] by track point index; a spot has i0 === i1. */
  showMarks(marks) {
    const d = this.points;
    if (!d) return;
    this._setData('marks', {
      type: 'FeatureCollection',
      features: marks.map((mk) => {
        const props = { kind: mk.kind, tags: JSON.stringify(mk.tags || []), note: mk.note || '' };
        if (mk.i1 - mk.i0 < 1) {
          return { type: 'Feature', properties: props, geometry: { type: 'Point', coordinates: [d.lon[mk.i0], d.lat[mk.i0]] } };
        }
        const coords = [];
        for (let i = mk.i0; i <= mk.i1; i++) coords.push([d.lon[i], d.lat[i]]);
        return { type: 'Feature', properties: props, geometry: { type: 'LineString', coordinates: coords } };
      }),
    });
  }

  /** Glide to a track point, keeping the zoom (+/- then zoom into it, as they zoom around the centre). */
  panToPoint(i) {
    const d = this.points;
    if (!d || i == null) return;
    this.map.easeTo({ center: [d.lon[i], d.lat[i]], duration: 450 });
  }

  /** Index of the track point within [i0, i1] closest to the mouse position. */
  _nearestIndex(i0, i1, lngLat) {
    const d = this.points;
    const k = Math.cos((lngLat.lat * Math.PI) / 180);
    let best = i0;
    let bestD = Infinity;
    for (let i = i0; i <= i1; i++) {
      const dx = (d.lon[i] - lngLat.lng) * k;
      const dy = d.lat[i] - lngLat.lat;
      const dd = dx * dx + dy * dy;
      if (dd < bestD) { bestD = dd; best = i; }
    }
    return best;
  }

  /** Show the hovered point; with null, fall back to the pinned (clicked) point. */
  setCursor(i) {
    const d = this.points;
    const shown = i ?? this.pinned;
    this._setData('cursor', {
      type: 'FeatureCollection',
      features: shown == null || !d ? [] : [{ type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: [d.lon[shown], d.lat[shown]] } }],
    });
  }

  setPinned(i) {
    this.pinned = i;
    this.setCursor(null);
  }

  resize() { this.map.resize(); }
}
