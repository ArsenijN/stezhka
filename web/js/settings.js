// Settings dialog: bikes, upload keys, recently deleted rides, language, password.
import { api } from './api.js';
import { t } from './i18n.js';
import * as fmt from './format.js';
import { $, el, errorText, rideTitle, toast } from './ui.js';

export const BIKE_KINDS = ['mtb', 'trekking', 'gravel', 'road', 'city', 'other'];

let onBikesChanged = () => {};
let onRideRestored = () => {};
let onRecordingsChanged = () => {};

function renderBikes(bikes) {
  const list = $('bike-list');
  list.replaceChildren(...bikes.map((b) =>
    el('li', {}, el('strong', {}, b.name), b.kind ? el('span', { class: 'muted' }, ` · ${t(`bike_kind.${b.kind}`)}`) : null)));
  if (!bikes.length) list.append(el('li', { class: 'muted' }, t('settings.no_bikes')));
}

async function addBike(e) {
  e.preventDefault();
  const name = $('bike-name').value.trim();
  if (!name) return;
  try {
    await api('bikes', { method: 'POST', json: { name, kind: $('bike-kind').value || null } });
    $('bike-name').value = '';
    const bikes = (await api('bikes')).bikes;
    renderBikes(bikes);
    onBikesChanged(bikes);
  } catch (ex) {
    toast(errorText(ex), 'error');
  }
}

async function changePassword(e) {
  e.preventDefault();
  const msg = $('pw-msg');
  msg.textContent = '';
  if ($('pw-new').value !== $('pw-repeat').value) {
    msg.textContent = t('settings.password_mismatch');
    return;
  }
  try {
    await api('password', { method: 'POST', json: { current: $('pw-current').value, new: $('pw-new').value } });
    e.target.reset();
    toast(t('settings.password_changed'));
  } catch (ex) {
    msg.textContent = errorText(ex);
  }
}

function renderKeys(tokens) {
  const list = $('key-list');
  list.replaceChildren(...tokens.map((k) => el('li', { class: 'key-item' },
    el('span', {}, el('strong', {}, k.name), el('span', { class: 'muted' }, ' · ',
      k.last_used_at ? t('settings.key_used', { when: fmt.dateTime(k.last_used_at) }) : t('settings.key_unused'))),
    el('button', { type: 'button', class: 'btn small danger', onclick: () => revokeKey(k) }, t('settings.revoke')))));
  if (!tokens.length) list.append(el('li', { class: 'muted' }, t('settings.no_keys')));
}

async function loadKeys() {
  renderKeys((await api('tokens')).tokens);
}

async function addKey(e) {
  e.preventDefault();
  try {
    const res = await api('tokens', { method: 'POST', json: { name: $('key-name').value.trim() } });
    $('key-name').value = '';
    $('key-value').value = res.token;
    $('key-new').hidden = false;
    await loadKeys();
  } catch (ex) {
    toast(errorText(ex), 'error');
  }
}

async function revokeKey(k) {
  if (!confirm(t('settings.revoke_confirm', { name: k.name }))) return;
  try {
    await api(`tokens/${k.id}`, { method: 'DELETE' });
    await loadKeys();
  } catch (ex) {
    toast(errorText(ex), 'error');
  }
}

async function copyKey() {
  const input = $('key-value');
  try {
    await navigator.clipboard.writeText(input.value);
  } catch {
    input.select(); // clipboard API needs a secure context; let the user copy manually
    return;
  }
  toast(t('settings.copied'));
}

// Which app's recording leads a trip recorded by two phones. Shown once there is such a trip.
async function loadRecorders() {
  const { preferred, recorders } = await api('recorders');
  $('recorder-section').hidden = recorders.length < 2 && !preferred;
  const sel = $('recorder');
  const names = preferred && !recorders.includes(preferred) ? [...recorders, preferred] : recorders;
  sel.replaceChildren(el('option', { value: '' }, t('settings.main_recording_auto')),
    ...names.map((n) => el('option', { value: n }, n)));
  sel.value = preferred || '';
}

async function setRecorder() {
  try {
    const res = await api('recorders', { method: 'PUT', json: { preferred: $('recorder').value || null } });
    toast(t('settings.main_recording_changed', { count: res.changed }));
    if (res.changed) onRecordingsChanged();
  } catch (ex) {
    toast(errorText(ex), 'error');
  }
}

function renderTrash({ rides, trash_days: days }) {
  $('trash-help').textContent = t('settings.trash_help', { count: days });
  const list = $('trash-list');
  list.replaceChildren(...rides.map((r) => el('li', { class: 'trash-item' },
    el('span', {}, el('strong', {}, rideTitle(r)), ' · ', fmt.rideDate(r.started_at), ' · ', fmt.distance(r.distance_m),
      el('span', { class: 'muted' }, ' · ', t('settings.erase_on', { when: fmt.date(r.purge_at) }))),
    el('span', { class: 'row-actions' },
      el('button', { type: 'button', class: 'btn small', onclick: () => restore(r) }, t('settings.restore')),
      el('button', { type: 'button', class: 'btn small danger', onclick: () => erase(r) }, t('settings.erase'))))));
  if (!rides.length) list.append(el('li', { class: 'muted' }, t('settings.trash_empty')));
}

async function loadTrash() {
  renderTrash(await api('trash'));
}

async function restore(r) {
  $('settings').close();
  onRideRestored(r.id);
}

async function erase(r) {
  if (!confirm(t('settings.erase_confirm'))) return; // the one step that cannot be undone
  try {
    await api(`trash/${r.id}`, { method: 'DELETE' });
    await loadTrash();
  } catch (ex) {
    toast(errorText(ex), 'error');
  }
}

function fillKindSelect() {
  const sel = $('bike-kind');
  const current = sel.value;
  sel.replaceChildren(el('option', { value: '' }, '—'), ...BIKE_KINDS.map((k) => el('option', { value: k }, t(`bike_kind.${k}`))));
  sel.value = current;
}

export function initSettings({ bikesChanged, rideRestored, recordingsChanged }) {
  onBikesChanged = bikesChanged;
  onRideRestored = rideRestored;
  onRecordingsChanged = recordingsChanged;
  $('recorder').addEventListener('change', setRecorder);
  $('settings-open').addEventListener('click', async () => {
    fillKindSelect();
    renderBikes((await api('bikes')).bikes);
    $('key-new').hidden = true; // a new key is shown once, never again after closing
    $('key-value').value = '';
    await Promise.all([loadKeys(), loadTrash(), loadRecorders()]);
    $('settings').showModal();
  });
  $('settings-close').addEventListener('click', () => $('settings').close());
  $('bike-form').addEventListener('submit', addBike);
  $('password-form').addEventListener('submit', changePassword);
  $('key-form').addEventListener('submit', addKey);
  $('key-copy').addEventListener('click', copyKey);
}
