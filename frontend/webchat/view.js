// Armado de los mensajes en el DOM. Todo el texto entra con textContent o como nodos de texto.

const AUTHORS = { user: 'Vos', bot: 'Asistente', operator: 'Operador/a', system: 'Aviso' };
const STATES = { sending: 'enviando…', sent: 'enviado', failed: 'no se envió' };
const MAX_SOURCES = 5;

// Solo URLs https. Se excluyen caracteres que no pueden ser parte de una URL sin codificar
// y las comillas tipográficas que suelen rodearlas en un texto.
const URL_PATTERN = /https:\/\/[^\s<>"'`{}|\\^[\]«»“”‘’]+/g;

const clock = new Intl.DateTimeFormat('es-AR', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function safeUrl(value) {
  if (typeof value !== 'string' || !value.startsWith('https://')) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && url.hostname ? url : null;
  } catch {
    return null;
  }
}

function link(url, text) {
  const anchor = el('a', 'link', text);
  anchor.href = url.href;
  anchor.target = '_blank';
  anchor.rel = 'noopener noreferrer';
  anchor.append(el('span', 'visually-hidden', ' (se abre en otra pestaña)'));
  return anchor;
}

function count(text, char) {
  return text.split(char).length - 1;
}

// Saca la puntuación final que queda pegada a una URL ("…argentina.gob.ar.") y los paréntesis
// de cierre que no tienen su apertura dentro de la URL.
function trimUrl(raw) {
  let end = raw.length;
  while (end > 0) {
    const char = raw[end - 1];
    const head = raw.slice(0, end);
    if ('.,;:!?'.includes(char) || (char === ')' && count(head, '(') < count(head, ')'))) {
      end -= 1;
    } else {
      break;
    }
  }
  return raw.slice(0, end);
}

function appendLinkified(parent, text) {
  let last = 0;
  for (const match of text.matchAll(URL_PATTERN)) {
    const href = trimUrl(match[0]);
    const url = safeUrl(href);
    if (!url) continue;
    parent.append(text.slice(last, match.index), link(url, href));
    last = match.index + href.length;
  }
  parent.append(text.slice(last));
}

function createSources(sources) {
  if (!Array.isArray(sources) || !sources.length) return null;
  const list = el('ul', 'sources__list');
  const seen = new Set();
  for (const source of sources) {
    const url = safeUrl(source?.url);
    if (!url || seen.has(url.href) || seen.size >= MAX_SOURCES) continue;
    seen.add(url.href);
    const title = typeof source.title === 'string' ? source.title.trim() : '';
    const item = el('li', 'sources__item');
    item.append(link(url, title || url.hostname));
    if (title) item.append(' ', el('span', 'sources__host', url.hostname));
    list.append(item);
  }
  if (!list.children.length) return null;
  const box = el('div', 'sources');
  box.append(el('p', 'sources__title', 'Fuentes'), list);
  return box;
}

function timeElement(value) {
  const node = el('time', 'msg__time');
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return node;
  node.dateTime = date.toISOString();
  node.textContent = clock.format(date);
  return node;
}

export function createMessage(entry) {
  const item = el('div', `msg msg--${entry.author}`);
  item.dataset.key = entry.key;
  item.append(el('p', 'msg__author', AUTHORS[entry.author] ?? AUTHORS.system));

  const bubble = el('div', 'msg__bubble');
  const text = el('p', 'msg__text');
  if (entry.author === 'user') text.textContent = entry.text;
  else appendLinkified(text, entry.text);
  bubble.append(text);
  const sources = createSources(entry.sources);
  if (sources) bubble.append(sources);

  item.append(bubble, el('div', 'msg__meta'));
  updateMessage(item, entry);
  return item;
}

// Rehace la línea de hora y estado (para los mensajes propios: enviando, enviado o error).
export function updateMessage(item, entry) {
  const meta = item.querySelector('.msg__meta');
  meta.replaceChildren(timeElement(entry.at));
  if (entry.author !== 'user') return;

  item.classList.toggle('msg--sending', entry.state === 'sending');
  item.classList.toggle('msg--failed', entry.state === 'failed');
  meta.append(el('span', 'msg__state', STATES[entry.state] ?? ''));
  if (entry.state !== 'failed') return;

  if (entry.error) meta.append(el('span', 'msg__error', entry.error));
  if (entry.retryable) {
    const retry = el('button', 'msg__retry', 'Reintentar');
    retry.type = 'button';
    retry.dataset.retry = entry.key;
    meta.append(retry);
  }
}
