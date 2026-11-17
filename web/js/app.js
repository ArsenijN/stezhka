// App shell: login, ride list, ride view. All user data is inserted with textContent, never HTML
// (ride names come from GPX files).
import { api, ApiError, columns } from './api.js';
import { initI18n, setLocale, locale, locales, onLocaleChange, t, has } from './i18n.js';
import * as fmt from './format.js';
import { RideMap, SPEED_STOPS } from './map.js';
import { ProfileChart } from './chart.js';

const $ = (id) => document.getElementById(id);

const state = {
  rides: [],
  ride: null, // detail of the selected ride
  points: null,
  map: null,
  chart: null,
  config: null,
};

function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') n.className = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const c of children) if (c != null) n.append(c);
  return n;
}

function errorText(err) {
  const code = err instanceof ApiError ? err.code : 'unknown';
  return has(`errors.${code}`) ? t(`errors.${code}`) : t('errors.unknown');
}

let toastTimer;
function toast(message, kind = 'info') {
  const box = $('toast');
  box.textContent = message;
  box.dataset.kind = kind;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 5000);
}

// --- login --------------------------------------------------------------------------------------

function showLogin(passwordSet) {
  $('app').hidden = true;
  $('login').hidden = false;
  $('login-nopw').hidden = passwordSet;
  $('pw').focus();
}

async function onLogin(e) {
  e.preventDefault();
  const err = $('login-error');
  err.textContent = '';
  const btn = e.submitter;
  if (btn) btn.disabled = true;
  try {
    await api('login', { method: 'POST', json: { password: $('pw').value } });
    $('pw').value = '';
    await startApp();
  } catch (ex) {
    err.textContent = errorText(ex);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function onLogout() {
  try { await api('logout', { method: 'POST' }); } catch { /* session is gone either way */ }
  location.hash = '';
  showLogin(true);
}

// --- ride list ----------------------------------------------------------------------------------

function renderRideList() {
  const list = $('ride-list');
  list.replaceChildren();
  $('ride-count').textContent = t('rides.count', { count: state.rides.length });
  $('no-rides').hidden = state.rides.length > 0;
  for (const r of state.rides) {
    const selected = state.ride?.id === r.id;
    const meta = [fmt.distance(r.distance_m), fmt.duration(r.moving_s), fmt.speed(r.avg_moving_mps)].join(' · ');
    const item = el('li', {},
      el('a', { href: `#ride=${r.id}`, class: 'ride-item', 'aria-current': selected ? 'true' : 'false' },
        el('span', { class: 'ride-date' }, fmt.weekdayDate(r.started_at), ' ', fmt.time(r.started_at)),
        el('span', { class: 'ride-name' }, r.name || t('rides.untitled')),
        el('span', { class: 'ride-meta' }, meta),
        r.bike_name ? el('span', { class: 'ride-bike' }, r.bike_name) : null,
      ));
    list.append(item);
  }
}

async function loadRides() {
  state.rides = (await api('rides')).rides;
  renderRideList();
}

// --- ride view ----------------------------------------------------------------------------------

function stat(label, value, extraClass = '') {
  return el('div', { class: `stat ${extraClass}` },
    el('span', { class: 'stat-label' }, label),
    el('span', { class: 'stat-value' }, value));
}

function renderStats() {
  const box = $('stats');
  box.replaceChildren();
  const r = state.ride;
  if (!r) {
    box.append(el('p', { class: 'muted' }, state.rides.length ? t('rides.select') : t('rides.empty')));
    return;
  }
  const stops = r.stops.filter((s) => s.kind === 'stop');
  const stopTime = stops.reduce((a, s) => a + (s.end_t - s.start_t), 0);
  box.append(
    el('h2', { class: 'ride-title' }, r.name || t('rides.untitled'),
      el('span', { class: 'muted' }, ' · ', fmt.dateTime(r.started_at))),
    el('div', { class: 'stat-grid' },
      stat(t('stats.distance'), fmt.distance(r.distance_m), 'primary'),
      stat(t('stats.moving_time'), fmt.duration(r.moving_s), 'primary'),
      stat(t('stats.avg_speed'), fmt.speed(r.avg_moving_mps), 'primary'),
      stat(t('stats.max_speed'), fmt.speed(r.max_mps)),
      stat(t('stats.elapsed_time'), fmt.duration(r.elapsed_s)),
      stat(t('stats.climb'), `↑ ${fmt.elevation(r.ascent_m)}  ↓ ${fmt.elevation(r.descent_m)}`),
      stat(t('stats.stops'), t('stats.stop_summary', { count: stops.length, time: fmt.duration(stopTime) })),
    ),
    weatherRow(r),
    el('p', { class: 'note' }, r.ele_source === 'dem' ? t('stats.elevation_source_dem') : t('stats.elevation_source_gps')),
  );
}

function windText(w) {
  const base = t('weather.wind', { speed: fmt.windSpeed(w.wind_speed_mps), dir: t(`weather.dir.${fmt.compass(w.wind_dir_deg)}`) });
  const h = w.headwind_mps;
  if (h == null) return base;
  if (h > 0.5) return `${base} · ${t('weather.headwind', { speed: fmt.windSpeed(h) })}`;
  if (h < -0.5) return `${base} · ${t('weather.tailwind', { speed: fmt.windSpeed(h) })}`;
  return `${base} · ${t('weather.crosswind')}`;
}

function weatherRow(r) {
  const w = r.weather;
  if (!w) {
    return el('p', { class: 'note' }, r.enrich_error ? t('weather.unavailable') : t('weather.pending'));
  }
  let road = t(`weather.wetness.${w.wetness}`);
  if (w.precip_ride_mm >= 0.1) road += ` · ${t('weather.rain_during', { amount: fmt.precipitation(w.precip_ride_mm) })}`;
  return el('div', { class: 'stat-grid weather' },
    stat(t('stats.temperature'), fmt.temperature(w.temperature_c)),
    stat(t('stats.wind'), windText(w)),
    stat(t('stats.road'), road),
  );
}

function renderLegend() {
  const box = $('legend');
  const min = SPEED_STOPS[0][0];
  const max = SPEED_STOPS[SPEED_STOPS.length - 1][0];
  const grad = SPEED_STOPS.map(([v, c]) => `${c} ${((v - min) / (max - min)) * 100}%`).join(', ');
  const bar = el('div', { class: 'legend-bar' });
  bar.style.background = `linear-gradient(to right, ${grad})`;
  const ticks = el('div', { class: 'legend-ticks' });
  for (const [v] of SPEED_STOPS) {
    const tick = el('span', {}, String(v));
    tick.style.left = `${((v - min) / (max - min)) * 100}%`;
    ticks.append(tick);
  }
  box.replaceChildren(el('div', { class: 'legend-title' }, t('legend.title')), bar, ticks);
}

async function selectRide(id) {
  if (!id) {
    state.ride = null;
    renderRideList();
    renderStats();
    return;
  }
  try {
    const [ride, pts] = await Promise.all([api(`rides/${id}`), api(`rides/${id}/points`)]);
    state.ride = ride;
    state.points = columns(pts);
  } catch (ex) {
    toast(errorText(ex), 'error');
    if (ex instanceof ApiError && ex.status === 401) return showLogin(true);
    return;
  }
  renderRideList();
  renderStats();
  state.chart.setData(state.points);
  await state.map.showRide(state.points, state.ride.stops);
}

function rideIdFromHash() {
  const m = /ride=(\d+)/.exec(location.hash);
  return m ? Number(m[1]) : null;
}

// --- upload -------------------------------------------------------------------------------------

async function onUpload(e) {
  const files = [...e.target.files];
  e.target.value = '';
  let created = 0;
  let lastId = null;
  for (const f of files) {
    try {
      const res = await api('rides', { method: 'POST', body: f, headers: { 'X-Filename': encodeURIComponent(f.name) } });
      if (res.created) { created++; lastId = res.id; } else toast(t('upload.duplicate', { name: f.name }));
    } catch (ex) {
      toast(t('upload.failed', { name: f.name, reason: errorText(ex) }), 'error');
    }
  }
  if (created) {
    toast(t('upload.done', { count: created }));
    await loadRides();
    location.hash = `ride=${lastId}`;
  }
}

// --- boot ---------------------------------------------------------------------------------------

function buildLanguagePicker() {
  const sel = $('lang');
  sel.replaceChildren(...locales().map((l) => el('option', { value: l.code }, l.name)));
  sel.value = locale();
  sel.addEventListener('change', () => setLocale(sel.value));
}

let appStarted = false;
async function startApp() {
  $('login').hidden = true;
  $('app').hidden = false;
  if (!appStarted) {
    appStarted = true;
    state.config = await api('config');
    state.map = new RideMap($('map'), state.config.basemap, state.config.attributions);
    $('map-note').hidden = state.config.basemap.mode !== 'none';
    state.chart = new ProfileChart($('chart'));
    state.chart.onHover((i) => state.map.setCursor(i));
    state.map.onHover((i) => state.chart.setCursor(i));
    renderLegend();
    window.addEventListener('hashchange', () => selectRide(rideIdFromHash()));
  }
  await loadRides();
  const id = rideIdFromHash() ?? state.rides[0]?.id ?? null;
  if (id && rideIdFromHash() == null) history.replaceState(null, '', `#ride=${id}`);
  await selectRide(id);
}

async function boot() {
  await initI18n();
  buildLanguagePicker();
  onLocaleChange(() => {
    $('lang').value = locale();
    if (!appStarted) return;
    renderRideList();
    renderStats();
    renderLegend();
    state.chart.draw();
  });
  $('login-form').addEventListener('submit', onLogin);
  $('logout').addEventListener('click', onLogout);
  $('upload').addEventListener('change', onUpload);

  let session;
  try {
    session = await api('session');
  } catch (ex) {
    toast(errorText(ex), 'error');
    return;
  }
  if (session.authenticated) await startApp();
  else showLogin(session.password_set);
}

boot();
