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

export function temperature(c) {
  if (c == null) return '–';
  return nf({ style: 'unit', unit: 'celsius', maximumFractionDigits: 0 }).format(c);
}

export function windSpeed(mps) {
  if (mps == null) return '–';
  return nf({ style: 'unit', unit: 'meter-per-second', maximumFractionDigits: 1 }).format(Math.abs(mps));
}

export function precipitation(mm) {
  if (mm == null) return '–';
  return nf({ style: 'unit', unit: 'millimeter', maximumFractionDigits: 1 }).format(mm);
}

/** Meteorological degrees -> one of n, ne, e, se, s, sw, w, nw (locale keys weather.dir.*). */
export function compass(deg) {
  return ['n', 'ne', 'e', 'se', 's', 'sw', 'w', 'nw'][Math.round(((deg % 360) + 360) % 360 / 45) % 8];
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

/** Ride start: "Wed, 18 Nov, 20:03"; the year only when it is not the current one. */
export function rideDate(unixSeconds) {
  const d = new Date(unixSeconds * 1000);
  const opts = { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
  return new Intl.DateTimeFormat(locale(), opts).format(d);
}

export function date(unixSeconds) {
  return new Intl.DateTimeFormat(locale(), { dateStyle: 'medium' }).format(unixSeconds * 1000);
}

export function time(unixSeconds) {
  return new Intl.DateTimeFormat(locale(), { timeStyle: 'short' }).format(unixSeconds * 1000);
}
