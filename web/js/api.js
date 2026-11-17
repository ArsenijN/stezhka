// Thin fetch wrapper. URLs are relative so the UI works both on its own subdomain and under a
// path prefix behind the reverse proxy. Errors carry the server's error code, never text.

export class ApiError extends Error {
  constructor(code, status) {
    super(code);
    this.code = code;
    this.status = status;
  }
}

export async function api(path, { method = 'GET', json, body, headers = {} } = {}) {
  const init = { method, headers: { ...headers }, credentials: 'same-origin' };
  if (json !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(json);
  } else if (body !== undefined) {
    init.body = body;
  }
  let r;
  try {
    r = await fetch(`api/${path}`, init);
  } catch {
    throw new ApiError('network', 0);
  }
  let data = null;
  try { data = await r.json(); } catch { /* empty or non-JSON body */ }
  if (!r.ok) throw new ApiError(data?.error ?? 'unknown', r.status);
  return data;
}

/** Columnar view of /points: { t: [...], lat: [...], ... } */
export function columns(points) {
  const out = {};
  points.fields.forEach((f, i) => { out[f] = points.rows.map((r) => r[i]); });
  out.length = points.rows.length;
  return out;
}
