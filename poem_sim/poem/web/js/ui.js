/* Общие виды: h-фactory, словари статусов/эпох, toast-хост, форматирование
   игрового времени (принцип 2: годы/сезоны, real-time — только в скобках). */
(function () {
  'use strict';

  /* ------------------------------ dom helper ---------------------------- */
  function h(tag, attrs) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = v;
        else if (k === 'html') el.innerHTML = v;
        else if (k.startsWith('on') && typeof v === 'function')
          el.addEventListener(k.slice(2), v);
        else if (k === 'dataset') Object.assign(el.dataset, v);
        else el.setAttribute(k, v === true ? '' : String(v));
      }
    }
    for (const child of Array.prototype.slice.call(arguments, 2)) {
      if (child === null || child === undefined || child === false) continue;
      el.appendChild(typeof child === 'string' || typeof child === 'number'
        ? document.createTextNode(String(child)) : child);
    }
    return el;
  }
  const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); };

  /* --------------------------- dictionaries ----------------------------- */
  const ERA_LABELS = {
    dawn: 'Заря', ancient: 'Древняя', classical: 'Классическая',
    medieval: 'Средневековье', renaissance: 'Возрождение',
    industrial: 'Индустриальная', modern: 'Современная', atomic: 'Атомная',
    information: 'Информационная', planetary: 'Планетарная',
  };
  // Акцент шапки по эпохе (§7): заря→охра, классическая→пурпур,
  // индустриальная→селена, планетарная→циан (ERA_STYLE_TOKENS).
  const ERA_ACCENTS = {
    dawn: '#c98a2d', ancient: '#a3743f', classical: '#b48ead',
    medieval: '#8a9a5b', renaissance: '#b06a8c', industrial: '#4ba892',
    modern: '#4a90c2', atomic: '#c25a5a', information: '#7a8fd0',
    planetary: '#3fb7c2',
  };
  const SEASON_LABELS = {
    spring: 'весна', summer: 'лето', autumn: 'осень', winter: 'зима',
    long_night: 'долгая ночь',
  };
  const LAW_STATUS_BADGE = {
    active: ['b-active', 'активен'],
    in_transition: ['b-transition', '⏳ переходный период'],
    suspended: ['b-suspended', 'приостановлен'],
    repealed: ['b-repealed', 'отменён'],
  };
  const DRAFT_STATUS_BADGE = {
    Editing: ['b-editing', 'правка'],
    ReadyForPreview: ['b-ready', 'готов к превью'],
    Validated: ['b-validated', 'валиден'],
    Invalid: ['b-invalid', 'невалиден'],
    Submitted: ['b-submitted', 'подан'],
    Discarded: ['b-grey', 'отброшен'],
  };
  const REV_STATUS_BADGE = {
    Pending: ['b-blue', 'ожидает среза'],
    IncludedInSimulation: ['b-teal', 'в снимке хода'],
    Applied: ['b-validated', 'применена'],
    Superseded: ['b-orange', 'перезаписана'],
    Rejected: ['b-red', 'отклонена'],
    Failed: ['b-red', 'сбой применения'],
    Cancelled: ['b-grey', 'отозвана'],
  };
  const ACTIONS = ['AmendLaw', 'RepealLaw', 'SuspendLaw', 'ReinstateLaw',
                   'ClarifyLaw', 'CancelPendingChange'];
  const ACTION_LABELS = {
    AmendLaw: 'Поправить', RepealLaw: 'Отменить', SuspendLaw: 'Приостановить',
    ReinstateLaw: 'Возобновить', ClarifyLaw: 'Уточнить толкование',
    CancelPendingChange: 'Отозвать правку',
  };

  /* ------------------------- game time formats -------------------------- */
  const fmtYears = (y) => {
    if (y === null || y === undefined) return '—';
    const r = Math.round(y * 10) / 10;
    return (Number.isInteger(r) ? r : r.toFixed(1).replace(/\.0$/, '')) + ' лет';
  };
  const fmtGameTs = (iso) => {
    if (!iso) return '—';
    try {
      const d = new Date(iso);
      return d.toLocaleString('ru-RU', { hour: '2-digit', minute: '2-digit' });
    } catch (e) { return iso; }
  };

  /* ------------------------------- badges ------------------------------- */
  function badge(pair, extra) {
    return h('span', { class: 'badge ' + pair[0], text: extra || pair[1] });
  }
  function lawBadge(status) {
    return badge(LAW_STATUS_BADGE[status] || ['b-grey', status]);
  }
  function draftBadge(status) {
    return badge(DRAFT_STATUS_BADGE[status] || ['b-grey', status]);
  }
  function revBadge(rev) {
    const pair = REV_STATUS_BADGE[rev.status] || ['b-grey', rev.status];
    const el = badge(pair);
    if (rev.will_apply_in) {
      // Один источник истины (принцип 4): текст берётся из серверного поля.
      el.title = rev.will_apply_in === 'current_turn'
        ? 'войдёт в текущий срез'
        : 'перенесена в следующий ход — штрафа нет';
    }
    return el;
  }

  /* -------------------------------- toasts ------------------------------ */
  // aria-live=polite: серверные notices доступны скринридерам (§7).
  let liveRegion = null;
  function toast(text, kind, ms) {
    const host = document.getElementById('toasts');
    if (!liveRegion) {
      liveRegion = h('div', { 'aria-live': 'polite', class: 'sr-live' });
      host.appendChild(liveRegion);
    }
    const t = h('div', { class: 'toast' + (kind ? ' ' + kind : ''), text });
    host.appendChild(t);
    liveRegion.appendChild(h('div', { text }));
    setTimeout(() => { t.remove(); }, ms || 6000);
    return t;
  }
  const toastError = (err) => toast(
    err && err.message ? err.message : String(err), 'error');

  /* ------------------------ confirm dialog ------------------------------ */
  function confirmDialog(message) {
    return new Promise((resolve) => {
      const overlay = h('div', { style: 'position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:70;display:flex;align-items:center;justify-content:center' });
      const box = h('div', { class: 'card', style: 'max-width:420px;margin:0 16px' },
        h('div', { text: message, style: 'white-space:pre-wrap;margin-bottom:14px' }),
        h('div', { style: 'display:flex;gap:10px;justify-content:flex-end' },
          h('button', { class: 'secondary', text: 'Отмена', onclick: () => { overlay.remove(); resolve(false); } }),
          h('button', { text: 'Подтвердить', onclick: () => { overlay.remove(); resolve(true); } })));
      overlay.appendChild(box);
      overlay.addEventListener('click', (e) => { if (e.target === overlay) { overlay.remove(); resolve(false); } });
      document.body.appendChild(overlay);
    });
  }

  /* --------------------------- error boundary --------------------------- */
  function renderError(container, err) {
    clear(container);
    container.appendChild(h('div', { class: 'banner offline',
      text: err && err.status === 0 ? 'Нет связи с сервером — данные показаны из памяти, ввод доступен.'
        : 'Ошибка: ' + ((err && err.message) || err) }));
  }

  window.P = {
    h, clear,
    ERA_LABELS, ERA_ACCENTS, SEASON_LABELS, ACTIONS, ACTION_LABELS,
    fmtYears, fmtGameTs,
    badge, lawBadge, draftBadge, revBadge,
    toast, toastError, confirmDialog, renderError,
  };
})();
