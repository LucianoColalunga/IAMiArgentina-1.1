// Utilidades de la consola: armado de nodos, textos, fechas, avisos y refresco periódico.

export const $ = (id) => document.getElementById(id);

// Crea un elemento. Los textos siempre entran con textContent o como nodos de texto.
export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('aria-')) node.setAttribute(key, String(value));
    else node[key] = value;
  }
  node.append(...[].concat(children).filter((child) => child !== null && child !== undefined && child !== false));
  return node;
}

export const CHANNELS = {
  whatsapp: 'WhatsApp',
  telegram: 'Telegram',
  instagram: 'Instagram',
  messenger: 'Messenger',
  x: 'X',
  webchat: 'Chat web',
  email: 'Correo',
  sms: 'SMS',
};

export const STATES = {
  bot: 'Asistente',
  waiting_human: 'Esperando persona',
  human: 'Con operador/a',
  closed: 'Cerrada',
};

export const ROLES = {
  admin: 'Administrador/a',
  supervisor: 'Supervisor/a',
  operator: 'Operador/a',
  auditor: 'Auditor/a',
};

export const AUTHORS = { user: 'Persona', bot: 'Asistente', operator: 'Operador/a', system: 'Sistema' };

export const STATUSES = {
  pending: 'pendiente',
  sending: 'enviando',
  sent: 'enviado',
  delivered: 'entregado',
  read: 'leído',
  failed: 'falló',
  dead: 'sin entregar',
  received: 'recibido',
  processing: 'procesando',
  answered: 'respondido',
  ignored: 'ignorado',
};

export const REASONS = {
  user_request: 'La persona pidió hablar con alguien',
  timeout: 'Venció la espera',
};

export const AUDIT_ACTIONS = {
  conversation_viewed: 'Abrió una conversación',
  conversation_claimed: 'Tomó una conversación',
  conversation_replied: 'Respondió en una conversación',
  conversation_released: 'Devolvió una conversación al asistente',
  conversation_closed: 'Cerró una conversación',
  contact_erased: 'Borró los datos de un contacto',
  contact_erasure_requested: 'La persona pidió borrar sus datos',
  contacts_erased: 'Se borraron datos de contactos',
  contact_opted_out: 'La persona pidió no recibir mensajes',
  contact_opted_in: 'La persona volvió a recibir mensajes',
  contacts_linked: 'Se vincularon contactos de distintos canales',
  message_retried: 'Reintentó un envío',
  housekeeping_done: 'Limpieza automática de datos',
};

export const AUDIT_TARGETS = { conversation: 'Conversación', contact: 'Contacto', message: 'Mensaje' };

export const label = (map, value) => map[value] ?? (value ? String(value) : '—');

// Valores que vienen de la API usados como modificador de clase CSS.
const modifier = (value) => String(value ?? '').toLowerCase().replace(/[^a-z_]/g, '') || 'otro';

export function stateBadge(state) {
  return el('span', { class: `badge badge--${modifier(state)}`, text: label(STATES, state) });
}

export function statusBadge(status) {
  return el('span', { class: `status status--${modifier(status)}`, text: label(STATUSES, status) });
}

// Fechas

const dateTime = new Intl.DateTimeFormat('es-AR', { dateStyle: 'short', timeStyle: 'medium', hourCycle: 'h23' });
const clock = new Intl.DateTimeFormat('es-AR', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const clockSeconds = new Intl.DateTimeFormat('es-AR', { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });

export function toDate(value) {
  if (value === null || value === undefined || value === '') return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDateTime(value) {
  const date = toDate(value);
  return date ? dateTime.format(date) : '—';
}

export function formatClock(value, withSeconds = false) {
  const date = toDate(value);
  if (!date) return '—';
  return (withSeconds ? clockSeconds : clock).format(date);
}

export function minutesSince(value, now = Date.now()) {
  const date = toDate(value);
  return date ? Math.max(0, (now - date.getTime()) / 60_000) : 0;
}

export function relative(value, now = Date.now()) {
  const date = toDate(value);
  if (!date) return '—';
  const seconds = Math.max(0, Math.round((now - date.getTime()) / 1000));
  if (seconds < 45) return 'hace instantes';
  const minutes = Math.max(1, Math.round(seconds / 60));
  if (minutes < 60) return `hace ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `hace ${hours} h`;
  const days = Math.floor(hours / 24);
  return days === 1 ? 'hace 1 día' : `hace ${days} días`;
}

// <time> con texto relativo ("hace 3 min"); refreshTimes() lo actualiza sin rehacer la lista.
export function timeAgo(value, prefix = '') {
  const date = toDate(value);
  if (!date) return el('span', { text: `${prefix}—` });
  return el('time', {
    class: 'js-relative',
    dateTime: date.toISOString(),
    title: dateTime.format(date),
    dataset: { prefix },
    text: prefix + relative(date),
  });
}

export function refreshTimes(root) {
  const now = Date.now();
  for (const node of root.querySelectorAll('time.js-relative')) {
    node.textContent = node.dataset.prefix + relative(node.dateTime, now);
  }
}

// Marca un botón como ocupado sin quitarle el foco (con disabled el navegador lo pierde).
export function setBusy(button, busy) {
  if (busy) button.setAttribute('aria-disabled', 'true');
  else button.removeAttribute('aria-disabled');
}

export const isBusy = (button) => button.getAttribute('aria-disabled') === 'true';

// Avisos no bloqueantes. Un aviso con la misma clave reemplaza al anterior en vez de apilarse.

const MAX_TOASTS = 4;
const toasts = new Map();

export function toast(message, { kind = 'info', key = message } = {}) {
  let entry = toasts.get(key);
  if (!entry) {
    const text = el('p', { class: 'toast__text' });
    const close = el('button', { type: 'button', class: 'toast__close', 'aria-label': 'Cerrar aviso', text: '×' });
    entry = { node: el('div', {}, [text, close]), text, timer: 0 };
    close.addEventListener('click', () => dismissToast(key));
    toasts.set(key, entry);
    $('toasts').append(entry.node);
    if (toasts.size > MAX_TOASTS) dismissToast(toasts.keys().next().value);
  }
  entry.node.className = `toast toast--${kind}`;
  entry.text.textContent = message;
  clearTimeout(entry.timer);
  entry.timer = setTimeout(() => dismissToast(key), kind === 'error' ? 12_000 : 5000);
}

export function dismissToast(key) {
  const entry = toasts.get(key);
  if (!entry) return;
  clearTimeout(entry.timer);
  entry.node.remove();
  toasts.delete(key);
}

// Ejecuta una tarea cada cierto tiempo sin superponer pedidos. Se pausa con la pestaña oculta
// y se pone al día apenas vuelve a estar visible.

const tickers = new Set();

export class Ticker {
  #task;
  #every;
  #timer = 0;
  #active = false;
  #running = false;
  #again = false;

  constructor(task, every) {
    this.#task = task;
    this.#every = every;
    tickers.add(this);
  }

  start() {
    if (this.#active) return;
    this.#active = true;
    this.now();
  }

  stop() {
    this.#active = false;
    this.#again = false;
    clearTimeout(this.#timer);
  }

  now() {
    if (!this.#active) return;
    if (this.#running) {
      this.#again = true;
      return;
    }
    clearTimeout(this.#timer);
    this.#run();
  }

  async #run() {
    if (!this.#active || this.#running || document.hidden) return;
    this.#running = true;
    try {
      await this.#task();
    } catch {
      // cada tarea informa sus propios errores
    } finally {
      this.#running = false;
      if (this.#active) {
        clearTimeout(this.#timer);
        this.#timer = setTimeout(() => this.#run(), this.#again ? 0 : this.#every);
        this.#again = false;
      }
    }
  }
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) for (const ticker of tickers) ticker.now();
});
