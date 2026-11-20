// Small DOM helpers shared by the app modules. User data is always inserted as text, never HTML.
import { ApiError } from './api.js';
import { t, has } from './i18n.js';

export const $ = (id) => document.getElementById(id);

export function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null) continue;
    if (k === 'class') n.className = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (k === 'value') n.value = v;
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children) if (c != null && c !== false) n.append(c);
  return n;
}

export function errorText(err) {
  const code = err instanceof ApiError ? err.code : 'unknown';
  return has(`errors.${code}`) ? t(`errors.${code}`) : t('errors.unknown');
}

let toastTimer;
/** Short message at the bottom; with `action` ({label, run}) it carries a button, e.g. Undo. */
export function toast(message, kind = 'info', { action = null, ms = 5000 } = {}) {
  const box = $('toast');
  const hide = () => { box.hidden = true; clearTimeout(toastTimer); };
  box.replaceChildren(el('span', {}, message));
  if (action) {
    box.append(el('button', { type: 'button', class: 'toast-action', onclick: () => { hide(); action.run(); } }, action.label));
  }
  box.dataset.kind = kind;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(hide, ms);
}

// Recorders often name a track by its start time (OpenTracks: 2026-11-18T20:03+02); the date is
// already shown, so such names are treated as untitled.
export const TIMESTAMP_NAME = /^\d{4}-\d{2}-\d{2}[T ]\d{1,2}[:_]\d{2}/;
export function rideTitle(r) {
  return r.name && !TIMESTAMP_NAME.test(r.name) ? r.name : t('rides.untitled');
}

// Line icons (24x24, stroke = currentColor). Constant markup, never user data.
const ICONS = {
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  upload: '<path d="M12 15V4M7 9l5-5 5 5M5 19h14"/>',
  settings: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16v4z"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  chevron: '<path d="M6 9l6 6 6-6"/>',
  download: '<path d="M12 4v11M7 10l5 5 5-5M5 19h14"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.01"/>',
};

export function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', `icon icon-${name}`);
  svg.innerHTML = ICONS[name];
  return svg;
}

/** Icon-only link (e.g. a file download). */
export function iconLink(name, label, href, extra = {}) {
  return el('a', { class: 'btn icon-btn', 'aria-label': label, title: label, href, ...extra }, icon(name));
}

/** Icon-only button; the label is its accessible name and tooltip. */
export function iconButton(name, label, onclick, extraClass = '') {
  return el('button', { type: 'button', class: `btn icon-btn ${extraClass}`, 'aria-label': label, title: label, onclick }, icon(name));
}
