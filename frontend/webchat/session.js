// Sesión anónima del chat, pedidos a la API y datos guardados en esta pestaña (sessionStorage).

const API = '/webchat';
const KEYS = { session: 'tramites.chat.session', seq: 'tramites.chat.seq', log: 'tramites.chat.log' };
const LOG_LIMIT = 100;
const RENEW_MARGIN = 10; // segundos: si a la sesión le queda menos, se pide otra antes de usarla
const SEND_TIMEOUT = 20_000;
const POLL_TIMEOUT = 40_000; // el servidor responde a los ~25 s; pasado esto la conexión quedó colgada

export const UNAVAILABLE = 'El chat no está disponible en este momento.';
const OFFLINE = 'No pudimos conectarnos. Revisá tu conexión y probá de nuevo.';
const EXPIRED = 'La sesión venció.';
const GENERIC = 'Algo salió mal. Probá de nuevo en unos minutos.';

export class ApiError extends Error {
  constructor(status, message, retryAfter = 0) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

function read(key) {
  try {
    const raw = sessionStorage.getItem(key);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function write(key, value) {
  try {
    if (value === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Sin almacenamiento (modo privado estricto o cuota llena): la charla dura lo que la página.
  }
}

const nowSeconds = () => Math.floor(Date.now() / 1000);
const AUTHORS = new Set(['user', 'bot', 'operator', 'system']);

// Identificador para client_message_id (el servidor acepta [A-Za-z0-9-], hasta 64 caracteres).
export function newMessageId() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  // randomUUID solo existe con HTTPS o en localhost; en una IP de la red local hay que armarlo.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

// Retry-After puede venir en segundos o como fecha HTTP.
function retryAfter(response) {
  const value = response.headers.get('Retry-After');
  if (!value) return 0;
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return Math.max(0, Math.ceil(seconds));
  const date = Date.parse(value);
  return Number.isNaN(date) ? 0 : Math.max(0, Math.ceil((date - Date.now()) / 1000));
}

function errorMessage(status, data) {
  if (status === 401) return EXPIRED;
  if (status === 404) return UNAVAILABLE; // el canal está deshabilitado en el servidor
  const detail = typeof data?.detail === 'string' ? data.detail.trim() : '';
  return detail || GENERIC;
}

async function request(path, { method = 'GET', token, body, signal, timeout = SEND_TIMEOUT } = {}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  const timer = setTimeout(abort, timeout);
  signal?.addEventListener('abort', abort);
  if (signal?.aborted) abort();

  const headers = { Accept: 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';

  try {
    let response;
    try {
      response = await fetch(path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
        cache: 'no-store',
      });
    } catch (error) {
      if (signal?.aborted) throw error; // lo canceló quien hizo el pedido
      throw new ApiError(0, OFFLINE);
    }
    const data = await response.json().catch(() => null);
    if (response.ok) return data;
    throw new ApiError(response.status, errorMessage(response.status, data), retryAfter(response));
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
  }
}

export class ChatSession {
  #token = null;
  #expiresAt = 0;
  #seq = 0;
  #epoch = 0; // cambia con cada clear(): invalida pedidos de sesión que estaban en curso
  #pending = null;

  lapsed = false; // había una sesión guardada, pero ya venció
  onRenew = null; // (replaced: boolean) => void

  constructor() {
    const saved = read(KEYS.session);
    if (typeof saved?.token !== 'string' || !saved.token) return;
    if (Number(saved.expiresAt) > nowSeconds()) {
      this.#token = saved.token;
      this.#expiresAt = Number(saved.expiresAt);
      this.#seq = Math.max(0, Number(read(KEYS.seq)) || 0);
    } else {
      this.lapsed = true;
      write(KEYS.session, null);
      write(KEYS.seq, null);
    }
  }

  get token() {
    return this.#token;
  }

  get lastSeq() {
    return this.#seq;
  }

  set lastSeq(value) {
    this.#seq = value;
    write(KEYS.seq, value);
  }

  #fresh() {
    return Boolean(this.#token) && this.#expiresAt - nowSeconds() > RENEW_MARGIN;
  }

  // Empieza de cero: también descarta un pedido de sesión que esté en curso.
  clear() {
    this.#epoch += 1;
    this.#pending = null;
    this.discard();
  }

  // El servidor ya no reconoce la sesión. A diferencia de clear(), no cancela un pedido de sesión
  // nueva que ya esté en curso (por ejemplo, el de un envío que recibió el mismo 401).
  discard() {
    this.#token = null;
    this.#expiresAt = 0;
    this.#seq = 0;
    write(KEYS.session, null);
    write(KEYS.seq, null);
  }

  // Lo guardado se valida igual: se muestra con textContent, pero puede no tener la forma esperada.
  loadLog() {
    const log = read(KEYS.log);
    if (!Array.isArray(log)) return [];
    return log.filter((entry) => Boolean(entry) && typeof entry.key === 'string'
      && typeof entry.text === 'string' && AUTHORS.has(entry.author));
  }

  saveLog(entries) {
    write(KEYS.log, entries.length ? entries.slice(-LOG_LIMIT) : null);
  }

  ensure() {
    return this.#fresh() ? Promise.resolve(this.#token) : this.renew(this.#token);
  }

  // Pide otra sesión, salvo que un pedido simultáneo ya haya reemplazado a la que falló.
  renew(stale) {
    if (this.#fresh() && this.#token !== stale) return Promise.resolve(this.#token);
    if (!this.#pending) {
      const pending = this.#create(this.#epoch).finally(() => {
        if (this.#pending === pending) this.#pending = null;
      });
      this.#pending = pending;
    }
    return this.#pending;
  }

  async #create(epoch) {
    let data;
    try {
      data = await request(`${API}/sessions`, { method: 'POST' });
    } catch (error) {
      if (error.status === 503) throw new ApiError(503, UNAVAILABLE, error.retryAfter);
      throw error;
    }
    if (epoch !== this.#epoch) throw new ApiError(0, 'La conversación se reinició.');
    if (typeof data?.token !== 'string' || !data.token) throw new ApiError(500, GENERIC);

    const replaced = this.#token !== null;
    this.#token = data.token;
    this.#expiresAt = Number(data.expires_at) || nowSeconds() + 3600;
    this.#seq = 0;
    write(KEYS.session, { token: this.#token, expiresAt: this.#expiresAt });
    write(KEYS.seq, 0);
    this.onRenew?.(replaced);
    return this.#token;
  }

  async send(text, clientMessageId) {
    const epoch = this.#epoch;
    const token = await this.ensure();
    try {
      return await this.#post(token, text, clientMessageId);
    } catch (error) {
      if (error.status !== 401 || epoch !== this.#epoch) throw error;
      // La sesión venció o el servidor no la reconoce: se pide otra y se reintenta una sola vez.
      return this.#post(await this.renew(token), text, clientMessageId);
    }
  }

  #post(token, text, clientMessageId) {
    return request(`${API}/messages`, {
      method: 'POST',
      token,
      body: { text, client_message_id: clientMessageId },
    });
  }

  async poll(token, after, signal) {
    const query = new URLSearchParams({ after: String(after), wait: 'true' });
    const data = await request(`${API}/messages?${query}`, { token, signal, timeout: POLL_TIMEOUT });
    return Array.isArray(data?.messages) ? data.messages : [];
  }
}
