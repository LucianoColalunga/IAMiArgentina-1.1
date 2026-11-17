// Chat web: envío de mensajes, estado de cada envío, indicador de escritura y conversación guardada.

import { ApiError, ChatSession, newMessageId } from './session.js';
import { Poller } from './poller.js';
import { createMessage, updateMessage } from './view.js';

const MAX_LENGTH = 1000;
const TYPING_LIMIT = 60_000;
const DEFAULT_COOLDOWN = 15;
const INPUT_MAX_HEIGHT = 160;
const SERVER_AUTHORS = new Set(['bot', 'operator', 'system']);
const EXPIRED_NOTICE = 'La sesión anterior venció. Si volvés a escribir, empieza una conversación nueva.';
const RENEWED_NOTICE = 'La sesión anterior venció, así que tu mensaje inició una conversación nueva.';

const $ = (id) => document.getElementById(id);
const scroller = $('chat');
const list = $('messages');
const welcome = $('welcome');
const typing = $('typing');
const form = $('composer');
const input = $('message-input');
const sendButton = $('send');
const sendLabel = $('send-label');
const counter = $('char-count');
const status = $('composer-status');
const srStatus = $('sr-status');
const dialog = $('new-chat-dialog');

const session = new ChatSession();
const poller = new Poller(session, { onMessages: receive, onExpired: expire });
const nodes = new Map();
let log = [];
let generation = 0; // cambia con "Nueva conversación": descarta respuestas de envíos anteriores
let typingTimer = 0;
let cooldownUntil = 0;
let cooldownTimer = 0;

session.onRenew = (replaced) => {
  if (replaced && log.length) addNotice(RENEWED_NOTICE);
  poller.restart();
};

function nearBottom() {
  return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 96;
}

function scrollToEnd() {
  scroller.scrollTop = scroller.scrollHeight;
}

function persist() {
  session.saveLog(log);
}

function render(entry) {
  const node = createMessage(entry);
  nodes.set(entry.key, node);
  list.append(node);
}

function addEntry(entry) {
  const follow = entry.author === 'user' || nearBottom();
  log.push(entry);
  render(entry);
  welcome.hidden = true;
  persist();
  if (follow) scrollToEnd();
}

function addNotice(text) {
  const last = log.at(-1);
  if (last?.local && last.text === text) return;
  addEntry({ key: `n:${newMessageId()}`, author: 'system', local: true, text, at: new Date().toISOString() });
}

function setState(entry, changes) {
  Object.assign(entry, changes);
  const node = nodes.get(entry.key);
  if (!node) return;
  updateMessage(node, entry);
  persist();
  refreshComposer();
}

function cleanSources(sources) {
  if (!Array.isArray(sources)) return [];
  return sources
    .filter((source) => typeof source?.url === 'string')
    .slice(0, 8)
    .map((source) => ({ title: typeof source.title === 'string' ? source.title : '', url: source.url }));
}

function receive(messages) {
  let last = session.lastSeq;
  let replied = false;
  const ordered = messages.filter((message) => Number.isSafeInteger(message?.seq)).sort((a, b) => a.seq - b.seq);
  for (const message of ordered) {
    if (message.seq <= last) continue;
    last = message.seq;
    const author = SERVER_AUTHORS.has(message.author) ? message.author : 'system';
    addEntry({
      key: `s:${message.seq}`,
      author,
      text: typeof message.text === 'string' ? message.text : '',
      sources: cleanSources(message.sources),
      at: typeof message.created_at === 'string' ? message.created_at : new Date().toISOString(),
    });
    if (author !== 'system') replied = true;
  }
  if (last !== session.lastSeq) session.lastSeq = last;
  if (replied) stopTyping();
}

function expire() {
  session.discard();
  stopTyping();
  if (log.length) addNotice(EXPIRED_NOTICE);
}

function startTyping() {
  const follow = nearBottom();
  clearTimeout(typingTimer);
  typing.textContent = 'El asistente está escribiendo…';
  typing.classList.add('is-active');
  typingTimer = setTimeout(stopTyping, TYPING_LIMIT);
  if (follow) scrollToEnd();
}

function stopTyping() {
  clearTimeout(typingTimer);
  typing.classList.remove('is-active');
  typing.textContent = '';
}

function cooldownLeft() {
  return Math.max(0, Math.ceil((cooldownUntil - Date.now()) / 1000));
}

function startCooldown(seconds, message) {
  const span = Math.min(Math.max(seconds || DEFAULT_COOLDOWN, 1), 3600);
  cooldownUntil = Math.max(cooldownUntil, Date.now() + span * 1000);
  status.textContent = message;
  clearInterval(cooldownTimer);
  cooldownTimer = setInterval(() => {
    if (cooldownLeft() === 0) {
      clearInterval(cooldownTimer);
      status.textContent = 'Ya podés volver a enviar mensajes.';
    }
    refreshComposer();
  }, 1000);
  refreshComposer();
}

function fitInput() {
  input.style.height = 'auto';
  const borders = input.offsetHeight - input.clientHeight;
  input.style.height = `${Math.min(input.scrollHeight + borders, INPUT_MAX_HEIGHT)}px`;
}

function refreshComposer() {
  const length = input.value.length;
  const waiting = cooldownLeft();
  counter.textContent = `${length}/${MAX_LENGTH}`;
  counter.classList.toggle('is-near', length >= MAX_LENGTH * 0.9);
  sendButton.disabled = waiting > 0 || input.value.trim() === '';
  sendLabel.textContent = waiting > 0 ? `Esperá ${waiting} s` : 'Enviar';
  for (const button of list.querySelectorAll('.msg__retry')) button.disabled = waiting > 0;
}

function isRetryable(statusCode) {
  return statusCode === 0 || statusCode === 401 || statusCode === 404 || statusCode === 408
    || statusCode === 429 || statusCode >= 500;
}

async function deliver(entry) {
  const current = generation;
  const retrying = entry.state === 'failed';
  setState(entry, { state: 'sending', error: '', retryable: false });
  startTyping();
  try {
    const result = await session.send(entry.text, entry.id);
    if (current !== generation) return;
    if (!result?.accepted && !result?.duplicated) throw new ApiError(0, 'El mensaje no se pudo procesar.');
    setState(entry, { state: 'sent' });
    if (cooldownLeft() === 0) status.textContent = '';
    if (retrying) srStatus.textContent = 'Se envió el mensaje.';
    poller.start();
  } catch (error) {
    if (current !== generation) return;
    const failure = error instanceof ApiError ? error : new ApiError(0, 'Algo salió mal. Probá de nuevo.');
    setState(entry, { state: 'failed', error: failure.message, retryable: isRetryable(failure.status) });
    if (!log.some((item) => item.state === 'sending')) stopTyping();
    if (failure.status === 429 || failure.status === 503) startCooldown(failure.retryAfter, failure.message);
    else srStatus.textContent = `No se envió el mensaje. ${failure.message}`;
  }
}

function submit() {
  const text = input.value.trim();
  if (!text || cooldownLeft() > 0) return;
  const id = newMessageId();
  const entry = {
    key: `u:${id}`,
    id,
    author: 'user',
    text: text.slice(0, MAX_LENGTH),
    at: new Date().toISOString(),
    state: 'sending',
  };
  input.value = '';
  input.focus();
  fitInput();
  status.textContent = '';
  addEntry(entry);
  deliver(entry);
}

function reset() {
  generation += 1;
  poller.stop();
  session.clear();
  log = [];
  nodes.clear();
  persist();
  list.replaceChildren();
  stopTyping();
  welcome.hidden = false;
  if (cooldownLeft() === 0) status.textContent = '';
  srStatus.textContent = 'Empezaste una conversación nueva.';
  refreshComposer();
  input.focus();
}

// Vuelve a mostrar la conversación guardada en la pestaña (por ejemplo, después de recargar).
function restore() {
  list.setAttribute('aria-busy', 'true');
  for (const saved of session.loadLog()) {
    const entry = { ...saved };
    if (entry.author === 'user' && entry.state === 'sending') {
      // No sabemos si llegó: reintentar es seguro porque se reusa el mismo client_message_id.
      Object.assign(entry, { state: 'failed', error: 'Se interrumpió el envío.', retryable: true });
    }
    log.push(entry);
    render(entry);
  }
  list.removeAttribute('aria-busy');
  welcome.hidden = log.length > 0;
  if (session.lapsed && log.length) addNotice(EXPIRED_NOTICE);
  persist();
  scrollToEnd();
}

form.addEventListener('submit', (event) => {
  event.preventDefault();
  submit();
});

input.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' || event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) return;
  if (event.isComposing || event.keyCode === 229) return; // confirmando texto con un IME
  event.preventDefault();
  submit();
});

input.addEventListener('input', () => {
  fitInput();
  refreshComposer();
});

list.addEventListener('click', (event) => {
  const button = event.target.closest('button[data-retry]');
  if (!button || cooldownLeft() > 0) return;
  const entry = log.find((item) => item.key === button.dataset.retry);
  if (!entry || entry.state !== 'failed') return;
  input.focus();
  deliver(entry);
});

welcome.addEventListener('click', (event) => {
  const button = event.target.closest('button[data-suggestion]');
  if (!button) return;
  input.value = button.textContent.trim();
  fitInput();
  refreshComposer();
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
});

$('new-chat').addEventListener('click', () => {
  if (!log.length && !session.token) {
    input.focus();
    return;
  }
  dialog.showModal();
});

$('new-chat-cancel').addEventListener('click', () => dialog.close());

$('new-chat-confirm').addEventListener('click', () => {
  dialog.close();
  reset();
});

restore();
refreshComposer();
poller.start();
