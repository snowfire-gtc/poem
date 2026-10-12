/* ApiClient + Poller (ui-client.md §3 shared, §5 polling, §6 errors).
   Единая точка выхода в HTTP API: dedup in-flight запросов, фоновая
   очередь повторных попыток с backoff 2/4/8/30 с (максимум 5), пауза
   поллинга на невидимой вкладке. Никаких сторонних источников — все
   запросы идут на origin сервера игры (air-gap, §8). */
(function () {
  'use strict';

  const BASE = '/api/v1';
  const RETRY_DELAYS_MS = [2000, 4000, 8000, 30000, 30000];

  class ApiError extends Error {
    constructor(message, status, detail) {
      super(message);
      this.status = status;
      this.detail = detail;
    }
  }

  class ApiClient {
    constructor() {
      this.token = localStorage.getItem('poem.token') || '';
      this.worldId = localStorage.getItem('poem.world') || 'world';
      this.civId = localStorage.getItem('poem.civ') || '';
      this.online = navigator.onLine !== false;
      this._inflight = new Map();          // key -> Promise (dedup)
      this._queue = [];                    // offline mutation retries
      this._retrying = false;
      this._listeners = { online: [], offline: [], queued: [] };
      window.addEventListener('online', () => { this.online = true; this._flush(); });
      window.addEventListener('offline', () => { this.online = false; this._emit('offline'); });
    }

    /* ---------------- session (localStorage: токен, мир, civ — принцип 1) */
    setSession(s) {
      if ('token' in s) { this.token = s.token || ''; persist('poem.token', this.token); }
      if ('world' in s) { this.worldId = s.world || 'world'; persist('poem.world', this.worldId); }
      if ('civ' in s) { this.civId = s.civ || ''; persist('poem.civ', this.civId); }
    }
    get role() { return this.token ? 'admin' : 'player'; }

    on(kind, fn) { this._listeners[kind].push(fn); }
    _emit(kind, data) { this._listeners[kind].forEach((f) => { try { f(data); } catch (e) {} }); }

    /* ---------------- core request -------------------------------------- */
    async request(method, path, body) {
      const headers = { 'Content-Type': 'application/json' };
      if (this.token) headers['Authorization'] = 'Bearer ' + this.token;
      let resp;
      try {
        resp = await fetch(BASE + path, {
          method,
          headers,
          body: body === undefined ? undefined : JSON.stringify(body),
        });
      } catch (netErr) {
        this.online = false;
        this._emit('offline');
        throw new ApiError('нет связи с сервером', 0, String(netErr));
      }
      let data;
      const text = await resp.text();
      try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { error: text }; }
      if (!resp.ok) {
        this.online = true;
        throw new ApiError(data.error || ('HTTP ' + resp.status),
                           resp.status, data.detail);
      }
      this.online = true;
      return data;
    }

    /* GET with in-flight dedup (§5: один ApiClient, dedup одинаковых запросов) */
    get(path) {
      if (this._inflight.has(path)) return this._inflight.get(path);
      const p = this.request('GET', path).finally(() => this._inflight.delete(path));
      this._inflight.set(path, p);
      return p;
    }
    post(path, body) { return this.request('POST', path, body || {}); }
    put(path, body) { return this.request('PUT', path, body || {}); }
    del(path) { return this.request('DELETE', path, {}); }

    /* Mutating call that survives connection drops: queue + backoff (§6). */
    mutate(method, path, body) {
      return new Promise((resolve, reject) => {
        const job = { method, path, body, attempt: 0, resolve, reject };
        this._queue.push(job);
        this._emit('queued', this._queue.length);
        this._flush();
      });
    }

    async _flush() {
      if (this._retrying || !navigator.onLine) return;
      this._retrying = true;
      try {
        for (const job of [...this._queue]) {
          try {
            const res = await this.request(job.method, job.path, job.body);
            this._queue = this._queue.filter((j) => j !== job);
            this._emit('queued', this._queue.length);
            job.resolve(res);
          } catch (err) {
            if (err.status === 0) {                       // network: retry later
              job.attempt += 1;
              if (job.attempt >= RETRY_DELAYS_MS.length) {
                this._queue = this._queue.filter((j) => j !== job);
                this._emit('queued', this._queue.length);
                job.reject(err);
              } else {
                setTimeout(() => this._flush(), RETRY_DELAYS_MS[job.attempt]);
                break;                                     // wait before next try
              }
            } else {                                       // server verdict: no retry
              this._queue = this._queue.filter((j) => j !== job);
              this._emit('queued', this._queue.length);
              job.reject(err);
            }
          }
        }
      } finally {
        this._retrying = false;
      }
    }

    /* ---------------- typed endpoints (карта §4) ------------------------- */
    chronology() { return this.get(`/worlds/${this.worldId}/chronology`); }
    turnSchedule() { return this.get(`/turn/schedule`); }
    report() { return this.get(`/worlds/${this.worldId}/turns/report`); }
    laws(civId) {
      const q = civId ? `?civilization_id=${encodeURIComponent(civId)}` : '';
      return this.get(`/laws${q}`);
    }
    law(id) { return this.get(`/laws/${encodeURIComponent(id)}`); }
    lawStatus(id) { return this.get(`/laws/${encodeURIComponent(id)}/status`); }
    revisions(id) { return this.get(`/laws/${encodeURIComponent(id)}/revisions`); }
    pending() { return this.get(`/laws/pending`); }
    snapshot() { return this.get(`/laws/snapshot`); }
    drafts(civId) {
      const q = civId ? `?civilization_id=${encodeURIComponent(civId)}` : '';
      return this.get(`/drafts${q}`);
    }
    draft(id) { return this.get(`/drafts/${encodeURIComponent(id)}`); }
    createDraft(body) { return this.mutate('POST', `/laws/drafts`, body); }
    updateDraft(id, body) { return this.mutate('PUT', `/drafts/${encodeURIComponent(id)}`, body); }
    deleteDraft(id) { return this.mutate('DELETE', `/drafts/${encodeURIComponent(id)}`, {}); }
    validateDraft(id) { return this.mutate('POST', `/drafts/${encodeURIComponent(id)}/validate`, {}); }
    createRevision(lawId, body) { return this.mutate('POST', `/laws/${encodeURIComponent(lawId)}/revisions`, body); }
    submitRevision(lawId, revId, body) {
      return this.mutate('POST', `/laws/${encodeURIComponent(lawId)}/revisions/${encodeURIComponent(revId)}/submit`, body || {});
    }
    cancelRevision(lawId, revId) {
      return this.mutate('POST', `/laws/${encodeURIComponent(lawId)}/revisions/${encodeURIComponent(revId)}/cancel`, {});
    }
    costPreview(lawId, body) { return this.post(`/laws/${encodeURIComponent(lawId)}/cost_preview`, body); }
    createLaw(body) { return this.mutate('POST', `/laws`, body); }
    images(status) {
      const q = status ? `?status=${encodeURIComponent(status)}` : '';
      return this.get(`/worlds/${this.worldId}/images${q}`);
    }
    imageMetrics() { return this.get(`/worlds/${this.worldId}/images/metrics`); }
    tokens(query) {
      const q = query ? '?' + new URLSearchParams(query).toString() : '';
      return this.get(`/worlds/${this.worldId}/tokens${q}`);
    }
    createToken(body) { return this.mutate('POST', `/worlds/${this.worldId}/tokens`, body); }
    deleteToken(id) { return this.mutate('DELETE', `/tokens/${encodeURIComponent(id)}`, {}); }
    addTreaty(body) { return this.mutate('POST', `/treaties`, body); }
    declareWar(body) { return this.mutate('POST', `/wars`, body); }
    createCiv(body) { return this.mutate('POST', `/worlds/${this.worldId}/civilizations`, body); }
    openTurn(body) { return this.mutate('POST', `/worlds/${this.worldId}/turns/open`, body); }
    runTurn(body) { return this.mutate('POST', `/worlds/${this.worldId}/turns/run`, body); }

    /* Asset as <img> src: binary route first, JSON base64 envelope fallback. */
    assetUrl(taskId) { return `${BASE}/images/${encodeURIComponent(taskId)}/asset`; }
    async assetDataUrl(taskId) {
      try {
        const resp = await fetch(this.assetUrl(taskId));
        if (!resp.ok) throw new Error(String(resp.status));
        const ctype = resp.headers.get('Content-Type') || '';
        if (ctype.startsWith('image/')) {
          const blob = await resp.blob();
          return await blobToDataUrl(blob);
        }
        const data = await resp.json();       // JSON envelope {asset:{base64,…}}
        const a = data.asset;
        if (!a) throw new Error('no asset');
        return `data:${a.media_type};base64,${a.base64}`;
      } catch (e) {
        throw new ApiError('иллюстрация ещё не готова', 400, String(e));
      }
    }
  }

  function persist(key, value) {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  }

  function blobToDataUrl(blob) {
    return new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve(fr.result);
      fr.onerror = reject;
      fr.readAsDataURL(blob);
    });
  }

  /* ---------------- Poller: интервалы из §5, пауза на скрытой вкладке ---- */
  class Poller {
    constructor(fn, ms, opts) {
      this.fn = fn; this.ms = ms; this.opts = opts || {};
      this.stopped = false; this.timer = null; this.failures = 0;
      document.addEventListener('visibilitychange', () => {
        if (document.hidden) this._pause();
        else this._resume();
      });
    }
    start() { this.stopped = false; this._tick(); return this; }
    stop() { this.stopped = true; if (this.timer) clearTimeout(this.timer); }
    _pause() { if (this.timer) { clearTimeout(this.timer); this.timer = null; } }
    _resume() { if (!this.stopped && !this.timer) this._schedule(0); }
    async _tick() {
      if (this.stopped) return;
      if (this.opts.shouldStop && this.opts.shouldStop()) { this.stop(); return; }
      if (!document.hidden) {
        try { await this.fn(); this.failures = 0; }
        catch (e) { this.failures += 1; }
      }
      if (!this.stopped) {
        // экспоненциальный деград при ошибках (§9: backoff при 5xx)
        const delay = this.ms * Math.pow(2, Math.min(this.failures, 3));
        this._schedule(delay);
      }
    }
    _schedule(ms) {
      this.timer = setTimeout(() => { this.timer = null; this._tick(); }, ms);
    }
  }

  window.poemApi = new ApiClient();
  window.PoemApiError = ApiError;
  window.Poller = Poller;
})();
