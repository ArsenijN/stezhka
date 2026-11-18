// Settings dialog: bikes and password.
import { api } from './api.js';
import { t } from './i18n.js';
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
    $('settings').showModal();
  });
  $('settings-close').addEventListener('click', () => $('settings').close());
  $('bike-form').addEventListener('submit', addBike);
  $('password-form').addEventListener('submit', changePassword);
}
