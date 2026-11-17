// Consola de operadores: ingreso, pestañas según permisos y manejo central de errores.

import { ApiError, api, clearToken, getToken, setToken } from './api.js';
import { createConversations } from './conversations.js';
import { createAudit, createChannels, createProblems } from './panels.js';
import { $, ROLES, el, label, toast } from './ui.js';

const TABS = [
  { id: 'conversations', permission: 'conversations:read' },
  { id: 'problems', permission: 'dead_letters:manage' },
  { id: 'channels', permission: 'channels:read' },
  { id: 'audit', permission: 'audit:read' },
];
const VIEWS = ['boot', 'login', 'error', 'denied', 'app'];
const BASE_TITLE = document.title;
const PRINTABLE = /^[!-~]+$/; // un token va en una cabecera HTTP: solo ASCII visible

let me = null;
let activeTab = null;

const ctx = {
  get me() {
    return me;
  },
  can: (permission) => Boolean(me?.permissions.includes(permission)),
  fail,
  setWaiting,
  openConversation(id) {
    if (!ctx.can('conversations:read')) return;
    selectTab('conversations');
    panels.conversations.open(id, true);
  },
};

const panels = {
  conversations: createConversations(ctx),
  problems: createProblems(ctx),
  channels: createChannels(ctx),
  audit: createAudit(ctx),
};

function showView(name) {
  for (const view of VIEWS) $(`view-${view}`).hidden = view !== name;
}

function setLoginError(text) {
  $('login-error').textContent = text;
  if (text) $('token-input').setAttribute('aria-invalid', 'true');
  else $('token-input').removeAttribute('aria-invalid');
}

function showLogin(message = '') {
  showView('login');
  $('login-notice').textContent = message;
  setLoginError('');
  $('token-input').focus();
}

function setWaiting(count) {
  const badge = $('waiting-badge');
  badge.hidden = !count;
  badge.replaceChildren(String(count), el('span', {
    class: 'visually-hidden',
    text: count === 1 ? ' conversación esperando' : ' conversaciones esperando',
  }));
  document.title = count ? `(${count}) ${BASE_TITLE}` : BASE_TITLE;
}

function fail(error) {
  if (error?.status === 401) {
    logout('Tu sesión venció o el token ya no es válido. Ingresá de nuevo.');
    return;
  }
  // Los mensajes del servidor ya vienen pensados para mostrarse; un error de programación no.
  toast(error instanceof ApiError ? error.message : 'Ocurrió un error inesperado.', { kind: 'error' });
}

function selectTab(id, focus = false) {
  if (activeTab && activeTab !== id) panels[activeTab].hide();
  for (const tab of TABS) {
    const selected = tab.id === id;
    const button = $(`tab-${tab.id}`);
    button.setAttribute('aria-selected', String(selected));
    button.tabIndex = selected ? 0 : -1;
    $(`panel-${tab.id}`).hidden = !selected;
  }
  activeTab = id;
  panels[id].show();
  if (focus) $(`tab-${id}`).focus();
}

function enter(profile) {
  me = {
    subject: String(profile?.subject ?? ''),
    name: typeof profile?.name === 'string' ? profile.name : '',
    roles: Array.isArray(profile?.roles) ? profile.roles.map(String) : [],
    permissions: Array.isArray(profile?.permissions) ? profile.permissions.map(String) : [],
  };
  $('who-name').textContent = me.name || me.subject;
  $('who-roles').replaceChildren(...me.roles.map((role) => el('span', { class: 'role', text: label(ROLES, role) })));
  $('who').hidden = false;

  const allowed = TABS.filter((tab) => ctx.can(tab.permission));
  for (const tab of TABS) $(`tab-${tab.id}`).hidden = !allowed.includes(tab);
  if (!allowed.length) {
    showView('denied');
    return;
  }
  showView('app');
  if (ctx.can('conversations:read')) panels.conversations.start();
  selectTab(allowed[0].id);
}

function logout(message = '') {
  clearToken();
  me = null;
  activeTab = null;
  for (const panel of Object.values(panels)) panel.reset();
  setWaiting(0);
  $('who').hidden = true;
  showLogin(message);
}

async function boot() {
  showView('boot');
  try {
    // Primero sin token propio: detrás del proxy OIDC la cabecera la agrega el proxy.
    enter(await api.me());
  } catch (error) {
    if (error?.status === 401) {
      const hadToken = Boolean(getToken());
      clearToken();
      showLogin(hadToken ? 'Tu sesión venció. Ingresá de nuevo.' : '');
      return;
    }
    $('error-text').textContent = error instanceof ApiError ? error.message : 'Ocurrió un error inesperado.';
    showView('error');
  }
}

$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = $('token-input');
  const submit = $('login-submit');
  const token = input.value.trim().replace(/^Bearer\s+/i, '');
  if (!token) {
    setLoginError('Pegá el token para ingresar.');
    input.focus();
    return;
  }
  if (!PRINTABLE.test(token)) {
    setLoginError('El token tiene caracteres que no corresponden. Copialo de nuevo, sin espacios.');
    input.focus();
    return;
  }
  if (submit.getAttribute('aria-disabled') === 'true') return;
  submit.setAttribute('aria-disabled', 'true');
  setLoginError('');
  setToken(token);
  try {
    const profile = await api.me();
    input.value = '';
    $('login-notice').textContent = '';
    enter(profile);
  } catch (error) {
    clearToken();
    setLoginError(error?.status === 401
      ? 'El token no es válido o ya venció.'
      : error instanceof ApiError ? error.message : 'Ocurrió un error inesperado.');
    input.focus();
  } finally {
    submit.removeAttribute('aria-disabled');
  }
});

$('logout').addEventListener('click', () => logout('Cerraste la sesión en esta pestaña.'));
$('error-retry').addEventListener('click', boot);

$('tablist').addEventListener('click', (event) => {
  const tab = event.target.closest('[role="tab"]');
  if (tab) selectTab(tab.dataset.tab);
});

// Patrón de pestañas: flechas, Inicio y Fin mueven el foco y activan la pestaña.
$('tablist').addEventListener('keydown', (event) => {
  const tabs = TABS.map((tab) => $(`tab-${tab.id}`)).filter((button) => !button.hidden);
  const index = tabs.indexOf(document.activeElement);
  if (index < 0) return;
  const targets = {
    ArrowRight: tabs[(index + 1) % tabs.length],
    ArrowLeft: tabs[(index - 1 + tabs.length) % tabs.length],
    Home: tabs[0],
    End: tabs[tabs.length - 1],
  };
  const next = targets[event.key];
  if (!next) return;
  event.preventDefault();
  selectTab(next.dataset.tab, true);
});

boot();
