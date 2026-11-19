// App shell: login, ride list, ride view. All user data is inserted with textContent, never HTML
// (ride names come from GPX files).
import { api, ApiError, columns } from './api.js';
import { initI18n, setLocale, locale, locales, onLocaleChange, t } from './i18n.js';
import * as fmt from './format.js';
import { RideMap, SPEED_STOPS } from './map.js';
import { ProfileChart } from './chart.js';
import { initSettings } from './settings.js';
import { $, el, errorText, icon, iconButton, rideTitle, TIMESTAMP_NAME, toast } from './ui.js';

const state = {
  rides: [],
  bikes: [],
  bikeFilter: 'all', // 'all' | 'none' | bike id as string
  ride: null, // detail of the selected ride
  points: null,
  editing: false,
  compareId: null,
  map: null,
  chart: null,
  config: null,
  details: initialDetails(), // full stats (true) or the essentials only
  eleInfo: false, // elevation-source note shown
};

const NARROW = matchMedia('(max-width: 760px)');

// Phones start with the essentials so the map gets the screen; the choice is remembered per browser.
function initialDetails() {
  try {
    const saved = localStorage.getItem('stezhka.details');
    if (saved === '1' || saved === '0') return saved === '1';
  } catch { /* storage blocked: use the default */ }
  return !matchMedia('(max-width: 760px)').matches;
}

function setDetails(open) {
  state.details = open;
  try { localStorage.setItem('stezhka.details', open ? '1' : '0'); } catch { /* not essential */ }
  renderStats();
}

// --- rides drawer (phones) ----------------------------------------------------------------------

function setDrawer(open) {
  document.body.classList.toggle('drawer-open', open && NARROW.matches);
  $('drawer-backdrop').hidden = !(open && NARROW.matches);
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
  $('settings').close();
  try { await api('logout', { method: 'POST' }); } catch { /* session is gone either way */ }
  location.hash = '';
  showLogin(true);
}

// --- ride list ----------------------------------------------------------------------------------

function visibleRides() {
  const f = state.bikeFilter;
  if (f === 'all') return state.rides;
  if (f === 'none') return state.rides.filter((r) => r.bike_id == null);
  return state.rides.filter((r) => String(r.bike_id) === f);
}

function renderBikeFilter() {
  const sel = $('bike-filter');
  sel.replaceChildren(
    el('option', { value: 'all' }, t('rides.filter_all')),
    ...state.bikes.map((b) => el('option', { value: String(b.id) }, b.name)),
    el('option', { value: 'none' }, t('rides.filter_none')),
  );
  if (![...sel.options].some((o) => o.value === state.bikeFilter)) state.bikeFilter = 'all';
  sel.value = state.bikeFilter;
  sel.hidden = state.bikes.length === 0;
}

function renderRideList() {
  const list = $('ride-list');
  list.replaceChildren();
  const rides = visibleRides();
  $('ride-count').textContent = t('rides.count', { count: rides.length });
  $('no-rides').hidden = state.rides.length > 0;
  for (const r of rides) {
    const selected = state.ride?.id === r.id;
    const meta = [fmt.distance(r.distance_m), fmt.duration(r.moving_s), fmt.speed(r.avg_moving_mps)].join(' · ');
    list.append(el('li', {},
      el('a', { href: `#ride=${r.id}`, class: 'ride-item', 'aria-current': selected ? 'true' : 'false' },
        el('span', { class: 'ride-date' }, fmt.rideDate(r.started_at)),
        el('span', { class: 'ride-name' }, rideTitle(r)),
        el('span', { class: 'ride-meta' }, meta),
        r.bike_name ? el('span', { class: 'ride-bike' }, r.bike_name) : null,
        r.creator ? el('span', { class: 'ride-source' }, r.creator) : null, // recording app name, as in the file
      )));
  }
}

async function loadRides() {
  state.rides = (await api('rides')).rides;
  renderRideList();
}

async function loadBikes() {
  state.bikes = (await api('bikes')).bikes;
  renderBikeFilter();
}

// --- ride view ----------------------------------------------------------------------------------

function stat(label, value, extraClass = '', labelExtra = null) {
  return el('div', { class: `stat ${extraClass}` },
    el('span', { class: 'stat-label' }, label, labelExtra),
    el('span', { class: 'stat-value' }, value));
}

function titleRow(r) {
  const more = iconButton('chevron', state.details ? t('stats.less') : t('stats.more'),
    () => setDetails(!state.details), state.details ? 'flip' : '');
  more.setAttribute('aria-expanded', String(state.details));
  return el('div', { class: 'ride-head' },
    el('h2', { class: 'ride-title' }, rideTitle(r),
      el('span', { class: 'muted' }, ' · ', fmt.rideDate(r.started_at)),
      r.bike_name ? el('span', { class: 'ride-bike' }, ' · ', r.bike_name) : null),
    el('div', { class: 'ride-actions' },
      iconButton('edit', t('actions.edit'), () => { state.editing = true; state.details = true; renderStats(); }),
      iconButton('trash', t('actions.delete'), onDelete, 'danger'),
      more),
  );
}

function elevationInfoButton() {
  const b = el('button', {
    type: 'button', class: 'info-btn', 'aria-label': t('stats.elevation_info'), title: t('stats.elevation_info'),
    'aria-expanded': String(state.eleInfo), onclick: () => { state.eleInfo = !state.eleInfo; renderStats(); },
  }, icon('info'));
  return b;
}

function editForm(r) {
  const bikeSel = el('select', { id: 'edit-bike' },
    el('option', { value: '' }, t('edit.no_bike')),
    ...state.bikes.map((b) => el('option', { value: String(b.id) }, b.name)));
  bikeSel.value = r.bike_id == null ? '' : String(r.bike_id);
  return el('form', { class: 'edit-form', onsubmit: onSaveEdit },
    el('label', {}, el('span', {}, t('edit.name')),
      el('input', { id: 'edit-name', maxlength: '200', value: r.name && !TIMESTAMP_NAME.test(r.name) ? r.name : '', placeholder: t('rides.untitled') })),
    el('label', {}, el('span', {}, t('edit.bike')), bikeSel),
    el('label', { class: 'wide' }, el('span', {}, t('edit.notes')),
      el('textarea', { id: 'edit-notes', rows: '2', maxlength: '10000', value: r.notes || '' })),
    el('div', { class: 'edit-actions' },
      el('button', { type: 'submit', class: 'btn primary small' }, t('actions.save')),
      el('button', { type: 'button', class: 'btn small', onclick: () => { state.editing = false; renderStats(); } }, t('actions.cancel'))),
  );
}

async function onSaveEdit(e) {
  e.preventDefault();
  const bike = $('edit-bike').value;
  try {
    const updated = await api(`rides/${state.ride.id}`, {
      method: 'PATCH',
      json: { name: $('edit-name').value.trim() || null, notes: $('edit-notes').value.trim() || null, bike_id: bike ? Number(bike) : null },
    });
    state.ride = { ...state.ride, ...updated };
    state.editing = false;
    await loadRides();
    renderStats();
  } catch (ex) {
    toast(errorText(ex), 'error');
  }
}

// No "are you sure?": the ride goes to the trash, the toast offers Undo, and Settings lists
// recently deleted rides for TRASH_DAYS (server side) before they are erased.
async function onDelete() {
  const id = state.ride.id;
  try {
    await api(`rides/${id}`, { method: 'DELETE' });
  } catch (ex) {
    toast(errorText(ex), 'error');
    return;
  }
  state.ride = null;
  state.compareId = null;
  await loadRides();
  location.hash = visibleRides()[0] ? `ride=${visibleRides()[0].id}` : '';
  toast(t('rides.deleted'), 'info', { action: { label: t('actions.undo'), run: () => restoreRide(id) }, ms: 8000 });
}

async function restoreRide(id) {
  try {
    await api(`trash/${id}/restore`, { method: 'POST' });
  } catch (ex) {
    toast(errorText(ex), 'error');
    return;
  }
  await loadRides();
  location.hash = `ride=${id}`;
}

function compareRow(r) {
  // Other rides, the ones closest in time first (a second phone on the same trip sorts to the top).
  const others = state.rides.filter((o) => o.id !== r.id)
    .sort((a, b) => Math.abs(a.started_at - r.started_at) - Math.abs(b.started_at - r.started_at));
  const sel = el('select', { id: 'compare', onchange: (e) => setCompare(e.target.value ? Number(e.target.value) : null) },
    el('option', { value: '' }, t('compare.none')),
    ...others.map((o) => el('option', { value: String(o.id) },
      `${fmt.rideDate(o.started_at)} · ${fmt.distance(o.distance_m)}${o.creator ? ` · ${o.creator}` : ''}`)));
  sel.value = state.compareId ? String(state.compareId) : '';
  const other = state.rides.find((o) => o.id === state.compareId);
  return el('div', { class: 'compare-row' },
    el('label', {}, el('span', { class: 'compare-swatch', 'aria-hidden': 'true' }), t('compare.label'), ' ', sel),
    other ? el('span', { class: 'muted' }, t('compare.summary', {
      distance: fmt.distance(other.distance_m), time: fmt.duration(other.moving_s), speed: fmt.speed(other.avg_moving_mps),
    })) : null);
}

async function setCompare(id) {
  state.compareId = id;
  renderStats();
  if (!id) return state.map.showCompare(null);
  try {
    state.map.showCompare(columns(await api(`rides/${id}/points`)));
  } catch (ex) {
    toast(errorText(ex), 'error');
  }
}

function renderStats() {
  const box = $('stats');
  box.replaceChildren();
  const r = state.ride;
  if (!r) {
    box.append(el('p', { class: 'muted' }, state.rides.length ? t('rides.select') : t('rides.empty')));
    return;
  }
  const full = state.details || state.editing;
  const stops = r.stops.filter((s) => s.kind === 'stop');
  const stopTime = stops.reduce((a, s) => a + (s.end_t - s.start_t), 0);
  const grid = el('div', { class: 'stat-grid' },
    stat(t('stats.distance'), fmt.distance(r.distance_m), 'primary'),
    stat(t('stats.moving_time'), fmt.duration(r.moving_s), 'primary'),
    stat(t('stats.avg_speed'), fmt.speed(r.avg_moving_mps), 'primary'));
  if (full) {
    grid.append(
      stat(t('stats.max_speed'), fmt.speed(r.max_mps)),
      stat(t('stats.elapsed_time'), fmt.duration(r.elapsed_s)),
      stat(t('stats.climb'), `↑ ${fmt.elevation(r.ascent_m)}  ↓ ${fmt.elevation(r.descent_m)}`, '', elevationInfoButton()),
      stat(t('stats.stops'), t('stats.stop_summary', { count: stops.length, time: fmt.duration(stopTime) })),
      ...weatherStats(r));
  }
  // Element.append() would turn null into the text "null", so drop the empty parts first
  box.append(...[
    state.editing ? editForm(r) : titleRow(r),
    full && !state.editing && r.notes ? el('p', { class: 'ride-notes' }, r.notes) : null,
    grid,
    full && state.eleInfo
      ? el('p', { class: 'note' }, r.ele_source === 'dem' ? t('stats.elevation_source_dem') : t('stats.elevation_source_gps'))
      : null,
    full && !r.weather ? el('p', { class: 'note' }, r.enrich_error ? t('weather.unavailable') : t('weather.pending')) : null,
    full ? compareRow(r) : null,
  ].filter((part) => part != null));
}

function windText(w) {
  const base = t('weather.wind', { speed: fmt.windSpeed(w.wind_speed_mps), dir: t(`weather.dir.${fmt.compass(w.wind_dir_deg)}`) });
  const h = w.headwind_mps;
  if (h == null) return base;
  if (h > 0.5) return `${base} · ${t('weather.headwind', { speed: fmt.windSpeed(h) })}`;
  if (h < -0.5) return `${base} · ${t('weather.tailwind', { speed: fmt.windSpeed(h) })}`;
  return `${base} · ${t('weather.crosswind')}`;
}

function weatherStats(r) {
  const w = r.weather;
  if (!w) return [];
  let road = t(`weather.wetness.${w.wetness}`);
  if (w.precip_ride_mm >= 0.1) road += ` · ${t('weather.rain_during', { amount: fmt.precipitation(w.precip_ride_mm) })}`;
  return [
    stat(t('stats.temperature'), fmt.temperature(w.temperature_c)),
    stat(t('stats.road'), road),
    stat(t('stats.wind'), windText(w), 'wide'),
  ];
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
  state.editing = false;
  if (state.compareId === id) state.compareId = null;
  if (!id) {
    state.ride = null;
    state.compareId = null;
    state.map.showCompare(null);
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
  if (state.compareId) await setCompare(state.compareId);
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
    state.chart.onSelect((i) => { state.map.setPinned(i); state.map.panToPoint(i); });
    // tapping the track pins that point too; the map is already looking at it, so no pan
    state.map.onSelect((i) => { state.map.setPinned(i); state.chart.setPinned(i); });
    renderLegend();
    window.addEventListener('hashchange', () => { setDrawer(false); selectRide(rideIdFromHash()); });
    $('drawer-open').addEventListener('click', () => setDrawer(true));
    $('drawer-backdrop').addEventListener('click', () => setDrawer(false));
    $('ride-list').addEventListener('click', () => setDrawer(false)); // also when the open ride is tapped again
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') setDrawer(false); });
    NARROW.addEventListener('change', () => setDrawer(false));
    $('bike-filter').addEventListener('change', (e) => { state.bikeFilter = e.target.value; renderRideList(); });
    initSettings({
      bikesChanged: (bikes) => { state.bikes = bikes; renderBikeFilter(); renderRideList(); renderStats(); },
      rideRestored: (id) => restoreRide(id),
    });
  }
  await Promise.all([loadRides(), loadBikes()]);
  const id = rideIdFromHash() ?? state.rides[0]?.id ?? null;
  if (id && rideIdFromHash() == null) history.replaceState(null, '', `#ride=${id}`);
  await selectRide(id);
}

async function boot() {
  for (const n of document.querySelectorAll('[data-icon]')) n.replaceWith(icon(n.dataset.icon));
  await initI18n();
  buildLanguagePicker();
  onLocaleChange(() => {
    $('lang').value = locale();
    if (!appStarted) return;
    renderBikeFilter();
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
  for (const a of document.querySelectorAll('.source-url')) {
    a.href = session.source_url;
    a.title = `${session.source_url} · Stezhka ${session.version} · AGPL-3.0`;
  }
  if (session.authenticated) await startApp();
  else showLogin(session.password_set);
}

boot();
