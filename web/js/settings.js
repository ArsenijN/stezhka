// Settings dialog: bikes and password.
import { api } from './api.js';
import { t } from './i18n.js';
import * as fmt from './format.js';
import { $, el, errorText, toast } from './ui.js';

export const BIKE_KINDS = ['mtb', 'trekking', 'gravel', 'road', 'city', 'other'];

let onBikesChanged = () => {};

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

function fillKindSelect() {
  const sel = $('bike-kind');
  const current = sel.value;
  sel.replaceChildren(el('option', { value: '' }, '—'), ...BIKE_KINDS.map((k) => el('option', { value: k }, t(`bike_kind.${k}`))));
  sel.value = current;
}

export function initSettings(bikesChanged) {
  onBikesChanged = bikesChanged;
  $('settings-open').addEventListener('click', async () => {
    fillKindSelect();
    renderBikes((await api('bikes')).bikes);
    $('key-new').hidden = true; // a new key is shown once, never again after closing
    $('key-value').value = '';
    await loadKeys();
    $('settings').showModal();
  });
  $('settings-close').addEventListener('click', () => $('settings').close());
  $('bike-form').addEventListener('submit', addBike);
  $('password-form').addEventListener('submit', changePassword);
  $('key-form').addEventListener('submit', addKey);
  $('key-copy').addEventListener('click', copyKey);
}
