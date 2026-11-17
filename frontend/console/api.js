// Cliente de la API de administración. El token, si lo hay, vive solo en sessionStorage.
// En producción el proxy OIDC agrega la cabecera Authorization y acá no se guarda nada.

const BASE = '/admin/api';
const TOKEN_KEY = 'tramites.console.token';
const TIMEOUT = 15_000;

const MESSAGES = {
  0: 'No se pudo conectar con el servidor.',
  401: 'Tu sesión venció o el token no es válido.',
  403: 'No tenés permiso para esta acción',
  404: 'No se encontró lo que buscabas.',
  409: 'Alguien más modificó la conversación. Actualizamos los datos.',
  422: 'El pedido no tiene el formato esperado.',
  429: 'Hay demasiados pedidos seguidos. Esperá un momento.',
};

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

let memoryToken = null; // si el navegador bloquea sessionStorage, el token dura lo que la página

export function getToken() {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? memoryToken;
  } catch {
    return memoryToken;
  }
}

export function setToken(token) {
  memoryToken = null;
  try {
    sessionStorage.setItem(TOKEN_KEY, token);
  } catch {
    memoryToken = token;
  }
}

export function clearToken() {
  memoryToken = null;
  try {
    sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // nada que borrar
  }
}

function messageFor(status, data) {
  if (status === 401 || status === 403) return MESSAGES[status];
  const detail = typeof data?.detail === 'string' ? data.detail.trim() : '';
  if (detail) return detail.charAt(0).toUpperCase() + detail.slice(1);
  if (MESSAGES[status]) return MESSAGES[status];
  return status >= 500 ? 'El servidor tuvo un problema. Probá de nuevo en unos minutos.' : 'No se pudo completar la acción.';
}

async function request(path, { method = 'GET', body, params = [] } = {}) {
  const url = new URL(BASE + path, window.location.origin);
  for (const [key, value] of params) url.searchParams.append(key, value);

  const headers = { Accept: 'application/json' };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT);
  try {
    let response;
    try {
      response = await fetch(url, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
        cache: 'no-store',
        credentials: 'same-origin', // la cookie del proxy OIDC, si existe
      });
    } catch {
      throw new ApiError(0, MESSAGES[0]);
    }
    const data = response.status === 204 ? null : await response.json().catch(() => null);
    if (!response.ok) throw new ApiError(response.status, messageFor(response.status, data));
    return data;
  } finally {
    clearTimeout(timer);
  }
}

const conversation = (id, suffix = '') => `/conversations/${encodeURIComponent(id)}${suffix}`;

export const api = {
  me: () => request('/me'),
  conversations: (states) => request('/conversations', { params: states.map((state) => ['state', state]) }),
  conversation: (id) => request(conversation(id)),
  claim: (id) => request(conversation(id, '/claim'), { method: 'POST' }),
  reply: (id, text) => request(conversation(id, '/reply'), { method: 'POST', body: { text } }),
  release: (id) => request(conversation(id, '/release'), { method: 'POST' }),
  close: (id) => request(conversation(id, '/close'), { method: 'POST' }),
  eraseContact: (id) => request(conversation(id, '/contact'), { method: 'DELETE' }),
  problems: () => request('/problems'),
  retry: (id) => request(`/problems/${encodeURIComponent(id)}/retry`, { method: 'POST' }),
  channels: () => request('/channels'),
  audit: (before) => request('/audit', {
    params: before ? [['limit', '100'], ['before', String(before)]] : [['limit', '100']],
  }),
};
