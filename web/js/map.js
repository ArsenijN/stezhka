// Ride map: speed-coloured track, stops, and a cursor shared with the profile chart.
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

const BLANK_STYLE = {
  version: 8,
  sources: {},
  layers: [{ id: 'bg', type: 'background', paint: { 'background-color': '#e9ece9' } }],
};

function speedColorExpr() {
  const expr = ['interpolate', ['linear'], ['get', 'kmh']];
  for (const [v, c] of SPEED_STOPS) expr.push(v, c);
  return expr;
}

export class RideMap {
  constructor(container, styleUrl) {
    this.map = new maplibregl.Map({
      container,
      style: styleUrl,
      center: UZHHOROD,
      zoom: 12,
      attributionControl: { compact: true },
    });
    this.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
    this.map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-right');
    this.hoverHandlers = new Set();
    this.data = null;
    this.ready = new Promise((resolve) => {
      let styleFailed = false;
      this.map.once('error', (e) => {
        // Basemap unreachable (offline, provider down): keep the app usable on a blank background.
        if (!this.map.isStyleLoaded() && !styleFailed) {
          styleFailed = true;
          console.warn('basemap failed, using blank style', e?.error);
          this.map.setStyle(BLANK_STYLE);
        }
      });
      this.map.once('load', () => { this._addLayers(); resolve(); });
    });
  }

  _addLayers() {
    const m = this.map;
    const empty = { type: 'FeatureCollection', features: [] };
    m.addSource('track', { type: 'geojson', data: empty });
    m.addSource('stops', { type: 'geojson', data: empty });
    m.addSource('cursor', { type: 'geojson', data: empty });
    m.addLayer({
      id: 'track-casing', type: 'line', source: 'track',
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: { 'line-color': '#1b1f1d', 'line-width': ['interpolate', ['linear'], ['zoom'], 10, 4, 16, 9], 'line-opacity': 0.55 },
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

  /** @param pts columnar points from api.columns(); @param stops ride.stops */
  async showRide(pts, stops) {
    await this.ready;
    this.data = pts;
    const features = [];
    for (let i = 0; i < pts.length - 1; i++) {
      if (pts.segment[i] !== pts.segment[i + 1]) continue;
      features.push({
        type: 'Feature',
        properties: { i, kmh: fmt.kmh((pts.speed[i] + pts.speed[i + 1]) / 2) },
        geometry: { type: 'LineString', coordinates: [[pts.lon[i], pts.lat[i]], [pts.lon[i + 1], pts.lat[i + 1]]] },
      });
    }
    this.map.getSource('track').setData({ type: 'FeatureCollection', features });
    this.map.getSource('stops').setData({
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
    const src = this.map.getSource('cursor');
    if (!src) return;
    const d = this.data;
    src.setData({
      type: 'FeatureCollection',
      features: i == null || !d ? [] : [{ type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: [d.lon[i], d.lat[i]] } }],
    });
  }

  resize() { this.map.resize(); }
}
