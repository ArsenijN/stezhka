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

const STOP_COLORS = { stop: '#d7301f', break: '#6b7280', pause: '#1f2937' };
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

export class RideMap {
  constructor(container, basemap) {
    this.basemap = basemap || { mode: 'none', styles: {} };
    this.track = EMPTY;
    this.stops = EMPTY;
    this.cursor = EMPTY;
    this.points = null;
    this.hoverHandlers = new Set();
    this.map = new maplibregl.Map({
      container,
      style: blankStyle(theme()),
      center: UZHHOROD,
      zoom: 12,
      attributionControl: { compact: true },
    });
    this.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
    this.map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-right');
    this.map.on('style.load', () => this._addLayers());
    this._bindEvents();
    this.ready = new Promise((resolve) => this.map.once('load', resolve));
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
    m.addSource('track', { type: 'geojson', data: this.track });
    m.addSource('stops', { type: 'geojson', data: this.stops });
    m.addSource('cursor', { type: 'geojson', data: this.cursor });
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
    // Invisible wide line: makes the track easy to hover with a mouse or finger.
    m.addLayer({
      id: 'track-hit', type: 'line', source: 'track',
      paint: { 'line-color': '#000', 'line-width': 18, 'line-opacity': 0 },
    });
    m.addLayer({
      id: 'stops', type: 'circle', source: 'stops',
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['get', 'duration'], 5, 4, 60, 7, 300, 10],
        'circle-color': ['match', ['get', 'kind'], 'stop', STOP_COLORS.stop, 'break', STOP_COLORS.break, STOP_COLORS.pause],
        'circle-stroke-color': '#fff',
        'circle-stroke-width': 2,
      },
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
      const i = e.features?.[0]?.properties?.i;
      if (i != null) this._emitHover(i);
    });
    m.on('mouseleave', 'track-hit', () => {
      m.getCanvas().style.cursor = '';
      this._emitHover(null);
    });
    m.on('click', 'stops', (e) => {
      const p = e.features?.[0]?.properties;
      if (!p) return;
      const box = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = t(`stop_kind.${p.kind}`);
      const body = document.createElement('div');
      body.textContent = `${fmt.time(p.start_t)} · ${fmt.duration(p.duration)}`;
      box.append(title, body);
      new maplibregl.Popup({ closeButton: false }).setLngLat(e.lngLat).setDOMContent(box).addTo(m);
    });
    m.on('mouseenter', 'stops', () => { m.getCanvas().style.cursor = 'pointer'; });
    m.on('mouseleave', 'stops', () => { m.getCanvas().style.cursor = ''; });
  }

  onHover(fn) { this.hoverHandlers.add(fn); }
  _emitHover(i) { for (const fn of this.hoverHandlers) fn(i); }

  _setData(id, fc) {
    this[id] = fc;
    this.map.getSource(id)?.setData(fc);
  }

  /** @param pts columnar points from api.columns(); @param stops ride.stops */
  async showRide(pts, stops) {
    await this.ready;
    this.points = pts;
    const features = [];
    for (let i = 0; i < pts.length - 1; i++) {
      if (pts.segment[i] !== pts.segment[i + 1]) continue;
      features.push({
        type: 'Feature',
        properties: { i, kmh: fmt.kmh((pts.speed[i] + pts.speed[i + 1]) / 2) },
        geometry: { type: 'LineString', coordinates: [[pts.lon[i], pts.lat[i]], [pts.lon[i + 1], pts.lat[i + 1]]] },
      });
    }
    this._setData('track', { type: 'FeatureCollection', features });
    this._setData('stops', {
      type: 'FeatureCollection',
      features: stops.map((s) => ({
        type: 'Feature',
        properties: { kind: s.kind, duration: s.end_t - s.start_t, start_t: s.start_t },
        geometry: { type: 'Point', coordinates: [s.lon, s.lat] },
      })),
    });
    this.setCursor(null);
    if (pts.length) {
      const b = new maplibregl.LngLatBounds();
      for (let i = 0; i < pts.length; i++) b.extend([pts.lon[i], pts.lat[i]]);
      this.map.fitBounds(b, { padding: 40, duration: 0, maxZoom: 16 });
    }
  }

  setCursor(i) {
    const d = this.points;
    this._setData('cursor', {
      type: 'FeatureCollection',
      features: i == null || !d ? [] : [{ type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: [d.lon[i], d.lat[i]] } }],
    });
  }

  resize() { this.map.resize(); }
}
