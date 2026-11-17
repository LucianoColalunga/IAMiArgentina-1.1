// Pestañas de problemas de envío, canales y auditoría.

import { api } from './api.js';
import {
  $, AUDIT_ACTIONS, AUDIT_TARGETS, AUTHORS, CHANNELS, STATUSES, Ticker, el, formatClock, formatDateTime,
  isBusy, label, setBusy, statusBadge, timeAgo, toast,
} from './ui.js';

const AUDIT_PAGE = 100;
const QUEUE_WARNING = 100;

const number = (value) => (Number(value) || 0).toLocaleString('es-AR');

export function createProblems(ctx) {
  const body = $('problems-body');
  const wrap = $('problems-wrap');
  const empty = $('problems-empty');
  const heading = $('problems-title');
  const refresh = $('problems-refresh');
  let gen = 0;

  async function load() {
    const current = ++gen;
    setBusy(refresh, true);
    try {
      const data = await api.problems();
      if (current !== gen) return;
      const items = Array.isArray(data?.items) ? data.items : [];
      body.replaceChildren(...items.map(row));
      wrap.hidden = items.length === 0;
      empty.hidden = items.length > 0;
    } catch (error) {
      if (current === gen) ctx.fail(error);
    } finally {
      if (current === gen) setBusy(refresh, false);
    }
  }

  function row(item) {
    const buttons = [
      el('button', { type: 'button', class: 'btn btn--small', text: 'Reintentar envío', dataset: { retry: String(item.id) } }),
    ];
    if (ctx.can('conversations:read') && item.conversation_id) {
      buttons.push(el('button', {
        type: 'button',
        class: 'btn btn--small btn--ghost',
        text: 'Ver conversación',
        dataset: { open: String(item.conversation_id) },
      }));
    }
    const error = [
      item.error_code ? el('code', { text: item.error_code }) : null,
      item.error_detail ? el('span', { class: 'cell-detail', text: item.error_detail }) : null,
    ].filter(Boolean);
    return el('tr', {}, [
      el('td', {}, timeAgo(item.updated_at)),
      el('td', { text: label(CHANNELS, item.channel) }),
      el('td', { text: label(AUTHORS, item.author) }),
      el('td', {}, statusBadge(item.status)),
      el('td', { class: 'num', text: number(item.attempts) }),
      el('td', { class: 'cell-error' }, error.length ? error : '—'),
      el('td', {}, el('div', { class: 'cell-actions' }, buttons)),
    ]);
  }

  async function retry(button) {
    if (isBusy(button)) return;
    setBusy(button, true);
    try {
      await api.retry(button.dataset.retry);
      toast('El mensaje volvió a la cola de envío.', { kind: 'success' });
      heading.focus(); // la fila desaparece al recargar
    } catch (error) {
      ctx.fail(error);
      setBusy(button, false);
      if (error.status !== 404) return;
    }
    await load();
  }

  body.addEventListener('click', (event) => {
    const open = event.target.closest('button[data-open]');
    if (open) {
      ctx.openConversation(open.dataset.open);
      return;
    }
    const button = event.target.closest('button[data-retry]');
    if (button) retry(button);
  });

  refresh.addEventListener('click', () => {
    if (!isBusy(refresh)) load();
  });

  return {
    show: load,
    hide() {},
    reset() {
      gen += 1;
      setBusy(refresh, false);
      body.replaceChildren();
      wrap.hidden = true;
      empty.hidden = true;
    },
  };
}

export function createChannels(ctx) {
  const list = $('channel-list');
  const queues = $('queues-body');
  const summary = $('summary-body');
  const summaryWrap = $('summary-wrap');
  const summaryEmpty = $('summary-empty');
  const updated = $('channels-updated');
  const ticker = new Ticker(load, 15_000);
  let gen = 0;

  async function load() {
    const current = ++gen;
    try {
      const data = await api.channels();
      if (current === gen) render(data ?? {});
    } catch (error) {
      if (current === gen) ctx.fail(error);
    }
  }

  function render(data) {
    const enabled = new Set(Array.isArray(data.enabled) ? data.enabled : []);
    const available = Array.isArray(data.available) ? data.available : [...enabled];
    list.replaceChildren(...available.map((channel) => {
      const on = enabled.has(channel);
      return el('li', { class: `channel${on ? ' channel--on' : ''}` }, [
        el('span', { class: 'channel__name', text: label(CHANNELS, channel) }),
        el('span', { class: 'channel__state', text: on ? 'Habilitado' : 'Deshabilitado' }),
      ]);
    }));

    const depths = Object.entries(data.queues && typeof data.queues === 'object' ? data.queues : {})
      .sort(([a], [b]) => (a === 'inbound' ? -1 : b === 'inbound' ? 1 : a.localeCompare(b)));
    queues.replaceChildren(...depths.map(([name, depth]) => {
      const known = typeof depth === 'number' && Number.isFinite(depth);
      return el('tr', {}, [
        el('th', { scope: 'row', text: name === 'inbound' ? 'Entrada (todos los canales)' : `Salida · ${label(CHANNELS, name)}` }),
        el('td', {
          class: `num${known && depth >= QUEUE_WARNING ? ' is-warning' : ''}`,
          text: known ? number(depth) : 'Sin datos',
        }),
      ]);
    }));

    // El servidor devuelve una fila por canal, dirección y estado; acá se agrupa por canal.
    const byChannel = new Map();
    for (const item of Array.isArray(data.last_24h) ? data.last_24h : []) {
      const total = Number(item?.total) || 0;
      const entry = byChannel.get(item.channel) ?? { inbound: 0, outbound: 0, failed: 0, parts: [] };
      if (item.direction === 'in') entry.inbound += total;
      else entry.outbound += total;
      if (item.status === 'failed' || item.status === 'dead') entry.failed += total;
      entry.parts.push(`${item.direction === 'in' ? 'Entrantes' : 'Salientes'} ${label(STATUSES, item.status)}: ${number(total)}`);
      byChannel.set(item.channel, entry);
    }
    summary.replaceChildren(...[...byChannel].map(([channel, entry]) => el('tr', {}, [
      el('th', { scope: 'row', text: label(CHANNELS, channel) }),
      el('td', { class: 'num', text: number(entry.inbound) }),
      el('td', { class: 'num', text: number(entry.outbound) }),
      el('td', { class: `num${entry.failed ? ' is-danger' : ''}`, text: number(entry.failed) }),
      el('td', { class: 'cell-detail', text: entry.parts.join(' · ') }),
    ])));
    summaryWrap.hidden = byChannel.size === 0;
    summaryEmpty.hidden = byChannel.size > 0;
    updated.textContent = `Actualizado a las ${formatClock(new Date(), true)}`;
  }

  $('channels-refresh').addEventListener('click', () => ticker.now());

  return {
    show: () => ticker.start(),
    hide: () => ticker.stop(),
    reset() {
      ticker.stop();
      gen += 1;
      list.replaceChildren();
      queues.replaceChildren();
      summary.replaceChildren();
      updated.textContent = '';
    },
  };
}

function auditDetail(detail) {
  let value = detail;
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value);
    } catch {
      return value || '—';
    }
  }
  if (!value || typeof value !== 'object') return value ? String(value) : '—';
  const entries = Object.entries(value);
  if (!entries.length) return '—';
  return entries
    .map(([key, item]) => `${key}: ${item !== null && typeof item === 'object' ? JSON.stringify(item) : String(item)}`)
    .join(' · ');
}

export function createAudit(ctx) {
  const body = $('audit-body');
  const wrap = $('audit-wrap');
  const empty = $('audit-empty');
  const count = $('audit-count');
  const more = $('audit-more');
  const heading = $('audit-title');
  const refresh = $('audit-refresh');
  let gen = 0;
  let lastId = null;
  let loaded = false;

  function row(item) {
    return el('tr', {}, [
      el('td', { class: 'nowrap', text: formatDateTime(item.at) }),
      el('td', { text: item.actor || '—' }),
      el('td', { text: label(AUDIT_ACTIONS, item.action) }),
      el('td', {}, item.target_type || item.target_id ? [
        item.target_type ? label(AUDIT_TARGETS, item.target_type) : null,
        item.target_id ? el('code', { class: 'cell-id', text: String(item.target_id) }) : null,
      ] : '—'),
      el('td', { class: 'cell-detail', text: auditDetail(item.detail) }),
    ]);
  }

  async function load(fromStart) {
    const current = ++gen;
    setBusy(more, true);
    setBusy(refresh, true);
    try {
      const data = await api.audit(fromStart ? null : lastId);
      if (current !== gen) return;
      const items = Array.isArray(data?.items) ? data.items : [];
      const rows = items.map(row);
      if (fromStart) body.replaceChildren(...rows);
      else body.append(...rows);
      if (items.length) lastId = items[items.length - 1].id;
      else if (fromStart) lastId = null;
      const total = body.rows.length;
      count.textContent = total ? `${number(total)} registros` : '';
      wrap.hidden = total === 0;
      empty.hidden = total > 0;
      const hadFocus = document.activeElement === more;
      more.hidden = items.length < AUDIT_PAGE;
      if (hadFocus && more.hidden) heading.focus();
    } catch (error) {
      if (current === gen) ctx.fail(error);
    } finally {
      if (current === gen) {
        setBusy(more, false);
        setBusy(refresh, false);
      }
    }
  }

  more.addEventListener('click', () => {
    if (!isBusy(more) && lastId !== null) load(false);
  });

  refresh.addEventListener('click', () => {
    if (!isBusy(refresh)) load(true);
  });

  return {
    show() {
      if (loaded) return;
      loaded = true;
      load(true);
    },
    hide() {},
    reset() {
      gen += 1;
      loaded = false;
      lastId = null;
      setBusy(more, false);
      setBusy(refresh, false);
      body.replaceChildren();
      wrap.hidden = true;
      empty.hidden = true;
      more.hidden = true;
      count.textContent = '';
    },
  };
}
