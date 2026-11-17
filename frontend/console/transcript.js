// Transcripción de una conversación: un <li> por mensaje, siempre como texto plano (sin enlaces).

import { AUTHORS, el, formatClock, formatDateTime, label, statusBadge, toDate } from './ui.js';

const KINDS = {
  interactive: 'Respuesta a un botón',
  document: 'Documento',
  image: 'Imagen',
  audio: 'Audio',
  video: 'Video',
  location: 'Ubicación',
  sticker: 'Sticker',
  reaction: 'Reacción',
  contact: 'Contacto',
  unsupported: 'Contenido no soportado',
};

const OUTCOMES = {
  answered: 'Consulta respondida con información',
  no_info: 'Consulta sin información disponible',
  refused: 'Consulta fuera de alcance',
  blocked: 'Consulta bloqueada',
  human_handling: 'Recibido durante la atención humana',
  opted_out: 'Sin respuesta: pidió no recibir mensajes',
  silent_kind: 'Sin respuesta: tipo de mensaje que no se contesta',
  throttled: 'Demasiados mensajes seguidos',
  unsupported_content: 'Contenido que el asistente no procesa',
  invalid_request: 'Consulta con formato no válido',
  quota_exceeded: 'Se alcanzó el límite de consultas',
};

// Cada comando con la palabra que escribe la persona.
const COMMANDS = {
  start: 'INICIO',
  help: 'AYUDA',
  human: 'HUMANO',
  privacy: 'PRIVACIDAD',
  erase: 'BORRAR',
  erase_confirm: 'BORRAR CONFIRMAR',
  link: 'VINCULAR',
  stop: 'BAJA',
  resume: 'ALTA',
};

function authorLabel(message) {
  const name = label(AUTHORS, message.author);
  return message.author === 'operator' && message.operator ? `${name} · ${message.operator}` : name;
}

function outcomeLabel(outcome) {
  if (outcome.startsWith('command:')) {
    const command = outcome.slice(8);
    return `Comando ${COMMANDS[command] ?? command.toUpperCase()}`;
  }
  return OUTCOMES[outcome] ?? `Resultado: ${outcome}`;
}

function timeNode(value) {
  const date = toDate(value);
  if (!date) return null;
  return el('time', { class: 'tmsg__time', dateTime: date.toISOString(), title: formatDateTime(date), text: formatClock(date) });
}

export function messageNode(message) {
  const inbound = message.direction === 'in';
  const author = ['user', 'bot', 'operator', 'system'].includes(message.author) ? message.author : 'system';
  const kind = message.kind && message.kind !== 'text' ? label(KINDS, message.kind) : null;
  const text = typeof message.text === 'string' && message.text ? message.text : kind ? `(${kind.toLowerCase()} sin texto)` : '(sin texto)';
  const node = el('li', { class: `tmsg tmsg--${inbound ? 'in' : 'out'} tmsg--${author}` }, [
    el('p', { class: 'tmsg__head' }, [
      el('span', { class: 'tmsg__author', text: authorLabel(message) }),
      timeNode(message.created_at),
      kind ? el('span', { class: 'tmsg__kind', text: kind }) : null,
    ]),
    el('p', { class: 'tmsg__text', text }),
    el('p', { class: 'tmsg__foot' }),
  ]);
  updateMessageNode(node, message);
  return node;
}

// Actualiza solo lo que puede cambiar en un mensaje ya mostrado: estado de entrega y errores.
export function updateMessageNode(node, message) {
  const signature = [message.status, message.outcome, message.error_code].join('|');
  if (node.dataset.signature === signature) return;
  node.dataset.signature = signature;

  const parts = [];
  if (message.direction === 'out' && message.status) {
    parts.push(el('span', { class: 'visually-hidden', text: 'Estado de entrega: ' }), statusBadge(message.status));
  } else if (message.direction === 'in' && message.outcome) {
    parts.push(el('span', { class: 'tmsg__outcome', text: outcomeLabel(String(message.outcome)) }));
  }
  if (message.error_code) parts.push(el('span', { class: 'tmsg__error', text: `Error: ${message.error_code}` }));

  const foot = node.querySelector('.tmsg__foot');
  foot.replaceChildren(...parts);
  foot.hidden = parts.length === 0;
  node.classList.toggle('tmsg--failed', message.status === 'failed' || message.status === 'dead');
}

// La lista no se rehace en cada refresco: se agregan los mensajes nuevos al final y se actualizan
// los estados, así no salta el scroll mientras se lee. Si cambió el orden, se arma de nuevo.
export function createTranscript(list, announce) {
  const nodes = new Map();
  let renderedIds = '';

  function clear() {
    nodes.clear();
    renderedIds = '';
    list.replaceChildren();
  }

  function render(messages) {
    const ids = messages.map((message) => message.id).join(',');
    const follow = !renderedIds || list.scrollHeight - list.scrollTop - list.clientHeight < 48;
    if (renderedIds && (ids === renderedIds || ids.startsWith(`${renderedIds},`))) {
      let incoming = 0;
      for (const message of messages) {
        const node = nodes.get(message.id);
        if (node) {
          updateMessageNode(node, message);
          continue;
        }
        const created = messageNode(message);
        nodes.set(message.id, created);
        list.append(created);
        if (message.direction === 'in') incoming += 1;
      }
      if (incoming) announce(incoming === 1 ? 'Llegó un mensaje nuevo.' : `Llegaron ${incoming} mensajes nuevos.`);
    } else {
      nodes.clear();
      const items = messages.map((message) => {
        const node = messageNode(message);
        nodes.set(message.id, node);
        return node;
      });
      list.replaceChildren(...(items.length ? items : [el('li', { class: 'transcript__empty', text: 'Todavía no hay mensajes.' })]));
    }
    renderedIds = ids;
    if (follow) list.scrollTop = list.scrollHeight;
  }

  return { clear, render };
}
