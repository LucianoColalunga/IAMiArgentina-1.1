// Long polling de respuestas: un único ciclo activo, cancelable, con espera creciente ante errores.

const RETRY_FIRST = 1000;
const RETRY_MAX = 30_000;
const RETRY_AFTER_MAX = 5 * 60_000;
const MIN_GAP = 1000; // si el servidor contesta vacío enseguida, no se lo vuelve a llamar al instante
const HIDDEN_LIMIT = 5 * 60_000;

export class Poller {
  #session;
  #onMessages;
  #onExpired;
  #controller = null;
  #wake = null;
  #hiddenSince = null;

  constructor(session, { onMessages, onExpired }) {
    this.#session = session;
    this.#onMessages = onMessages;
    this.#onExpired = onExpired;
    if (document.hidden) this.#hiddenSince = Date.now();
    document.addEventListener('visibilitychange', () => this.#visibilityChanged());
    window.addEventListener('online', () => this.#wake?.());
  }

  start() {
    if (this.#controller || !this.#session.token) return;
    const controller = new AbortController();
    this.#controller = controller;
    this.#loop(controller.signal).finally(() => {
      if (this.#controller === controller) this.#controller = null;
    });
  }

  stop() {
    this.#controller?.abort();
    this.#controller = null;
  }

  restart() {
    this.stop();
    this.start();
  }

  #visibilityChanged() {
    if (document.hidden) {
      this.#hiddenSince = Date.now();
      return;
    }
    this.#hiddenSince = null;
    // Al volver a la pestaña: si el ciclo estaba esperando para reintentar, se adelanta; si se había
    // pausado, arranca de nuevo.
    if (this.#controller) this.#wake?.();
    else this.start();
  }

  #hiddenTooLong() {
    return this.#hiddenSince !== null && Date.now() - this.#hiddenSince > HIDDEN_LIMIT;
  }

  async #loop(signal) {
    let retry = RETRY_FIRST;
    // Termina al cancelarlo, al perder la sesión o después de 5 minutos con la pestaña oculta.
    while (!signal.aborted && !this.#hiddenTooLong()) {
      const token = this.#session.token;
      if (!token) return;
      const started = Date.now();
      let messages;
      try {
        messages = await this.#session.poll(token, this.#session.lastSeq, signal);
      } catch (error) {
        if (signal.aborted) return;
        if (error.status === 401) {
          if (token !== this.#session.token) continue; // la sesión cambió mientras esperábamos
          this.#onExpired();
          return;
        }
        const hinted = Math.min((error.retryAfter || 0) * 1000, RETRY_AFTER_MAX);
        await this.#sleep(Math.max(retry, hinted), signal);
        retry = Math.min(retry * 2, RETRY_MAX);
        continue;
      }
      if (signal.aborted) return;
      retry = RETRY_FIRST;
      if (token !== this.#session.token) continue;
      if (messages.length) {
        this.#onMessages(messages);
        continue;
      }
      const elapsed = Date.now() - started;
      if (elapsed < MIN_GAP) await this.#sleep(MIN_GAP - elapsed, signal);
    }
  }

  // Espera cancelable: termina antes si se aborta el ciclo o si alguien llama a #wake.
  #sleep(ms, signal) {
    return new Promise((resolve) => {
      const done = () => {
        clearTimeout(timer);
        signal.removeEventListener('abort', done);
        if (this.#wake === done) this.#wake = null;
        resolve();
      };
      const timer = setTimeout(done, ms);
      signal.addEventListener('abort', done);
      this.#wake = done;
    });
  }
}
