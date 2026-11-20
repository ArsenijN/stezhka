// App shell: login, ride list, ride view. All user data is inserted with textContent, never HTML
// (ride names come from GPX files).
import { api, ApiError, columns } from './api.js';
import { initI18n, setLocale, locale, locales, onLocaleChange, t } from './i18n.js';
import * as fmt from './format.js';
import { RideMap, SPEED_STOPS, USUAL_STOPS } from './map.js';
import { ProfileChart } from './chart.js';
import { initSettings } from './settings.js';
import { NEGATIVE_TAGS, POSITIVE_TAGS, markKind, tagsFor } from './feelings.js';
import { $, el, errorText, icon, iconButton, iconLink, rideTitle, TIMESTAMP_NAME, toast } from './ui.js';

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
  feelingOpen: false, // reason chips shown in the compact view right after picking a feeling
  markMode: false, // waiting for a drag/tap on the profile
  draft: null, // mark being created or edited: {id|null, start_t, end_t, tags, note}
  roads: null, // ridden segments (/api/roads) while "My roads" is on
  roadMarks: null, // marks of every ride placed on those roads (id -> mark)
  usual: null, // the selected ride against the usual speed per segment (/api/rides/{id}/usual)
  usualOn: initialUsual(), // map colours the ride by "vs usual" instead of speed
};

// --- "vs usual": this ride against the typical speed of each road segment ---------------------------

function initialUsual() {
  try { return localStorage.getItem('stezhka.usual') === '1'; } catch { return false; }
}

function hasUsual() {
  return Boolean(state.usual?.passes.length);
}

/** Show the toggle when there is something to compare, and draw the comparison when it is on. */
function applyUsual() {
  const btn = $('usual-toggle');
  btn.hidden = !hasUsual();
  btn.setAttribute('aria-pressed', String(state.usualOn));
  state.map.showUsual(state.usualOn && hasUsual() ? state.usual.passes : null);
  renderLegend();
}

function setUsual(on) {
  state.usualOn = on;
  try { localStorage.setItem('stezhka.usual', on ? '1' : '0'); } catch { /* not essential */ }
  applyUsual();
}

function gapText(s) {
  const a = Math.abs(s);
  return a < 60 ? t('usual.seconds', { count: Math.round(a) }) : fmt.duration(a);
}

/** "42 s faster than usual on 3.1 km", or null when nothing was compared. */
function usualText() {
  const u = state.usual;
  if (!hasUsual()) return null;
  const dist = fmt.distance(u.compared_m);
  if (Math.abs(u.delta_s) < 1) return t('usual.same', { dist });
  return u.delta_s < 0 ? t('usual.faster', { time: gapText(u.delta_s), dist }) : t('usual.slower', { time: gapText(u.delta_s), dist });
}

function usualPopup(p, lngLat) {
  const delta = Number(p.delta);
  state.map.popup(lngLat, el('div', { class: 'road-popup' },
    el('strong', {}, fmt.speed(Number(p.speed))),
    el('div', {}, t('usual.usually', { speed: fmt.speed(Number(p.usual)), count: Number(p.samples) })),
    Math.abs(delta) >= 0.5
      ? el('div', { class: delta < 0 ? 'usual-gain' : 'usual-loss' }, delta < 0 ? t('usual.gained', { time: gapText(delta) }) : t('usual.lost', { time: gapText(delta) }))
      : null));
}

// --- "My roads": ridden segments with typical speed -----------------------------------------------

function roadLabel(s) {
  const name = locale() === 'uk' ? s.name : (s.name_en || s.name);
  if (name) return name;
  const kind = s.subclass && ROAD_KINDS.includes(s.subclass) ? s.subclass : s.class;
  return ROAD_KINDS.includes(kind) ? t(`road_class.${kind}`) : t('road_class.other');
}

const ROAD_KINDS = ['motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'minor', 'service', 'track', 'path',
  'footway', 'cycleway', 'pedestrian'];

async function setRoads(on) {
  const btn = $('roads-toggle');
  btn.setAttribute('aria-pressed', String(on));
  try { localStorage.setItem('stezhka.roads', on ? '1' : '0'); } catch { /* not essential */ }
  if (!on) {
    state.roads = null;
    state.roadMarks = null;
    state.map.showRoads(null);
    state.map.showRoadMarks(null);
    return;
  }
  await loadRoads(false);
}

/** (Re)load "My roads"; `quiet` after an edit elsewhere: no toasts, no zooming. */
async function loadRoads(quiet) {
  let res;
  try {
    res = await api('roads');
  } catch (ex) {
    if (!quiet) toast(errorText(ex), 'error');
    return;
  }
  if ($('roads-toggle').getAttribute('aria-pressed') !== 'true') return; // switched off meanwhile
  if (!quiet && !res.available) toast(t('roads.no_basemap'));
  else if (!quiet && !res.segments.length) toast(res.pending ? t('roads.pending') : t('roads.none'));
  state.roads = new Map(res.segments.map((s) => [s.id, s]));
  state.roadMarks = new Map(res.marks.map((m) => [m.id, m]));
  state.map.showRoads(res.segments);
  state.map.showRoadMarks(res.marks);
  if (!quiet && !state.ride) state.map.fitRoads(res.segments);
}

function markSummary(m) {
  return m.tags.length ? m.tags.map((tg) => t(`tag.${tg}`)).join(', ') : t('marks.no_tags');
}

/** Marks under a tap on "My roads": what, the note, which ride (tap to open it). */
function roadMarksPopup(ids, lngLat) {
  const list = ids.map((id) => state.roadMarks?.get(id)).filter(Boolean)
    .sort((a, b) => b.ride_started_at - a.ride_started_at);
  if (!list.length) return;
  state.map.popup(lngLat, el('div', { class: 'road-popup' }, ...list.map((m) => el('div', { class: `road-mark ${markKind(m.tags)}` },
    el('strong', {}, markSummary(m)),
    m.note ? el('div', {}, m.note) : null,
    el('a', { href: `#ride=${m.ride_id}`, class: 'muted' }, fmt.rideDate(m.ride_started_at))))));
}

function roadPopup(id, lngLat) {
  const s = state.roads?.get(id);
  if (!s) return;
  const line = (text) => el('div', {}, text);
  state.map.popup(lngLat, el('div', { class: 'road-popup' },
    el('strong', {}, roadLabel(s)),
    line(t('roads.rides', { count: s.rides })),
    s.speed_mps != null ? line(t('roads.typical', { speed: fmt.speed(s.speed_mps) })) : line(t('roads.no_speed')),
    s.best_mps != null && s.passes > 1 ? line(t('roads.best', { speed: fmt.speed(s.best_mps) })) : null,
    line(t('roads.last', { when: fmt.date(s.last_t) })),
    roadMarkCounts(id),
    el('div', { class: 'muted' }, fmt.distance(s.length_m), s.surface ? ` · ${s.surface === 'unpaved' ? t('roads.unpaved') : t('roads.paved')}` : '')));
}

/** "2 marks: puddles ×2, traffic" for a segment, or nothing. */
function roadMarkCounts(segmentId) {
  const on = [...(state.roadMarks?.values() || [])].filter((m) => m.segments.includes(segmentId));
  if (!on.length) return null;
  const counts = new Map();
  for (const m of on) for (const tg of m.tags.length ? m.tags : ['']) counts.set(tg, (counts.get(tg) || 0) + 1);
  const parts = [...counts].map(([tg, n]) => `${tg ? t(`tag.${tg}`) : t('marks.no_tags')}${n > 1 ? ` ×${n}` : ''}`);
  const kind = on.some((m) => markKind(m.tags) === 'bad') ? 'bad' : on.some((m) => markKind(m.tags) === 'good') ? 'good' : 'note';
  return el('div', { class: `road-mark ${kind}` }, t('roads.marks', { count: on.length }), ': ', parts.join(', '));
}

const FEELING_EMOJI = { 1: '👍', 0: '😐', '-1': '👎' };

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

// --- totals -------------------------------------------------------------------------------------

const DAY = 86400;
const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());

function sumRides(rides) {
  return {
    count: rides.length,
    distance: rides.reduce((a, r) => a + r.distance_m, 0),
    moving: rides.reduce((a, r) => a + r.moving_s, 0),
  };
}

function totalCell(label, s) {
  return el('div', { class: 'total' },
    el('span', { class: 'stat-label' }, label),
    el('span', { class: 'total-km' }, fmt.distance(s.distance)),
    el('span', { class: 'muted' }, fmt.duration(s.moving), ' · ', t('totals.rides', { count: s.count })));
}

// Linked recordings of one trip are one ride here: the list holds main rides only.
function renderTotals() {
  const rides = visibleRides();
  const box = $('totals');
  box.hidden = rides.length === 0;
  if (!rides.length) return;
  const today = startOfDay(new Date());
  const week = new Date(today); week.setDate(today.getDate() - ((today.getDay() + 6) % 7)); // Monday
  const month = new Date(today.getFullYear(), today.getMonth(), 1);
  const since = (d) => rides.filter((r) => r.started_at >= d.getTime() / 1000);

  const days = 14;
  const first = new Date(today); first.setDate(today.getDate() - (days - 1));
  const perDay = Array.from({ length: days }, () => 0);
  for (const r of since(first)) {
    const i = Math.round((startOfDay(new Date(r.started_at * 1000)) - first) / (DAY * 1000));
    if (i >= 0 && i < days) perDay[i] += r.distance_m;
  }
  const max = Math.max(...perDay, 1);
  const bars = el('div', { class: 'day-bars', role: 'img', 'aria-label': t('totals.last_days', { count: days }) },
    ...perDay.map((m, i) => {
      const d = new Date(first); d.setDate(first.getDate() + i);
      const bar = el('span', { class: m ? 'bar' : 'bar empty', title: `${fmt.date(d.getTime() / 1000)}: ${fmt.distance(m)}` });
      bar.style.height = `${m ? Math.max(8, (m / max) * 100) : 4}%`;
      if (d.getDay() === 1) bar.classList.add('monday');
      return bar;
    }));
  box.replaceChildren(
    el('div', { class: 'totals-row' },
      totalCell(t('totals.week'), sumRides(since(week))),
      totalCell(t('totals.month'), sumRides(since(month))),
      totalCell(t('totals.all'), sumRides(rides))),
    el('div', { class: 'day-bars-label muted' }, t('totals.last_days', { count: days })),
    bars);
}

function renderRideList() {
  renderTotals();
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
        el('span', { class: 'ride-name' }, rideTitle(r), r.feeling != null ? ` ${FEELING_EMOJI[r.feeling]}` : ''),
        el('span', { class: 'ride-meta' }, meta),
        r.bike_name ? el('span', { class: 'ride-bike' }, r.bike_name) : null,
        // recording app(s), as named in the files; a trip recorded by two phones lists both
        r.creator ? el('span', { class: 'ride-source' }, r.other_creators ? `${r.creator} + ${r.other_creators}` : r.creator) : null,
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
      iconLink('download', t('actions.download_gpx'), `api/rides/${r.id}/gpx`, { download: '' }),
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
  const option = (o) => el('option', { value: String(o.id) },
    `${fmt.rideDate(o.started_at)} · ${fmt.distance(o.distance_m)}${o.creator ? ` · ${o.creator}` : ''}`);
  const sel = el('select', { id: 'compare', onchange: (e) => setCompare(e.target.value ? Number(e.target.value) : null) },
    el('option', { value: '' }, t('compare.none')),
    r.recordings.length ? el('optgroup', { label: t('compare.recordings') }, ...r.recordings.map(option)) : null,
    el('optgroup', { label: t('compare.rides') }, ...others.map(option)));
  sel.value = state.compareId ? String(state.compareId) : '';
  const other = findRide(state.compareId);
  return el('div', { class: 'compare-row' },
    el('label', {}, el('span', { class: 'compare-swatch', 'aria-hidden': 'true' }), t('compare.label'), ' ', sel),
    other ? el('span', { class: 'muted' }, t('compare.summary', {
      distance: fmt.distance(other.distance_m), time: fmt.duration(other.moving_s), speed: fmt.speed(other.avg_moving_mps),
    })) : null);
}

// --- how it felt: feeling, reasons, marks ---------------------------------------------------------

function chip(tag, on, onclick) {
  return el('button', { type: 'button', class: `chip${on ? ' on' : ''}`, 'aria-pressed': String(on), onclick }, t(`tag.${tag}`));
}

async function patchRide(fields) {
  try {
    const updated = await api(`rides/${state.ride.id}`, { method: 'PATCH', json: fields });
    state.ride = { ...state.ride, ...updated };
    const listed = state.rides.find((o) => o.id === state.ride.id);
    if (listed) Object.assign(listed, { feeling: updated.feeling, feeling_tags: updated.feeling_tags });
  } catch (ex) {
    toast(errorText(ex), 'error');
  }
}

// Tapping the chosen feeling again clears it: "not rated" is different from "it was OK".
async function setFeeling(value) {
  const tags = value == null ? [] : state.ride.feeling_tags.filter((tg) => tagsFor(value).includes(tg));
  await patchRide(value == null ? { feeling: null } : { feeling: value, feeling_tags: tags });
  state.feelingOpen = value != null;
  renderRideList();
  renderStats();
}

async function toggleFeelingTag(tag) {
  const tags = state.ride.feeling_tags.includes(tag)
    ? state.ride.feeling_tags.filter((x) => x !== tag) : [...state.ride.feeling_tags, tag];
  await patchRide({ feeling_tags: tags });
  renderStats();
}

function feelingRow(r, full) {
  const btn = (value, label) => el('button', {
    type: 'button', class: `btn small feel${r.feeling === value ? ' active' : ''}`, 'aria-pressed': String(r.feeling === value),
    title: label, 'aria-label': label, onclick: () => setFeeling(r.feeling === value ? null : value),
  }, FEELING_EMOJI[value]);
  const markBtn = state.markMode
    ? el('button', { type: 'button', class: 'btn small', onclick: cancelMarking }, t('actions.cancel'))
    : el('button', { type: 'button', class: 'btn small', onclick: startMarking, disabled: state.draft != null }, `+ ${t('marks.add')}`);
  return [
    el('div', { class: 'feeling-row' },
      el('span', { class: 'muted' }, t('feeling.question')),
      btn(1, t('feeling.like')), btn(0, t('feeling.neutral')), btn(-1, t('feeling.dislike')),
      el('span', { class: 'spacer' }), markBtn),
    r.feeling != null && (full || state.feelingOpen)
      ? el('div', { class: 'chips feeling-chips' }, ...tagsFor(r.feeling).map((tg) => chip(tg, r.feeling_tags.includes(tg), () => toggleFeelingTag(tg))))
      : null,
    state.markMode ? el('p', { class: 'note mark-hint' }, t('marks.hint')) : null,
  ];
}

function idxAtTime(time) {
  const ts = state.points.t;
  let lo = 0, hi = ts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (ts[mid] < time) lo = mid + 1; else hi = mid;
  }
  return lo;
}

function markRange(m) {
  return { i0: idxAtTime(m.start_t), i1: idxAtTime(m.end_t) };
}

function markWhere(m) {
  const { i0, i1 } = markRange(m);
  const d = state.points.dist;
  return i1 - i0 < 1 ? t('marks.spot', { at: fmt.distance(d[i0]) }) : t('marks.range', { from: fmt.distance(d[i0]), to: fmt.distance(d[i1]) });
}

/** Push the ride's marks (and the one being edited) to the map and the profile. */
function updateMarks() {
  if (!state.points || !state.ride) return;
  const saved = state.ride.marks.filter((m) => m.id !== state.draft?.id)
    .map((m) => ({ ...markRange(m), kind: markKind(m.tags), tags: m.tags, note: m.note }));
  const draft = state.draft ? { ...markRange(state.draft), kind: 'draft' } : null;
  state.chart.setMarks(saved);
  if (!state.markMode) state.chart.setSelection(draft ? { a: draft.i0, b: draft.i1 } : null);
  state.map.showMarks(draft ? [...saved, draft] : saved);
}

function startMarking() {
  state.markMode = true;
  state.draft = null;
  renderStats();
  state.chart.startSelect((i0, i1) => {
    state.markMode = false;
    state.draft = { id: null, start_t: state.points.t[i0], end_t: state.points.t[i1], tags: [], note: '' };
    renderStats();
    updateMarks();
  });
}

function cancelMarking() {
  state.markMode = false;
  state.draft = null;
  state.chart.setSelection(null);
  renderStats();
  updateMarks();
}

async function refreshRide() {
  try {
    state.ride = await api(`rides/${state.ride.id}`);
  } catch (ex) {
    toast(errorText(ex), 'error');
  }
  state.draft = null;
  renderStats();
  updateMarks();
}

async function saveMark(e) {
  e.preventDefault();
  const d = state.draft;
  const body = { tags: d.tags, note: d.note.trim() || null };
  try {
    if (d.id) await api(`marks/${d.id}`, { method: 'PATCH', json: body });
    else await api(`rides/${state.ride.id}/marks`, { method: 'POST', json: { start_t: d.start_t, end_t: d.end_t, ...body } });
  } catch (ex) {
    toast(errorText(ex), 'error');
    return;
  }
  await refreshRide();
  if (state.roads) loadRoads(true);
}

async function deleteMark(m) {
  try {
    await api(`marks/${m.id}`, { method: 'DELETE' });
  } catch (ex) {
    toast(errorText(ex), 'error');
    return;
  }
  const rideId = state.ride.id;
  await refreshRide();
  if (state.roads) loadRoads(true);
  toast(t('marks.deleted'), 'info', { ms: 8000, action: { label: t('actions.undo'), run: async () => {
    await api(`rides/${rideId}/marks`, { method: 'POST', json: { start_t: m.start_t, end_t: m.end_t, tags: m.tags, note: m.note } });
    if (state.ride?.id === rideId) await refreshRide();
    if (state.roads) loadRoads(true);
  } } });
}

function focusMark(m) {
  const { i0, i1 } = markRange(m);
  const mid = Math.round((i0 + i1) / 2);
  state.map.setPinned(mid);
  state.chart.setPinned(mid);
  state.map.panToPoint(mid);
}

function markEditor() {
  const d = state.draft;
  const toggle = (tg) => () => {
    d.tags = d.tags.includes(tg) ? d.tags.filter((x) => x !== tg) : [...d.tags, tg];
    renderStats();
  };
  return el('form', { class: 'mark-editor', onsubmit: saveMark },
    el('strong', {}, markWhere(d)),
    el('div', { class: 'chips' }, ...NEGATIVE_TAGS.map((tg) => chip(tg, d.tags.includes(tg), toggle(tg)))),
    el('div', { class: 'chips' }, ...POSITIVE_TAGS.map((tg) => chip(tg, d.tags.includes(tg), toggle(tg)))),
    el('textarea', { rows: '2', maxlength: '2000', placeholder: t('marks.note'), 'aria-label': t('marks.note'), value: d.note,
      oninput: (e) => { d.note = e.target.value; } }),
    el('div', { class: 'edit-actions' },
      el('button', { type: 'submit', class: 'btn primary small' }, t('actions.save')),
      el('button', { type: 'button', class: 'btn small', onclick: cancelMarking }, t('actions.cancel'))));
}

function marksList(r) {
  if (!r.marks.length) return null;
  return el('ul', { class: 'mark-list' }, ...r.marks.map((m) => el('li', { class: `mark-item ${markKind(m.tags)}` },
    el('button', { type: 'button', class: 'mark-where', onclick: () => focusMark(m) }, markWhere(m)),
    el('span', { class: 'mark-text' },
      m.tags.length ? el('span', {}, m.tags.map((tg) => t(`tag.${tg}`)).join(', ')) : null,
      m.note ? el('span', { class: 'muted' }, m.tags.length ? ' · ' : '', m.note) : null),
    el('span', { class: 'row-actions' },
      iconButton('edit', t('actions.edit'), () => {
        state.draft = { id: m.id, start_t: m.start_t, end_t: m.end_t, tags: [...m.tags], note: m.note || '' };
        renderStats();
        updateMarks();
      }),
      iconButton('trash', t('actions.delete'), () => deleteMark(m), 'danger')))));
}

function findRide(id) {
  return state.rides.find((o) => o.id === id) ?? state.ride?.recordings.find((o) => o.id === id);
}

// The trip's other recordings (a second phone): compare in one tap; pick which one is main, or
// split one off when it was not the same trip after all.
function recordingsRow(r) {
  if (!r.recordings.length) return null;
  return el('div', { class: 'recordings' }, ...r.recordings.map((o) => {
    const on = state.compareId === o.id;
    const toggle = el('button', { type: 'button', class: `btn small${on ? ' active' : ''}`, 'aria-pressed': String(on),
      onclick: () => setCompare(on ? null : o.id) }, el('span', { class: 'compare-swatch', 'aria-hidden': 'true' }), t('recordings.compare'));
    return el('div', { class: 'recording' },
      el('span', {}, el('span', { class: 'muted' }, t('recordings.also'), ' '), el('strong', {}, o.creator || '?'),
        el('span', { class: 'muted' }, ' · ', fmt.distance(o.distance_m), ' · ', fmt.speed(o.avg_moving_mps))),
      el('span', { class: 'row-actions' },
        toggle,
        state.details ? el('button', { type: 'button', class: 'btn small', title: t('recordings.make_main_help'),
          onclick: () => makeMain(o.id) }, t('recordings.make_main')) : null,
        state.details ? el('button', { type: 'button', class: 'btn small', title: t('recordings.split_help'),
          onclick: () => splitRecording(o.id) }, t('recordings.split')) : null));
  }));
}

async function makeMain(id) {
  try {
    await api(`rides/${id}/main`, { method: 'POST' });
  } catch (ex) {
    toast(errorText(ex), 'error');
    return;
  }
  const old = state.ride.id;
  state.compareId = state.compareId === id ? old : state.compareId;
  await loadRides();
  location.hash = `ride=${id}`;
}

async function splitRecording(id) {
  try {
    await api(`rides/${id}/split`, { method: 'POST' });
  } catch (ex) {
    toast(errorText(ex), 'error');
    return;
  }
  toast(t('recordings.split_done'));
  await loadRides();
  await selectRide(state.ride.id);
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
  const slowdowns = r.stops.filter((s) => s.kind === 'slowdown');
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
      ...(slowdowns.length ? [stat(t('stats.slowdowns'), String(slowdowns.length))] : []),
      ...weatherStats(r));
    const why = reasonSummary([...stops, ...slowdowns]);
    if (why) grid.append(stat(t('stats.why_stopped'), why, 'wide'));
    const vs = usualText();
    if (vs) grid.append(stat(t('usual.title'), vs, 'wide'));
  }
  // Element.append() would turn null into the text "null", so drop the empty parts first
  box.append(...[
    state.editing ? editForm(r) : titleRow(r),
    full && !state.editing && r.notes ? el('p', { class: 'ride-notes' }, r.notes) : null,
    grid,
    ...(state.editing ? [] : feelingRow(r, full)),
    state.draft ? markEditor() : null,
    full && !state.draft ? marksList(r) : null,
    recordingsRow(r),
    full && state.eleInfo
      ? el('p', { class: 'note' }, r.ele_source === 'dem' ? t('stats.elevation_source_dem') : t('stats.elevation_source_gps'))
      : null,
    full && !r.weather ? el('p', { class: 'note' }, r.enrich_error ? t('weather.unavailable') : t('weather.pending')) : null,
    full ? compareRow(r) : null,
  ].filter((part) => part != null));
}

const STOP_REASONS = ['traffic_signals', 'level_crossing', 'crossing', 'stop_sign', 'barrier', 'junction', 'bus_stop'];

/** "Traffic lights 3 · Junction 2 · Unknown 1" over stops and slowdowns, most common first. */
function reasonSummary(list) {
  if (!list.length) return null;
  const counts = new Map();
  for (const s of list) {
    const k = STOP_REASONS.includes(s.reason) ? s.reason : '';
    counts.set(k, (counts.get(k) || 0) + 1);
  }
  return [...counts].sort((a, b) => b[1] - a[1] || (a[0] ? 0 : 1) - (b[0] ? 0 : 1))
    .map(([k, n]) => `${k ? t(`stop_reason.${k}`) : t('stops.reason_unknown')} ${n}`).join(' · ');
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
  if (state.usualOn && hasUsual()) {
    const min = USUAL_STOPS[0][0];
    const max = USUAL_STOPS[USUAL_STOPS.length - 1][0];
    const bar = el('div', { class: 'legend-bar' });
    bar.style.background = `linear-gradient(to right, ${USUAL_STOPS.map(([v, c]) => `${c} ${((v - min) / (max - min)) * 100}%`).join(', ')})`;
    const ticks = el('div', { class: 'legend-ticks legend-ends' }, el('span', {}, t('usual.slower_short')), el('span', {}, t('usual.faster_short')));
    box.replaceChildren(el('div', { class: 'legend-title' }, t('usual.legend')), bar, ticks);
    return;
  }
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
  state.feelingOpen = false;
  state.markMode = false;
  state.draft = null;
  if (state.compareId === id) state.compareId = null;
  state.map.hideRoadMarksOf(id || null);
  state.usual = null;
  applyUsual();
  if (!id) {
    // nothing selected (the logo): an empty map, or all of "My roads" when that layer is on
    state.ride = null;
    state.points = null;
    state.compareId = null;
    state.map.clearRide();
    state.chart.clear();
    if (state.roads) state.map.fitRoads([...state.roads.values()]);
    renderRideList();
    renderStats();
    return;
  }
  try {
    const [ride, pts, usual] = await Promise.all([
      api(`rides/${id}`), api(`rides/${id}/points`),
      api(`rides/${id}/usual`).catch(() => null), // optional: the ride still shows without it
    ]);
    state.ride = ride;
    state.points = columns(pts);
    state.usual = usual;
  } catch (ex) {
    toast(errorText(ex), 'error');
    if (ex instanceof ApiError && ex.status === 401) return showLogin(true);
    return;
  }
  renderRideList();
  renderStats();
  state.chart.setData(state.points);
  await state.map.showRide(state.points, state.ride.stops);
  updateMarks();
  applyUsual();
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
    state.map.onRoadClick(roadPopup);
    state.map.onRoadMarkClick(roadMarksPopup);
    state.map.onUsualClick(usualPopup);
    $('usual-toggle').addEventListener('click', () => setUsual(!state.usualOn));
    $('roads-toggle').addEventListener('click', () => setRoads($('roads-toggle').getAttribute('aria-pressed') !== 'true'));
    try { if (localStorage.getItem('stezhka.roads') === '1') setRoads(true); } catch { /* storage blocked */ }
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
      // the main recording of some trips changed: their ids in the list did too
      recordingsChanged: async () => {
        await loadRides();
        if (state.ride && !state.rides.some((r) => r.id === state.ride.id)) {
          const moved = state.rides.find((r) => r.started_at === state.ride.started_at)
            ?? state.rides.find((r) => Math.abs(r.started_at - state.ride.started_at) < 600);
          location.hash = moved ? `ride=${moved.id}` : '';
        }
      },
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
