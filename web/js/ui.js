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
export function toast(message, kind = 'info') {
  const box = $('toast');
  box.textContent = message;
  box.dataset.kind = kind;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 5000);
}
