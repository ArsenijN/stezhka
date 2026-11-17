// Locale-aware number/date formatting. Metric only for now; a unit-system setting can switch
// these functions later without touching callers.
import { locale } from './i18n.js';

const cache = new Map();
function nf(opts) {
  const key = locale() + JSON.stringify(opts);
  if (!cache.has(key)) cache.set(key, new Intl.NumberFormat(locale(), opts));
  return cache.get(key);
}

export const kmh = (mps) => mps * 3.6;

export function distance(m) {
  if (m == null) return '–';
  if (m < 1000) return nf({ style: 'unit', unit: 'meter', maximumFractionDigits: 0 }).format(m);
  return nf({ style: 'unit', unit: 'kilometer', maximumFractionDigits: m < 10000 ? 2 : 1 }).format(m / 1000);
}

export function speed(mps) {
  if (mps == null) return '–';
  return nf({ style: 'unit', unit: 'kilometer-per-hour', maximumFractionDigits: 1 }).format(kmh(mps));
}

export function elevation(m) {
  if (m == null) return '–';
  return nf({ style: 'unit', unit: 'meter', maximumFractionDigits: 0 }).format(m);
}

export function duration(s) {
  if (s == null) return '–';
  const total = Math.round(s);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

export function dateTime(unixSeconds) {
  return new Intl.DateTimeFormat(locale(), { dateStyle: 'medium', timeStyle: 'short' }).format(unixSeconds * 1000);
}

export function weekdayDate(unixSeconds) {
  return new Intl.DateTimeFormat(locale(), { weekday: 'short', day: 'numeric', month: 'short' }).format(unixSeconds * 1000);
}

export function time(unixSeconds) {
  return new Intl.DateTimeFormat(locale(), { timeStyle: 'short' }).format(unixSeconds * 1000);
}
