// Key-based i18n. Locale files are i18next JSON v4 (nested keys, plural suffixes _one/_few/_many/_other,
// {{var}} interpolation), so they can be managed in Weblate or any tool that speaks that format.
//
// Rules (enforced by server/tests/test_i18n.py):
//   * UI text never lives in code: always t('some.key'); English is just another locale file.
//   * Keys are string literals so they can be checked statically. The only dynamic lookups
//     allowed are the prefixes listed in DYNAMIC_PREFIXES (e.g. server error codes).
//   * Missing key in the current locale -> English; missing in English -> the key itself + a warning.

export const FALLBACK_LOCALE = 'en';
export const DYNAMIC_PREFIXES = ['errors.', 'stop_kind.', 'weather.dir.', 'weather.wetness.', 'bike_kind.', 'tag.', 'road_class.'];
const STORAGE_KEY = 'stezhka.locale';

let available = [{ code: FALLBACK_LOCALE, name: 'English' }];
let current = FALLBACK_LOCALE;
let messages = {};
let fallback = {};
let rules = new Intl.PluralRules(FALLBACK_LOCALE);
const fallbackRules = new Intl.PluralRules(FALLBACK_LOCALE);
const listeners = new Set();
const warned = new Set();

async function loadJson(path) {
  const r = await fetch(path, { cache: 'no-cache' });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

function readStored() {
  try { return localStorage.getItem(STORAGE_KEY); } catch { return null; }
}

function writeStored(code) {
  try { localStorage.setItem(STORAGE_KEY, code); } catch { /* private mode etc. */ }
}

function pickFromBrowser() {
  const codes = available.map((l) => l.code);
  for (const tag of navigator.languages || [navigator.language]) {
    const base = String(tag).toLowerCase().split('-')[0];
    if (codes.includes(base)) return base;
  }
  return FALLBACK_LOCALE;
}

export async function initI18n() {
  try {
    available = (await loadJson('locales/index.json')).locales;
  } catch (e) {
    console.error('i18n: cannot load locale index', e);
  }
  fallback = await loadJson(`locales/${FALLBACK_LOCALE}.json`);
  await setLocale(readStored() || pickFromBrowser(), { persist: false });
}

export async function setLocale(code, { persist = true } = {}) {
  if (!available.some((l) => l.code === code)) code = FALLBACK_LOCALE;
  let dict = fallback;
  if (code !== FALLBACK_LOCALE) {
    try {
      dict = await loadJson(`locales/${code}.json`);
    } catch (e) {
      console.error(`i18n: cannot load ${code}`, e);
      code = FALLBACK_LOCALE;
    }
  }
  messages = dict;
  current = code;
  rules = new Intl.PluralRules(code);
  document.documentElement.lang = code;
  if (persist) writeStored(code);
  translateDom();
  for (const fn of listeners) fn(code);
}

export const locale = () => current;
export const locales = () => available.slice();
export function onLocaleChange(fn) { listeners.add(fn); return () => listeners.delete(fn); }

function lookup(dict, key) {
  let node = dict;
  for (const part of key.split('.')) {
    if (node == null || typeof node !== 'object') return undefined;
    node = node[part];
  }
  return typeof node === 'string' ? node : undefined;
}

function resolve(key, vars) {
  if (vars && typeof vars.count === 'number') {
    const msg = lookup(messages, `${key}_${rules.select(vars.count)}`)
      ?? lookup(messages, `${key}_other`)
      ?? lookup(fallback, `${key}_${fallbackRules.select(vars.count)}`)
      ?? lookup(fallback, `${key}_other`);
    if (msg !== undefined) return msg;
  }
  return lookup(messages, key) ?? lookup(fallback, key);
}

/** True if the key (or its plural forms) exists in the fallback locale. */
export function has(key) {
  return lookup(fallback, key) !== undefined || lookup(fallback, `${key}_other`) !== undefined;
}

export function t(key, vars) {
  const msg = resolve(key, vars);
  if (msg === undefined) {
    if (!warned.has(key)) { warned.add(key); console.warn(`i18n: missing key "${key}"`); }
    return key;
  }
  if (!vars) return msg;
  return msg.replace(/\{\{\s*(\w+)\s*\}\}/g, (m, name) => {
    const v = vars[name];
    if (v === undefined || v === null) return m;
    return typeof v === 'number' ? new Intl.NumberFormat(current).format(v) : String(v);
  });
}

/**
 * Translate static markup:
 *   <span data-i18n="rides.title"></span>
 *   <input data-i18n-attr="placeholder:login.password,aria-label:login.password">
 */
export function translateDom(root = document) {
  for (const el of root.querySelectorAll('[data-i18n]')) el.textContent = t(el.dataset.i18n);
  for (const el of root.querySelectorAll('[data-i18n-attr]')) {
    for (const pair of el.dataset.i18nAttr.split(',')) {
      const [attr, key] = pair.split(':').map((s) => s.trim());
      if (attr && key) el.setAttribute(attr, t(key));
    }
  }
}
