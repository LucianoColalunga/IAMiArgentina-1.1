// Detalle de una conversación: datos, transcripción, acciones de atención humana y respuesta.

import { api } from './api.js';
import { createTranscript } from './transcript.js';
import { $, CHANNELS, REASONS, Ticker, el, isBusy, label, setBusy, stateBadge, timeAgo, toast } from './ui.js';

const REFRESH_EVERY = 3000;
const REPLY_MAX = 4000;
const narrow = window.matchMedia('(max-width: 767.98px)');

// changed(): algo cambió y conviene refrescar la lista.
// closed(id, focus): se cerró el detalle; focus indica a dónde llevar el foco ('item', 'title' o null).
export function createDetail(ctx, { changed, closed }) {
  const detail = $('detail');
  const empty = $('detail-empty');
  const title = $('detail-title');
  const facts = $('detail-facts');
  const actions = $('detail-actions');
  const buttons = { claim: $('act-claim'), release: $('act-release'), close: $('act-close'), erase: $('act-erase') };
  const replyForm = $('reply-form');
  const replyText = $('reply-text');
  const replyHint = $('reply-hint');
  const replyCount = $('reply-count');
  const replySend = $('reply-send');
  const status = $('detail-status');
  const dialog = $('erase-dialog');
  const eraseConfirm = $('erase-confirm');

  const drafts = new Map(); // borradores de respuesta por conversación, solo en memoria
  const ticker = new Ticker(load, REFRESH_EVERY);
  const announce = (text) => { status.textContent = text; };
  const transcript = createTranscript($('transcript'), announce);
  const subject = () => ctx.me?.subject ?? '';
  let selectedId = null;
  let current = null;
  let gen = 0;
  let sending = false;

  function saveDraft() {
    if (!selectedId) return;
    if (replyText.value.trim()) drafts.set(selectedId, replyText.value);
    else drafts.delete(selectedId);
  }

  // focusTitle: se abrió desde otra sección (el botón que tenía el foco quedó oculto).
  function open(id, focusTitle = false) {
    if (!id) return;
    if (id !== selectedId) {
      saveDraft();
      selectedId = id;
      current = null;
      gen += 1;
      transcript.clear();
      facts.replaceChildren();
      title.textContent = 'Cargando conversación…';
      actions.hidden = true;
      replyText.value = drafts.get(id) ?? '';
      updateReply();
    }
    empty.hidden = true;
    detail.hidden = false;
    ticker.stop();
    ticker.start();
    if (focusTitle || narrow.matches) title.focus();
  }

  function close(focus = null) {
    const id = selectedId;
    if (dialog.open) dialog.close();
    selectedId = null;
    current = null;
    gen += 1;
    ticker.stop();
    transcript.clear();
    facts.replaceChildren();
    detail.hidden = true;
    empty.hidden = false;
    if (id) closed(id, focus);
  }

  async function load() {
    const id = selectedId;
    if (!id) return;
    const request = ++gen;
    try {
      const data = await api.conversation(id);
      if (request === gen && id === selectedId) render(data);
    } catch (error) {
      if (request !== gen || id !== selectedId) return;
      if (error.status === 404) {
        drafts.delete(id);
        toast('La conversación ya no existe.');
        close(detail.contains(document.activeElement) ? 'title' : null);
        return;
      }
      ctx.fail(error);
    }
  }

  function render(data) {
    current = data;
    title.textContent = `${label(CHANNELS, data.channel)} · ${data.contact || 'Contacto sin datos'}`;
    renderFacts(data);
    renderActions(data);
    transcript.render(Array.isArray(data.messages) ? data.messages : []);
    updateReply();
  }

  function renderFacts(data) {
    const me = subject();
    const rows = [
      ['Estado', stateBadge(data.state)],
      ['Asignada a', !data.assigned_to ? 'Nadie' : data.assigned_to === me ? 'Vos' : data.assigned_to],
      data.handoff_reason ? ['Motivo de la derivación', label(REASONS, data.handoff_reason)] : null,
      ['Inicio', timeAgo(data.opened_at)],
      ['Última actividad', timeAgo(data.last_activity_at)],
      data.no_info_count ? ['Consultas sin información', String(data.no_info_count)] : null,
      data.opted_out ? ['Mensajes', 'Pidió no recibir más mensajes'] : null,
      data.linked ? ['Identidad', 'Vinculada con otros canales'] : null,
    ].filter(Boolean);
    facts.replaceChildren(...rows.map(([term, value]) => el('div', { class: 'fact' }, [
      el('dt', { text: term }),
      el('dd', {}, value),
    ])));
  }

  function renderActions(data) {
    const mine = Boolean(data.assigned_to) && data.assigned_to === subject();
    const allowed = !data.assigned_to || mine || ctx.can('conversations:reassign');
    const isOpen = data.state !== 'closed';
    const withPerson = data.state === 'waiting_human' || data.state === 'human';
    const focused = actions.contains(document.activeElement) ? document.activeElement : null;

    buttons.claim.hidden = !(ctx.can('conversations:claim') && isOpen && allowed && !(data.state === 'human' && mine));
    buttons.release.hidden = !(ctx.can('conversations:close') && withPerson && allowed);
    buttons.close.hidden = !(ctx.can('conversations:close') && isOpen && allowed);
    buttons.erase.hidden = !ctx.can('contacts:delete');
    actions.hidden = Object.values(buttons).every((button) => button.hidden);
    // Si otra persona cambió la conversación y desapareció el botón con foco, el foco va al título.
    if (focused && (focused.hidden || actions.hidden)) title.focus();
  }

  // Respuesta: solo cuando la conversación está con una persona y asignada a quien escribe.

  function canReply(data = current) {
    return Boolean(data) && ctx.can('conversations:reply') && data.state === 'human'
      && Boolean(data.assigned_to) && data.assigned_to === subject();
  }

  function replyBlocker(data) {
    if (data.state === 'closed') return 'La conversación está cerrada.';
    if (data.state === 'human' && data.assigned_to && data.assigned_to !== subject()) {
      return `La está atendiendo ${data.assigned_to}.`;
    }
    return 'Para responder, primero tomá la conversación.';
  }

  function updateReply() {
    replyForm.hidden = !current || !ctx.can('conversations:reply');
    replyCount.textContent = `${replyText.value.length}/${REPLY_MAX}`;
    const allowed = canReply();
    replySend.disabled = !allowed || sending || !replyText.value.trim();
    replyHint.textContent = !current ? '' : allowed ? 'Ctrl + Enter para enviar.' : replyBlocker(current);
  }

  async function sendReply() {
    const id = selectedId;
    const text = replyText.value.trim();
    if (!id || !text || text.length > REPLY_MAX || sending || !canReply()) return;
    sending = true;
    if (document.activeElement === replySend) replyText.focus();
    updateReply();
    try {
      await api.reply(id, text);
      drafts.delete(id);
      if (selectedId === id) {
        replyText.value = '';
        announce('Respuesta enviada.');
      }
      ticker.now();
      changed();
    } catch (error) {
      ctx.fail(error);
      if (error.status === 409) ticker.now();
    } finally {
      sending = false;
      updateReply();
    }
  }

  async function act(button, run, success, after) {
    const id = selectedId;
    if (!id || isBusy(button)) return;
    setBusy(button, true);
    try {
      await run(id);
      toast(success, { kind: 'success' });
      if (id === selectedId) after();
    } catch (error) {
      ctx.fail(error); // un 409 trae el motivo; igual se refresca abajo
    } finally {
      setBusy(button, false);
      ticker.now();
      changed();
    }
  }

  async function erase() {
    const id = selectedId;
    if (isBusy(eraseConfirm)) return;
    if (!id) {
      dialog.close();
      return;
    }
    setBusy(eraseConfirm, true);
    try {
      await api.eraseContact(id);
      drafts.delete(id);
      toast('Se borraron los datos del contacto.', { kind: 'success' });
      if (selectedId === id) close('title');
      else dialog.close();
    } catch (error) {
      dialog.close();
      ctx.fail(error);
      ticker.now();
    } finally {
      setBusy(eraseConfirm, false);
      changed();
    }
  }

  $('detail-back').addEventListener('click', () => {
    saveDraft();
    close('item');
  });

  replyForm.addEventListener('submit', (event) => {
    event.preventDefault();
    sendReply();
  });

  replyText.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || !(event.ctrlKey || event.metaKey)) return;
    event.preventDefault();
    sendReply();
  });

  replyText.addEventListener('input', updateReply);

  buttons.claim.addEventListener('click', () => act(buttons.claim, api.claim, 'Tomaste la conversación.', () => replyText.focus()));
  buttons.release.addEventListener('click', () => act(buttons.release, api.release, 'La conversación volvió al asistente.', () => title.focus()));
  buttons.close.addEventListener('click', () => act(buttons.close, api.close, 'Cerraste la conversación.', () => title.focus()));

  buttons.erase.addEventListener('click', () => {
    if (!current) return;
    $('erase-contact').textContent = current.contact || '(sin datos)';
    dialog.showModal();
  });

  $('erase-cancel').addEventListener('click', () => dialog.close());
  eraseConfirm.addEventListener('click', erase);

  return {
    get id() {
      return selectedId;
    },
    open,
    show() {
      if (selectedId) ticker.start();
    },
    hide() {
      ticker.stop();
    },
    reset() {
      close();
      drafts.clear();
      replyText.value = '';
    },
  };
}
