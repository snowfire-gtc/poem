/* App: роутер, layout (TopBar/Sidebar/Content), WorldProvider-store и
   единый цикл поллинга (ui-client.md §1, §3, §5). Экраны подписаны на store,
   все запросы идут через один ApiClient с dedup; поллинг замораживается на
   невидимой вкладке (Poller). Клиент тонкий: в памяти — только проекции
   ответов сервера, юридически значимых локальных копий нет (принцип 1). */
(function () {
  'use strict';

  const api = window.poemApi;
  const S_ = () => store;                       // доступ экранов к store
  const Screens = window.Screens;

  /* ------------------------------- store -------------------------------- */
  const store = {
    sessionCiv: api.civId,
    chronology: null, report: null, schedule: null,
    laws: null, pending: null, snapshot: null, drafts: null,
    images: null, metrics: null, tokens: null, map: null,
    lawDetail: {},                              // law_id -> GET /laws/{id}
  };

  let currentRoute = null;                      // {name, params}
  let renderSeq = 0;                            // гонка асинхронных рендеров

  /* ------------------------------ routing ------------------------------- */
  const ROUTES = [
    ['login',      /^\/login$/,                    'LoginScreen'],
    ['dashboard',  /^\/dashboard$/,                'DashboardScreen'],
    ['chronicle',  /^\/chronicle$/,                'ChronicleScreen'],
    ['laws',       /^\/laws$/,                     'LawsScreen'],
    ['law',        /^\/laws\/([^/]+)$/,            'LawCardScreen'],
    ['drafts',     /^\/drafts$/,                   'DraftsScreen'],
    ['draft',      /^\/drafts\/([^/]+)$/,          'DraftEditorScreen'],
    ['pending',    /^\/pending$/,                  'PendingScreen'],
    ['diplomacy',  /^\/diplomacy$/,                'DiplomacyScreen'],
    ['map',        /^\/map$/,                    'MapScreen'],
    ['gallery',    /^\/gallery$/,                  'GalleryScreen'],
    ['adminQueue', /^\/admin\/queue$/,             'AdminQueueScreen'],
    ['adminWorld', /^\/admin\/world$/,             'AdminWorldScreen'],
  ];

  function parseHash() {
    const hash = (location.hash || '#/').slice(1);
    for (const [name, pattern, screen] of ROUTES) {
      const m = pattern.exec(hash);
      if (m) return { name, screen, params: m.slice(1).map(decodeURIComponent) };
    }
    return { name: 'dashboard', screen: 'DashboardScreen', params: [] };
  }

  /* --------------------------- poll intervals (§5) ---------------------- */
  const MS = { chronology: 30000, report: 60000, pending: 20000,
               images: 10000, metrics: 15000 };

  const pollers = [];           // активные поллеры текущего экрана
  let worldPollers = [];        // глобальные: chronology + report
  let lastTurnSeen = null;

  function stopPollers(list) { (list || []).forEach((p) => p.stop()); }

  /* Единый цикл обновления: chronology каждые 30 с; при изменении
     current_turn — немедленный refetch отчёта (§5). */
  async function refreshChronology() {
    store.chronology = await api.chronology();
    if (lastTurnSeen !== null && store.chronology.current_turn !== lastTurnSeen) {
      await refreshReport();
    }
    lastTurnSeen = store.chronology.current_turn;
    applyAccent();
    updateTopbarChips();
  }

  async function refreshReport() {
    store.report = await api.report();
    store.schedule = await api.turnSchedule().catch(() => store.schedule);
  }

  async function refreshLaws() {
    const civ = store.sessionCiv || undefined;
    store.laws = await api.laws(civ);
  }

  async function refreshPendingBundles() {
    store.pending = await api.pending();
    store.snapshot = await api.snapshot();
  }

  async function refreshDrafts() {
    store.drafts = await api.drafts(store.sessionCiv || undefined);
  }

  async function refreshImages() {
    store.images = await api.images();
    const busy = (store.images.tasks || [])
      .some((t) => t.status === 'queued' || t.status === 'running');
    if (!busy) restartScreenPoller('images');   // стоп-условие §5: все done/failed
    return store.images;
  }

  async function refreshMetrics() { store.metrics = await api.imageMetrics(); }
  async function refreshTokens() { store.tokens = await api.tokens(); }

  /* Карта — по запросу экрана (не поллится): серверная призма знаний
     цивилизации + проекция из localStorage клиента (ui-client.md §2.11). */
  window.refreshMap = async function () {
    const proj = localStorage.getItem('poem.map.projection') || 'equirectangular';
    store.map = await api.mapView(store.sessionCiv || undefined, proj);
    return store.map;
  };

  async function refreshLawDetail(id) {
    store.lawDetail[id] = await api.law(id);
  }

  /* Инвалидация после действий пользователя (§5): submit/cancel →
     pending + карточка закона; run turn → report + chronology + gallery. */
  window.invalidateLaw = async function (id) {
    try { await refreshLawDetail(id); } catch (e) {}
    try { await refreshPendingBundles(); } catch (e) {}
    try { await refreshLaws(); } catch (e) {}
    rerender();
  };
  window.refreshPending = async function () {
    try { await refreshPendingBundles(); } catch (e) {}
    rerender();
  };
  window.refreshReport_ = refreshReport;
  window.refreshReport = async function () {
    try { await refreshReport(); } catch (e) {}
    rerender();
  };
  window.invalidateAll = async function () {
    try { await refreshChronology(); } catch (e) {}
    try { await refreshReport(); } catch (e) {}
    try { await refreshLaws(); } catch (e) {}
    try { await refreshImages(); } catch (e) {}
    rerender();
  };
  window.invalidateTokens = async function () {
    try { await refreshTokens(); } catch (e) {}
    rerender();
  };

  /* ------------------------------- layout ------------------------------- */
  const NAV = [
    ['#/dashboard',  '📊', 'Дашборд'],
    ['#/chronicle',  '📜', 'Хроника'],
    ['#/laws',       '⚖', 'Законы'],
    ['#/drafts',     '✍', 'Черновики'],
    ['#/pending',    '🗂', 'Правки'],
    ['#/diplomacy',  '🤝', 'Диплом.'],
    ['#/map',        '🗺', 'Карта'],
    ['#/gallery',    '🖼', 'Галерея'],
  ];
  const ADMIN_NAV = [
    ['#/admin/queue', '⚙', 'Очередь'],
    ['#/admin/world', '🌍', 'Мир'],
  ];

  let shellEl = null, contentEl = null, topbarEls = {};

  function buildShell() {
    const root = document.getElementById('app');
    root.innerHTML = '';
    shellEl = P.h('div', { class: 'shell' });

    /* TopBar: мир ▾ · цивилизация ▾ · Ход N · год · эпоха · срез · профиль */
    const worldIn = P.h('input', { type: 'text', value: api.worldId, title: 'world id' });
    const civSel = P.h('select', { title: 'ваша цивилизация' });
    const turnChip = P.h('span', { class: 'turn-chip', text: '—' });
    const sliceStatus = P.h('span', { class: 'slice-status' });
    const onlineChip = P.h('span', { class: 'chip', text: 'онлайн' });
    const logoutBtn = P.h('button', { class: 'secondary', text: 'Выйти', onclick: () => {
      api.setSession({ token: '' });
      location.hash = '#/login';
    } });
    worldIn.addEventListener('change', () => {
      api.setSession({ world: worldIn.value.trim() || 'world' });
      invalidateAll();
    });
    civSel.addEventListener('change', () => {
      api.setSession({ civ: civSel.value });
      store.sessionCiv = civSel.value;
      invalidateAll();
    });
    topbarEls = { civSel, turnChip, sliceStatus, onlineChip };

    const topbar = P.h('div', { class: 'topbar' },
      P.h('span', { class: 'brand', text: 'ПОЭМА' }),
      worldIn, civSel, turnChip, sliceStatus,
      P.h('div', { class: 'spacer' }), onlineChip, logoutBtn);

    const sidebar = P.h('nav', { class: 'sidebar', 'aria-label': 'Навигация' });
    for (const [href, icon, label] of NAV.concat(api.role === 'admin' ? ADMIN_NAV : [])) {
      sidebar.appendChild(P.h('a', { href, dataset: { nav: href } },
        icon + ' ', P.h('span', { class: 'label', text: label })));
    }
    sidebar.appendChild(P.h('a', { href: '#/login', class: 'muted' },
      '🔑 ', P.h('span', { class: 'label', text: 'Сессия' })));

    contentEl = P.h('main', { class: 'content', id: 'content' });
    shellEl.appendChild(topbar);
    shellEl.appendChild(P.h('div', { class: 'body-grid' }, sidebar, contentEl));
    root.appendChild(shellEl);

    api.on('online', () => { topbarEls.onlineChip.textContent = 'онлайн'; });
    api.on('offline', () => { topbarEls.onlineChip.textContent = 'нет связи'; });
    api.on('queued', (n) => {
      topbarEls.onlineChip.textContent = n > 0 ? `очередь: ${n}` : (api.online ? 'онлайн' : 'нет связи');
    });
  }

  function updateTopbarChips() {
    if (!shellEl) return;
    const ch = store.chronology || {};
    topbarEls.turnChip.textContent =
      `Ход ${ch.current_turn ?? '—'} · ${ch.current_year ?? '—'} г. · ` +
      `${P.SEASON_LABELS[ch.current_season] || ''} · ` +
      `${P.ERA_LABELS[ch.current_era] || ch.current_era || '—'}` +
      (ch.next_turn_span ? ` · ⏳ след. ход ≈ ${P.fmtYears(ch.next_turn_span.years)}` : '');
    const sched = store.schedule || {};
    topbarEls.sliceStatus.classList.toggle('processing', !!sched.processing);
    topbarEls.sliceStatus.textContent = sched.turn_open
      ? (sched.processing ? '⏸ ход обрабатывается'
         : `срез: ${P.fmtGameTs(sched.simulation_cutoff_at)}`)
      : 'ход закрыт';
  }

  /* Акцент шапки по эпохе (§7, ERA_STYLE_TOKENS). */
  function applyAccent() {
    const era = (store.chronology && store.chronology.current_era) || '';
    document.body.dataset.era = era;
    const accent = P.ERA_ACCENTS[era];
    if (accent) document.documentElement.style.setProperty('--accent', accent);
  }

  async function populateCivSelect() {
    // Реестр цивилизаций мира — серверный GET (тонкий клиент: не выводим
    // список из scraping записей законов, а читаем проекцию ответа).
    try {
      const reg = await api.civilizations();
      const civs = reg.civilizations || [];
      const sel = topbarEls.civSel;
      sel.innerHTML = '';
      sel.appendChild(P.h('option', { value: '', text: '(не выбрана)' }));
      for (const c of civs) {
        sel.appendChild(P.h('option', { value: c.id,
          text: c.name ? `${c.name} (${c.id})` : c.id }));
      }
      if (!civs.some((c) => c.id === api.civId) && api.civId) {
        sel.appendChild(P.h('option', { value: api.civId, text: api.civId }));
      }
      sel.value = api.civId || '';
      store.sessionCiv = api.civId || '';
    } catch (e) { /* сети нет — селект заполнится при следующем поллинге */ }
  }

  /* --------------------- per-screen data & pollers ---------------------- */
  const SCREEN_LOADERS = {
    DashboardScreen:  async () => { await Promise.allSettled([refreshChronology(), refreshReport()]); },
    ChronicleScreen:  async () => { await Promise.allSettled([refreshReport(), refreshImages()]); },
    LawsScreen:       async () => { await Promise.allSettled([refreshLaws(), refreshPendingBundles()]); },
    LawCardScreen:    async (params) => { await refreshLawDetail(params[0]); await refreshPendingBundles().catch(() => {}); },
    DraftsScreen:     async () => { await refreshDrafts(); },
    DraftEditorScreen: async () => {},           // сам тянет черновик (editor state)
    PendingScreen:    async () => { await refreshPendingBundles(); },
    DiplomacyScreen:  async () => { await refreshReport(); },
    MapScreen:        async () => { await Promise.allSettled([refreshChronology(), refreshMap()]); },
    GalleryScreen:    async () => { await refreshImages(); },
    AdminQueueScreen: async () => { await refreshMetrics(); },
    AdminWorldScreen: async () => { await Promise.allSettled([refreshChronology(), refreshTokens()]); },
    LoginScreen:      async () => {},
  };

  /* Поллеры текущего экрана по таблице §5. */
  function startScreenPollers(route) {
    stopPollers(pollers); pollers.length = 0;
    const name = route.screen;
    const add = (fn, ms, opts) => pollers.push(new Poller(fn, ms, opts).start());
    if (name === 'PendingScreen') {
      add(refreshPendingBundles, MS.pending, {
        shouldStop: () => currentRoute.name !== 'pending',
      });
    }
    if (name === 'GalleryScreen' || name === 'ChronicleScreen') {
      const p = new Poller(refreshImages, MS.images).start();
      p.__images = true;                       // опознаватель для стоп-условия
      pollers.push(p);
    }
    if (name === 'AdminQueueScreen') {
      add(refreshMetrics, MS.metrics);
    }
    if (name === 'LawCardScreen') {
      const id = route.params[0];
      add(() => refreshLawDetail(id).then(rerender), MS.pending);
    }
    if (name === 'DraftsScreen') {
      add(refreshDrafts, MS.pending);
    }
  }

  function restartScreenPoller(kind) {
    // Стоп-условие (§5): изображения все done/failed — поллер очереди гасится.
    if (kind === 'images') stopPollers(pollers.filter((p) => p.__images));
  }

  /* ------------------------------- render ------------------------------- */
  function highlightNav() {
    if (!shellEl) return;
    const target = '#' + (location.hash.slice(1) || '/dashboard');
    shellEl.querySelectorAll('[data-nav]').forEach((a) => {
      a.classList.toggle('active', target.startsWith(a.dataset.nav.replace('#', '#')));
    });
    shellEl.querySelectorAll('.sidebar a.active').forEach((a) => {
      a.classList.toggle('active', a.getAttribute('href') === target ||
        (a.getAttribute('href') === '#/laws' && target.startsWith('#/laws/')) ||
        (a.getAttribute('href') === '#/drafts' && target.startsWith('#/drafts/')));
    });
  }

  async function renderRoute() {
    const seq = ++renderSeq;
    const route = parseHash();
    currentRoute = route;

    if (route.name === 'login') {
      stopPollers(worldPollers); worldPollers = [];
      stopPollers(pollers); pollers.length = 0;
      contentEl = contentEl || document.createElement('div');
      const host = document.getElementById('app');
      if (shellEl && host.contains(shellEl)) host.removeChild(shellEl);
      const wrap = P.h('div', { class: 'content' });
      host.appendChild(wrap);
      Screens.LoginScreen(wrap);
      window.enterApp = enterApp;
      return;
    }

    if (!shellEl) buildShell();
    if (!host_has_shell()) { document.getElementById('app').appendChild(shellEl); }
    window.enterApp = enterApp;

    const loader = SCREEN_LOADERS[route.screen];
    try { await loader(route.params); } catch (e) { /* покажем что есть */ }
    if (seq !== renderSeq) return;                 // новая навигация выиграла гонку

    clear_(contentEl);
    try {
      Screens[route.screen](contentEl, route.params);
    } catch (e) {
      P.renderError(contentEl, e);
    }
    highlightNav();
    startScreenPollers(route);
    updateTopbarChips();
  }

  function host_has_shell() {
    return document.getElementById('app').contains(shellEl);
  }
  function clear_(el) { while (el.firstChild) el.removeChild(el.firstChild); }

  function rerender() {
    if (currentRoute) renderRoute();
  }
  window.rerender = rerender;

  /* ----------------------------- app entry ------------------------------ */
  function startWorldPolling() {
    stopPollers(worldPollers); worldPollers = [];
    worldPollers.push(new Poller(refreshChronology, MS.chronology).start());
    worldPollers.push(new Poller(refreshReport, MS.report).start());
    worldPollers.push(new Poller(populateCivSelect, MS.report).start());
  }

  function enterApp() {
    store.sessionCiv = api.civId;
    // Роль — по проверке учётных данных на сервере (§15), затем перестроить
    // сайдбар (админ-разделы появляются только для admin).
    api.probe().then(() => { if (shellEl) buildShell(); }).catch(() => {});
    if ((location.hash || '#/').startsWith('#/login') || !location.hash) {
      location.hash = '#/dashboard';
    } else {
      renderRoute();
    }
    startWorldPolling();
  }
  window.enterApp = enterApp;
  window.S = S_;

  window.addEventListener('hashchange', () => {
    if ((location.hash || '') === '#/login') renderRoute();
    else if (!shellEl && !location.hash.startsWith('#/login')) { /* login ниже */ }
    renderRoute();
  });

  /* Старт: если токен/мир уже в localStorage и мир отвечает — вход сразу,
     иначе — экран логина (guide_client.md §4.1 проверка доступа). */
  (async function boot() {
    try {
      if (api.token !== undefined) {
        await api.chronology();                   // минимальная проверка (§8)
        enterApp();
        return;
      }
    } catch (e) { /* сервер недоступен или мир не выбран */ }
    location.hash = '#/login';
    renderRoute();
  })();
})();
