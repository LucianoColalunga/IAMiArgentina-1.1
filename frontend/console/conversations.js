// Pestaña de conversaciones: lista con filtros de estado y, al lado, el detalle de la elegida.

import { api } from './api.js';
import { createDetail } from './detail.js';
import {
  $, CHANNELS, Ticker, el, formatClock, label, minutesSince, refreshTimes, stateBadge, timeAgo, toast,
} from './ui.js';

const LIST_EVERY = 5000;
const LATE_MINUTES = 10; // a partir de acá una espera se marca como demorada
const DEFAULT_STATES = ['waiting_human', 'human'];

export function createConversations(ctx) {
  const split = $('split');
  const list = $('conv-list');
  const listTitle = $('list-title');
  const listEmpty = $('list-empty');
  const listUpdated = $('list-updated');
  const filters = $('filters');

  const states = new Set(DEFAULT_STATES);
  const listTicker = new Ticker(loadList, LIST_EVERY);
  let listSignature = '';
  let listGen = 0;

  const findItem = (id) => [...list.querySelectorAll('button[data-id]')].find((button) => button.dataset.id === id);

  const detail = createDetail(ctx, {
    changed: () => listTicker.now(),
    closed(id, focus) {
      split.classList.remove('has-selection');
      markSelected();
      listTicker.now();
      if (focus === 'item') (findItem(id) ?? listTitle).focus();
      else if (focus === 'title') listTitle.focus();
    },
  });

  async function loadList() {
    const gen = ++listGen;
    try {
      const data = await api.conversations([...states]);
      if (gen === listGen) renderList(Array.isArray(data?.items) ? data.items : []);
    } catch (error) {
      if (gen === listGen) ctx.fail(error);
    }
  }

  function renderList(items) {
    const waiting = items.filter((item) => item.state === 'waiting_human');
    const late = waiting.filter((item) => minutesSince(item.last_activity_at) >= LATE_MINUTES).map((item) => item.id);
    ctx.setWaiting(states.has('waiting_human') ? waiting.length : 0);
    listUpdated.textContent = `Actualizado a las ${formatClock(new Date(), true)}`;
    listEmpty.hidden = items.length > 0;

    // Si nada cambió solo se actualizan los "hace X min", así no se pierde el foco de teclado.
    const signature = `${JSON.stringify(items)}|${late.join(',')}`;
    if (signature === listSignature) {
      refreshTimes(list);
      return;
    }
    listSignature = signature;
    const focused = list.contains(document.activeElement) ? document.activeElement.dataset.id : null;
    list.replaceChildren(...items.map(listItem));
    if (focused) findItem(focused)?.focus({ preventScroll: true });
  }

  function assignee(value) {
    if (!value) return 'Sin asignar';
    return value === ctx.me?.subject ? 'La atendés vos' : `La atiende ${value}`;
  }

  function listItem(item) {
    const waiting = item.state === 'waiting_human';
    const late = waiting && minutesSince(item.last_activity_at) >= LATE_MINUTES;
    const id = String(item.id);
    const button = el('button', {
      type: 'button',
      class: ['conv', waiting && 'conv--waiting', late && 'conv--late'].filter(Boolean).join(' '),
      dataset: { id },
      'aria-current': id === detail.id ? 'true' : undefined,
    }, [
      el('span', { class: 'conv__top' }, [
        el('span', { class: 'conv__contact', text: item.contact || 'Contacto sin datos' }),
        el('span', { class: 'conv__channel', text: label(CHANNELS, item.channel) }),
      ]),
      el('span', { class: 'conv__state' }, [
        stateBadge(item.state),
        waiting ? timeAgo(item.last_activity_at, 'Esperando desde ') : null,
      ]),
      item.preview ? el('span', { class: 'conv__preview', text: item.preview }) : null,
      el('span', { class: 'conv__meta' }, [
        el('span', { text: assignee(item.assigned_to) }),
        waiting ? null : timeAgo(item.last_activity_at, 'Actividad '),
      ]),
    ]);
    return el('li', {}, button);
  }

  function markSelected() {
    for (const button of list.querySelectorAll('button[data-id]')) {
      if (button.dataset.id === detail.id) button.setAttribute('aria-current', 'true');
      else button.removeAttribute('aria-current');
    }
  }

  function open(id, focusTitle = false) {
    if (!id) return;
    // Primero se muestra el panel (en pantallas angostas reemplaza a la lista) y después se lo
    // carga: un elemento oculto no puede recibir el foco.
    split.classList.add('has-selection');
    detail.open(id, focusTitle);
    markSelected();
  }

  filters.addEventListener('click', (event) => {
    const chip = event.target.closest('button[data-state]');
    if (!chip) return;
    const { state } = chip.dataset;
    if (states.has(state) && states.size === 1) {
      toast('Tiene que quedar al menos un estado elegido.');
      return;
    }
    if (states.has(state)) states.delete(state);
    else states.add(state);
    chip.setAttribute('aria-pressed', String(states.has(state)));
    listGen += 1; // descarta una respuesta en curso pedida con los filtros anteriores
    listTicker.now();
  });

  list.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-id]');
    if (button) open(button.dataset.id);
  });

  return {
    // La lista se refresca aunque se esté en otra sección de la consola: así el contador de espera
    // de la pestaña "Conversaciones" queda al día. Con la pestaña del navegador oculta se pausa.
    start() {
      listTicker.start();
    },
    show() {
      listTicker.start();
      detail.show();
    },
    hide() {
      detail.hide();
    },
    open,
    reset() {
      listTicker.stop();
      listGen += 1;
      detail.reset();
      states.clear();
      for (const state of DEFAULT_STATES) states.add(state);
      for (const chip of filters.querySelectorAll('button[data-state]')) {
        chip.setAttribute('aria-pressed', String(states.has(chip.dataset.state)));
      }
      list.replaceChildren();
      listSignature = '';
      listUpdated.textContent = '';
      listEmpty.hidden = true;
    },
  };
}
