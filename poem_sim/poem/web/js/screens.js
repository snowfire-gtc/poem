/* Экраны веб-клиента (ui-client.md §2). Каждый экран — функция
   (contentEl, params) => Promise|void; данные берутся из store
   (WorldProvider в app.js), действия — через poemApi. Клиент тонкий:
   никаких локальных копий законов и ревизий (принцип 1). */
(function () {
  'use strict';
  const { h, clear } = P;
  const api = window.poemApi;

  /* ================================ ДАШБОРД ============================== */
  /* §2.1 / design.md §16.1 — плашка хода из пяти обязательных строк. */
  function DashboardScreen(el) {
    const s = S();
    const ch = s.chronology || {};
    const rep = s.report && s.report.report ? s.report.report.view : null;
    const sched = s.schedule || {};

    const yearsSince = sched.last_report && sched.last_report.years_elapsed != null
      ? sched.last_report.years_elapsed
      : (rep && rep.dashboard ? rep.dashboard.years_since_last_turn : null);

    el.appendChild(h('div', { class: 'card' },
      h('div', { class: 'kv' }, 'Ход:', h('b', String(ch.current_turn ?? '—'))),
      h('div', { class: 'kv' }, 'Эпоха:', h('b', P.ERA_LABELS[ch.current_era] || ch.current_era || '—')),
      h('div', { class: 'kv' }, 'Прошло времени с прошлого хода:',
        h('b', yearsSince == null ? '—' : P.fmtYears(yearsSince))),
      h('div', { class: 'kv' }, 'Текущий год:', h('b', String(ch.current_year ?? '—'))),
      h('div', { class: 'kv' }, 'Следующий ход охватит примерно:',
        h('b', ch.next_turn_span ? P.fmtYears(ch.next_turn_span.years) : '—')),
      h('div', { class: 'kv' }, 'Сезон:',
        h('b', P.SEASON_LABELS[ch.current_season] || ch.current_season || '—'))));

    // Баннер смены интервала (§16.1):span последнего отчёта ≠ текущего.
    if (sched.last_report && sched.last_report.span_years_next != null &&
        ch.next_turn_span && sched.last_report.span_years_next !== ch.next_turn_span.years) {
      el.appendChild(h('div', { class: 'banner warn' },
        `Внимание: благодаря развитию цивилизаций следующий ход охватит ${P.fmtYears(ch.next_turn_span.years)} вместо ${P.fmtYears(sched.last_report.span_years_next)}.`));
    }

    // Таймер среза: wall-clock — только как вспомогательная подпись (§2).
    if (sched.turn_open && sched.simulation_cutoff_at) {
      const cutoff = new Date(sched.simulation_cutoff_at);
      const mins = Math.round((cutoff - Date.now()) / 60000);
      el.appendChild(h('div', { class: 'card' },
        sched.processing
          ? h('div', {}, '⏸ Ход обрабатывается…')
          : h('div', {}, `⏳ Следующий ход ≈ ${ch.next_turn_span ? P.fmtYears(ch.next_turn_span.years) : '—'} · срез хода: ${cutoff.toLocaleString('ru-RU')} (${mins > 0 ? `через ${mins} мин` : 'прошёл'})`),
        h('div', { class: 'muted small' }, sched.note || '')));
    }

    if (!rep) {
      el.appendChild(h('div', { class: 'muted' },
        'Отчёта хода ещё нет — админ может открыть и просимулировать ход в разделе «Админ».'));
      return;
    }

    // Экономика: карточки по цивилизациям.
    el.appendChild(h('h2', {}, 'Экономика'));
    const econ = h('div', { class: 'grid-cards' });
    for (const e of rep.economy || []) {
      econ.appendChild(h('div', { class: 'card' },
        h('b', {}, e.civilization_id),
        h('div', { class: 'kv' }, 'производство', h('b', fmtNum(e.production))),
        h('div', { class: 'kv' }, 'прирост населения', h('b', fmtNum(e.population_growth ?? e.growth))),
        h('div', { class: 'kv' }, 'исследования', h('b', fmtNum(e.research))),
        h('div', { class: 'kv' }, 'население', h('b', fmtNum(e.population)))));
    }
    el.appendChild(econ);

    // Сводка законов.
    const laws = rep.laws || [];
    const active = laws.filter((l) => l.status === 'active').length;
    const transitions = laws.filter((l) => l.status === 'in_transition').length;
    const conflicts = laws.reduce((n, l) => n + (l.conflicts || []).length, 0);
    el.appendChild(h('h2', {}, 'Законы'));
    el.appendChild(h('div', { class: 'card' },
      h('div', { class: 'kv' }, 'активных', h('b', String(active))),
      h('div', { class: 'kv' }, 'переходных периодов', h('b', String(transitions))),
      h('div', { class: 'kv' }, 'конфликтов норм', h('b', String(conflicts)))));

    // Notices — тексты сервера дословно (§16.3, принцип 3).
    el.appendChild(h('h2', {}, 'Уведомления хода'));
    const notices = rep.notices || [];
    if (!notices.length) el.appendChild(h('div', { class: 'muted' }, '—'));
    for (const n of notices) el.appendChild(h('div', { class: 'notice', text: String(n) }));
  }

  const fmtNum = (v) => v == null ? '—' : (Math.round(v * 100) / 100).toLocaleString('ru-RU');

  /* ================================ ХРОНИКА ============================== */
  /* §2.2 — лента событий интервала по игровому году + нелинейная шкала. */
  function ChronicleScreen(el) {
    const s = S();
    const view = s.report && s.report.report ? s.report.report.view : null;
    if (!view) { el.appendChild(h('div', { class: 'muted' }, 'Хроника появится после первого просимулированного хода.')); return; }
    const events = (view.events || []).slice().sort((a, b) => a.year - b.year);

    let kindFilter = '';
    const kinds = [...new Set(events.map((e) => e.kind))];
    const feedEl = h('ul', { class: 'feed' });
    const scaleEl = h('div', { class: 'timeline' });

    function render() {
      clear(feedEl); clear(scaleEl);
      const shown = kindFilter ? events.filter((e) => e.kind === kindFilter) : events;
      const y0 = events.length ? events[0].year : 0;
      const y1 = events.length ? events[events.length - 1].year : 1;
      for (const e of shown) {
        feedEl.appendChild(h('li', { dataset: { year: String(e.year) } },
          h('span', { class: 'year', text: String(Math.round(e.year)) }),
          h('b', { text: e.title }),
          h('span', { class: 'chip', text: e.kind }),
          h('div', { class: 'muted small', text: e.detail || '' })));
        // Нелинейные деления: древние ходы длинные, поздние короткие (§2.2).
        const frac = y1 > y0 ? (e.year - y0) / (y1 - y0) : 0;
        const tick = h('div', { class: 'tick', style: `left:${(frac * 98 + 1).toFixed(2)}%`,
          title: `${Math.round(e.year)}: ${e.title}`,
          onclick: () => {
            const li = feedEl.querySelector(`[data-year="${e.year}"]`);
            if (li) li.scrollIntoView({ behavior: 'smooth', block: 'center' });
            scaleEl.querySelectorAll('.hot').forEach((t) => t.classList.remove('hot'));
            tick.classList.add('hot');
          } });
        scaleEl.appendChild(tick);
      }
      if (!shown.length) feedEl.appendChild(h('li', { class: 'muted', text: 'событий нет' }));
    }

    const filters = h('div', {}, h('button', {
      class: 'secondary' + (kindFilter === '' ? '' : ''), text: 'все', onclick: () => { kindFilter = ''; render(); } }));
    for (const k of kinds) {
      filters.appendChild(h('button', { class: 'secondary', text: k, style: 'margin-left:6px',
        onclick: () => { kindFilter = k; render(); } }));
    }
    el.appendChild(h('div', { class: 'card' }, h('div', { class: 'muted small', text: 'Шкала времени хода (клик — подсветить событие)' }), scaleEl));
    el.appendChild(filters);
    el.appendChild(h('h2', {}, 'События последнего хода'));
    el.appendChild(feedEl);

    // Иллюстрации этого хода (когда done) — ссылка на галерею.
    const imgs = (s.images && s.images.tasks) || [];
    const done = imgs.filter((t) => t.status === 'done');
    el.appendChild(h('h2', {}, 'Иллюстрации'));
    if (!done.length) {
      el.appendChild(h('div', { class: 'muted' },
        `иллюстрации в очереди (${(s.images && s.images.pending) || 0}) — `,
        h('a', { href: '#/gallery', text: 'галерея' })));
    } else {
      const strip = h('div', { class: 'grid-cards' });
      for (const t of done.slice(-4)) strip.appendChild(galleryCard(t, true));
      el.appendChild(strip);
    }
    render();
  }

  /* ============================= СПИСОК ЗАКОНОВ ========================== */
  /* §2.3 — GET /laws?civilization_id, бейджи, традиция, возраст, поиск. */
  function LawsScreen(el) {
    const s = S();
    let query = '', sortBy = 'age';
    const tableWrap = h('div', {});

    function pendingCountFor(lawId) {
      const p = s.pending;
      if (!p) return 0;
      return ['will_apply_this_slice', 'awaiting_next_turn']
        .reduce((n, b) => n + p[b].filter((r) => r.law_id === lawId).length, 0);
    }

    function render() {
      clear(tableWrap);
      let laws = (s.laws && s.laws.laws) || [];
      if (query) laws = laws.filter((l) => (l.title || '').toLowerCase().includes(query.toLowerCase()));
      laws = laws.slice().sort((a, b) => sortBy === 'status'
        ? a.status.localeCompare(b.status) : b.age_years - a.age_years);
      const table = h('table', { class: 'laws' },
        h('tr', {}, h('th', {}, 'Название'), h('th', {}, 'Статус'),
          h('th', {}, 'Традиция'), h('th', {}, 'Возраст'),
          h('th', {}, 'Правки'), h('th', {}, '')));
      for (const l of laws) {
        const trad = Math.max(0, Math.min(1, l.tradition));
        table.appendChild(h('tr', {},
          h('td', {}, h('a', { href: '#/laws/' + encodeURIComponent(l.id), text: l.title || l.id })),
          h('td', {}, P.lawBadge(l.status)),
          h('td', {}, h('div', { class: 'bar tradition', title: 'традиция ' + trad.toFixed(2) },
            h('i', { style: `width:${(trad * 100).toFixed(0)}%` }))),
          h('td', { text: P.fmtYears(l.age_years) }),
          h('td', { text: pendingCountFor(l.id) || '' }),
          h('td', {}, h('a', { href: '#/laws/' + encodeURIComponent(l.id), text: 'открыть' }))));
      }
      if (!laws.length) table.appendChild(h('tr', {}, h('td', { colspan: '6', class: 'muted', text: 'законов нет' })));
      tableWrap.appendChild(table);
    }

    el.appendChild(h('div', { style: 'display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap' },
      h('input', { type: 'text', placeholder: 'поиск по названию…', oninput: (e) => { query = e.target.value; render(); } }),
      h('select', { onchange: (e) => { sortBy = e.target.value; render(); } },
        h('option', { value: 'age', text: 'по возрасту' }),
        h('option', { value: 'status', text: 'по статусу' })),
      h('div', { style: 'flex:1' }),
      h('button', { text: '+ Новый закон', onclick: openNewLawDialog })));
    el.appendChild(tableWrap);
    render();
  }

  /* Диалог создания черновика нового закона (→ редактор, §2.5). */
  async function openNewLawDialog() {
    const civ = S().sessionCiv || api.civId;
    if (!civ) { P.toast('Выберите цивилизацию в шапке', 'error'); return; }
    const ta = h('textarea', { class: 'raw-text', placeholder: 'Текст будущего закона…' });
    const titleIn = h('input', { type: 'text', placeholder: 'Название' });
    const err = h('div', { class: 'inline-error' });
    const overlay = h('div', { style: 'position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:70;display:flex;align-items:center;justify-content:center' },
      h('div', { class: 'card', style: 'max-width:640px;width:90%' },
        h('h1', {}, 'Новый закон (черновик на сервере)'),
        h('label', { class: 'field', text: 'Название' }), titleIn,
        h('label', { class: 'field', text: 'Текст' }), ta, err,
        h('div', { style: 'display:flex;gap:10px;justify-content:flex-end;margin-top:12px' },
          h('button', { class: 'secondary', text: 'Отмена', onclick: () => overlay.remove() }),
          h('button', {
            text: 'Создать черновик',
            onclick: async () => {
              try {
                const res = await api.createDraft({
                  civilization_id: civ, title: titleIn.value, raw_text: ta.value });
                overlay.remove();
                location.hash = '#/drafts/' + res.draft.id;
              } catch (e) { err.textContent = e.message; }
            },
          }),
        ),
      ),
    );
    document.body.appendChild(overlay);
    overlay.addEventListener('click', (ev) => { if (ev.target === overlay) overlay.remove(); });
  }

  /* ============================= КАРТОЧКА ЗАКОНА ========================= */
  /* §2.4 — единый GET /laws/{id} возвращает все секции design.md §16.2. */
  function LawCardScreen(el, params) {
    const id = params[0];
    const detail = S().lawDetail && S().lawDetail[id];
    if (!detail) { el.appendChild(h('div', { class: 'muted', text: 'загрузка…' })); return; }
    const law = detail.law;

    /* --- header + ActionBar --- */
    const head = h('div', { class: 'card' },
      h('div', { style: 'display:flex;gap:12px;align-items:center;flex-wrap:wrap' },
        h('h1', { style: 'margin:0', text: law.title || law.id }), P.lawBadge(law.status)),
      h('div', { class: 'muted small' }, `традиция ${(law.tradition ?? 0).toFixed(2)} · возраст ${P.fmtYears(law.age_years)} · цивилизация ${law.civilization_id}`),
      h('div', { style: 'display:flex;gap:8px;margin-top:10px;flex-wrap:wrap' },
        ...P.ACTIONS.filter((a) => a !== 'CancelPendingChange').map((a) =>
          h('button', { class: 'secondary', text: P.ACTION_LABELS[a],
            onclick: () => openEditForm(id, a) }))));
    el.appendChild(head);

    /* --- текущая версия (пергамент) --- */
    el.appendChild(h('h2', {}, 'Текущая версия'));
    el.appendChild(h('div', { class: 'manuscript', text: law.raw_text || '(пусто)' }));
    el.appendChild(h('div', { class: 'muted small', style: 'margin-top:6px' },
      'Традиция ограничивает резкие противоречащие изменения (§17.6)'));

    /* --- черновики --- */
    el.appendChild(h('h2', {}, 'Черновики'));
    if (!detail.drafts.length) el.appendChild(h('div', { class: 'muted' }, 'нет'));
    for (const d of detail.drafts) {
      el.appendChild(h('div', { class: 'card' },
        h('div', {}, P.draftBadge(d.status), ' ',
          h('a', { href: '#/drafts/' + d.id, text: (d.title || d.id).slice(0, 60) })),
        h('div', { class: 'muted small', text: 'обновлён: ' + d.updated_at })));
    }

    /* --- ожидающие правки: timeline-бейджи из серверных данных (принцип 4) --- */
    el.appendChild(h('h2', {}, 'Ожидающие правки'));
    if (!detail.pending_edits.length) el.appendChild(h('div', { class: 'muted' }, 'нет'));
    for (const r of detail.pending_edits) {
      const cur = r.will_apply_in === 'current_turn';
      el.appendChild(h('div', { class: 'card' },
        h('div', {}, P.revBadge(r), ' ',
          h('b', { text: r.action }),
          cur ? P.badge(['b-blue', 'войдёт в текущий ход'])
              : P.badge(['b-grey', 'перенесена в следующий ход — штрафа нет'])),
        h('div', { class: 'manuscript small', style: 'margin-top:8px', text: r.raw_text }),
        h('div', { class: 'actions', style: 'margin-top:8px' },
          h('button', { class: 'secondary', text: 'Подтвердить',
            onclick: async () => { const res = await api.submitRevision(id, r.id).catch(P.toastError); if (res) { P.toast(res.notice, 'ok'); invalidateLaw(id); } } }),
          h('button', { class: 'danger', text: 'Отозвать',
            onclick: async () => { await api.cancelRevision(id, r.id).catch(P.toastError); invalidateLaw(id); } }))));
    }

    /* --- история версий: diff двух последних --- */
    el.appendChild(h('h2', {}, 'История версий'));
    const hist = detail.version_history || [];
    const diffBox = h('div', { class: 'card' });
    if (hist.length >= 2) diffBox.appendChild(diffView(hist[hist.length - 2].raw_text, hist[hist.length - 1].raw_text));
    for (const r of hist.slice().reverse()) {
      diffBox.appendChild(h('div', { class: 'rev-item' },
        h('span', {}, P.revBadge(r), ' №' + r.sequence_number + ' · ' + r.action),
        h('div', { class: 'muted small', text: (r.raw_text || '').slice(0, 120) })));
    }
    if (!hist.length) diffBox.appendChild(h('div', { class: 'muted' }, 'истории нет'));
    el.appendChild(diffBox);

    /* --- переходные периоды (обратный отсчёт в игровых годах) --- */
    if ((detail.transitions || []).length) {
      el.appendChild(h('h2', {}, 'Переходные периоды'));
      for (const t of detail.transitions) {
        const total = (t.payload && t.payload.total_years) || Math.max(t.remaining_years, 1);
        el.appendChild(h('div', { class: 'card' },
          h('div', {}, t.kind === 'suspension' ? 'Приостановка' : 'Переходный период',
            ' — осталось ', h('b', P.fmtYears(t.remaining_years))),
          h('div', { class: 'bar transition', style: 'margin-top:6px' },
            h('i', { style: `width:${Math.min(100, (t.remaining_years / total) * 100)}%` }))));
      }
    }

    /* --- конфликты норм --- */
    if ((detail.conflicts || []).length) {
      el.appendChild(h('h2', {}, 'Конфликты норм'));
      const chips = h('div', {});
      for (const c of detail.conflicts) {
        chips.appendChild(h('a', { href: '#/laws/' + encodeURIComponent(c.law_b), style: 'text-decoration:none' },
          P.badge(['b-orange', '⚠ ' + (c.title || c.law_b)])));
      }
      el.appendChild(chips);
    }

    /* --- фольклорные эффекты --- */
    if ((detail.folklore_effects || []).length) {
      el.appendChild(h('h2', {}, 'Фольклорные эффекты'));
      const grid = h('div', { class: 'grid-cards' });
      for (const f of detail.folklore_effects) {
        grid.appendChild(miniStat('распространение', f.spread));
        grid.appendChild(miniStat('мутация', f.mutation));
        grid.appendChild(miniStat('конфликтность', f.conflict_factor));
      }
      el.appendChild(grid);
    }

    /* --- образы закона (visual tokens) --- */
    if ((detail.visual_tokens || []).length) {
      el.appendChild(h('h2', {}, 'Образы закона'));
      const row = h('div', {});
      for (const t of detail.visual_tokens) row.appendChild(h('span', { class: 'chip', text: `${t.kind}: ${t.name}` }));
      el.appendChild(row);
    }
  }

  function miniStat(label, value) {
    const v = Math.max(0, Math.min(1, value || 0));
    return h('div', { class: 'card' },
      h('div', { class: 'muted small', text: label }),
      h('b', { text: (value || 0).toFixed(3) }),
      h('div', { class: 'bar', style: 'margin-top:6px' }, h('i', { style: `width:${v * 100}%` })));
  }

  /* Простейший построчный diff двух последних ревизий (§2.4 HistoryDiff). */
  function diffView(a, b) {
    const la = (a || '').split('\n'), lb = (b || '').split('\n');
    const setA = new Set(la), setB = new Set(lb);
    const box = h('div', { style: 'margin-bottom:10px' });
    for (const line of la) if (!setB.has(line)) box.appendChild(h('div', { class: 'diff-line diff-del', text: '- ' + line }));
    for (const line of lb) if (!setA.has(line)) box.appendChild(h('div', { class: 'diff-line diff-add', text: '+ ' + line }));
    return box;
  }

  /* ====================== ФОРМА ПРАВКИ (§2.6, модал) ===================== */
  async function openEditForm(lawId, presetAction) {
    const civ = S().sessionCiv || api.civId;
    const actionSel = h('select', {}, P.ACTIONS.map((a) =>
      h('option', { value: a, text: P.ACTION_LABELS[a], selected: a === presetAction })));
    const textTa = h('textarea', { class: 'raw-text', placeholder: 'Текст правки…' });
    const canonTa = h('textarea', { class: 'json-form', placeholder: '{"kind":"tax","scope":{},"effect":{}} — необязательно' });
    const transYears = h('input', { type: 'number', min: '0', step: '0.5', value: '0' });
    const suspendYears = h('input', { type: 'number', min: '0', step: '1', value: '0' });
    const urgentChk = h('input', { type: 'checkbox' });
    const costBox = h('div', {});
    const err = h('div', { class: 'inline-error' });

    function formBody() {
      const body = {
        civilization_id: civ,
        action: actionSel.value,
        raw_text: textTa.value,
      };
      const cc = {};
      let bad = false;
      if (canonTa.value.trim()) {
        try { cc.new_canonical = JSON.parse(canonTa.value); }
        catch (e) { bad = true; }
      }
      const ty = parseFloat(transYears.value);
      if (ty > 0) cc.transition = { years: ty, seasons: 0, days: 0 };
      const sy = parseFloat(suspendYears.value);
      if (sy > 0) cc.suspend_years = sy;
      cc.urgent = urgentChk.checked;
      body.canonical_change = cc;
      return { body, bad };
    }

    const overlay = h('div', { style: 'position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:70;display:flex;align-items:center;justify-content:center' },
      h('div', { class: 'card', style: 'max-width:720px;width:92%;max-height:90vh;overflow:auto' },
        h('h1', {}, 'Правка закона'),
        h('label', { class: 'field', text: 'Действие (9 типов §3.2)' }), actionSel,
        h('label', { class: 'field', text: 'Текст правки' }), textTa,
        h('details', {}, h('summary', { class: 'muted', text: 'Новая каноническая форма (для опытных)' }), canonTa),
        h('div', { class: 'row' },
          h('div', {}, h('label', { class: 'field', text: 'Переходный период, лет' }), transYears),
          h('div', {}, h('label', { class: 'field', text: 'Приостановка, лет' }), suspendYears),
          h('div', {}, h('label', { class: 'field', text: 'Срочное принятие' }), urgentChk)),
        costBox, err,
        h('div', { style: 'display:flex;gap:10px;justify-content:flex-end;margin-top:12px' },
          h('button', { class: 'secondary', text: 'Предпросмотр стоимости', onclick: async () => {
            const { body, bad } = formBody();
            if (bad) { err.textContent = 'каноническая форма — невалидный JSON'; return; }
            clear(costBox);
            try {
              const pv = await api.costPreview(lawId, {
                action: body.action, new_canonical: body.canonical_change.new_canonical,
                transition_years: parseFloat(transYears.value) || 0,
                urgent: urgentChk.checked, raw_text: body.raw_text });
              costBox.appendChild(h('div', { class: 'banner' },
                h('div', {}, 'Оценка стоимости: ', h('b', fmtNum(pv.estimated_cost)), ' административного ресурса'),
                h('div', {}, 'Конфликтов: ', h('b', String(pv.contradictions))),
                h('div', { class: 'muted small', text: pv.note })));
            } catch (e) { err.textContent = e.message; }
          } }),
          h('button', { class: 'secondary', text: 'Отмена', onclick: () => overlay.remove() }),
          h('button', {
            text: 'Отправить правку',
            onclick: async () => {
              const { body, bad } = formBody();
              if (bad) { err.textContent = 'каноническая форма — невалидный JSON'; return; }
              if (!body.raw_text) { err.textContent = 'текст правки обязателен'; return; }
              try {
                const res = await api.createRevision(lawId, body);
                overlay.remove();
                // Notice сервера печатается дословно (§2.6 п.4).
                P.toast(res.notice, 'ok', 9000);
                invalidateLaw(lawId);
              } catch (e) { err.textContent = e.message; }
            },
          }),
        ),
      ),
    );
    document.body.appendChild(overlay);
    overlay.addEventListener('click', (ev) => { if (ev.target === overlay) overlay.remove(); });
  }

  /* ============================== ЧЕРНОВИКИ ============================== */
  /* §2.5 — серверное хранилище; список. */
  function DraftsScreen(el) {
    const s = S();
    const civ = s.sessionCiv || api.civId;
    const drafts = (s.drafts && s.drafts.drafts) || [];
    el.appendChild(h('div', { style: 'display:flex;gap:10px;margin-bottom:12px' },
      h('button', { text: '+ Новый черновик', onclick: openNewLawDialog })));
    if (!civ) el.appendChild(h('div', { class: 'banner warn', text: 'Выберите цивилизацию в шапке — черновики привязаны к ней.' }));
    for (const d of drafts) {
      el.appendChild(h('div', { class: 'card' },
        h('div', {}, P.draftBadge(d.status), ' ',
          h('a', { href: '#/drafts/' + d.id, text: d.title || d.id })),
        h('div', { class: 'muted small', text: `${d.target_law_id ? 'правка закона ' + d.target_law_id : 'новый закон'} · обновлён ${d.updated_at}` }),
        h('div', { class: 'manuscript small', style: 'margin-top:8px', text: (d.raw_text || '').slice(0, 300) })));
    }
    if (!drafts.length) el.appendChild(h('div', { class: 'muted' }, 'черновиков нет'));
  }

  /* Редактор черновика: две панели, автосохранение debounce 1500 мс. */
  function DraftEditorScreen(el, params) {
    const draftId = params[0];
    const state = { savedAt: null, dirty: false, timer: null, validation: null };
    const indicator = h('span', { class: 'save-indicator', text: '—' });
    const textTa = h('textarea', { class: 'raw-text' });
    const canonTa = h('textarea', { class: 'json-form' });
    const verdictBox = h('div', {});
    const err = h('div', { class: 'inline-error' });

    const submitBtn = h('button', { disabled: true, title: 'сначала «Проверить логику»',
      text: 'Подать', onclick: doSubmit });

    async function load() {
      try {
        const res = await api.draft(draftId);
        const d = res.draft;
        textTa.value = d.raw_text || '';
        canonTa.value = d.canonical_draft ? JSON.stringify(d.canonical_draft, null, 2) : '';
        state.savedAt = d.updated_at;
        setIndicator('saved');
        updateSubmit(d);
        el.dataset.draftStatus = d.status;
      } catch (e) { P.toastError(e); }
    }

    function updateSubmit(d) {
      const ok = (d || { status: el.dataset.draftStatus }).status === 'Validated';
      submitBtn.disabled = !ok;
      submitBtn.title = ok ? '' : 'подача невозможна: сначала «Проверить логику»';
    }

    function setIndicator(mode) {
      indicator.className = 'save-indicator ' + mode;
      indicator.textContent = { saving: 'сохранение…', saved: 'сохранено на сервере',
        offline: 'нет связи — сохранится автоматически' }[mode];
    }

    function scheduleSave() {
      state.dirty = true;
      setIndicator(api.online ? 'saving' : 'offline');
      clearTimeout(state.timer);
      state.timer = setTimeout(doSave, 1500);   // debounce 1500 мс (§2.5)
    }

    async function doSave() {
      if (!state.dirty) return;
      const body = { raw_text: textTa.value };
      if (canonTa.value.trim()) {
        try { body.canonical_draft = JSON.parse(canonTa.value); } catch (e) { /* canonical не обязан быть валидным при автосейве */ }
      }
      try {
        const res = await api.updateDraft(draftId, body);
        state.dirty = false;
        state.savedAt = res.draft.updated_at;
        setIndicator('saved');
      } catch (e) {
        setIndicator(api.online ? 'offline' : 'offline');
        if (e.status !== 0) P.toastError(e);     // сеть: mutate уже в очереди retry
      }
    }

    textTa.addEventListener('input', scheduleSave);
    canonTa.addEventListener('input', scheduleSave);

    const validateBtn = h('button', {
      text: 'Проверить логику', onclick: async () => {
        await doSave();
        clear(verdictBox);
        try {
          const res = await api.validateDraft(draftId);
          state.validation = res.validation;
          const v = res.validation;
          if (res.draft.canonical_draft) canonTa.value = JSON.stringify(res.draft.canonical_draft, null, 2);
          updateSubmit(res.draft);
          verdictBox.appendChild(h('div', { class: 'card' },
            h('div', {}, P.draftBadge(res.draft.status), ' specificity: ', h('b', String(v.specificity))),
            h('div', { class: 'bar', style: 'margin:6px 0' }, h('i', { style: `width:${(v.specificity || 0) * 100}%` })),
            ...(v.errors || []).map((s) => h('div', { class: 'diff-line diff-del', text: '✗ ' + s })),
            ...(v.warnings || []).map((s) => h('div', { class: 'diff-line', text: '⚠ ' + s })),
            v.canonical ? h('pre', { class: 'terminal', text: JSON.stringify(v.canonical, null, 2) }) : null,
            v.status === 'Invalid' ? h('div', { class: 'muted small', text: 'Черновик остаётся редактируемым — можно править текст и проверить снова.' }) : null));
        } catch (e) { P.toastError(e); }
      } });

    async function doSubmit() {
      const targetLaw = el.dataset.targetLaw;
      if (!targetLaw) {
        err.textContent = 'этот черновик создаёт новый закон — подача выполняется через создание закона (админ) или укажите целевой закон';
        return;
      }
      try {
        // SubmitDraft: POST /laws/{law_id}/revisions/{rid}/submit {draft_id}
        // (§3.2/§15.2; submit_revision принимает draft_id по любому rid).
        const res = await api.submitRevision(targetLaw, 'new', { draft_id: draftId });
        P.toast(res.notice, 'ok', 9000);
        invalidateLaw(targetLaw);
        location.hash = '#/laws/' + encodeURIComponent(targetLaw);
      } catch (e) {
        err.textContent = e.message;
        if (/valid|не прошёл|валид/i.test(e.message)) validateBtn.focus();  // фокус на «Проверить логику» (§6)
      }
    }

    el.appendChild(h('div', { style: 'display:flex;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap' },
      indicator, h('div', { style: 'flex:1' }), validateBtn, submitBtn,
      h('button', { class: 'secondary', text: 'Удалить черновик', onclick: async () => {
        if (!(await P.confirmDialog('Удалить серверный черновик?'))) return;
        await api.deleteDraft(draftId).catch(P.toastError);
        location.hash = '#/drafts';
      } })));
    el.appendChild(h('div', { class: 'editor-grid' },
      h('div', {}, h('div', { class: 'muted small', text: 'текст закона (сервер — источник истины)' }), textTa),
      h('div', {}, h('div', { class: 'muted small', text: 'каноническая форма (auto из вердикта, правится вручную)' }), canonTa)));
    el.appendChild(verdictBox);
    el.appendChild(err);

    // Подтянем карточку, чтобы знать target_law_id для подачи.
    api.draft(draftId).then((res) => {
      el.dataset.targetLaw = res.draft.target_law_id || '';
      updateSubmit(res.draft);
    }).catch(() => {});
    load();
  }

  /* ============================ ОЖИДАЮЩИЕ ПРАВКИ ========================= */
  /* §2.7 — пять корзин §15.3 + Failed, и инспектор снимка хода. */
  const BUCKETS = [
    ['will_apply_this_slice', 'Войдут в текущий срез', 'b-blue'],
    ['included_in_current_snapshot', 'Уже в снимке этого хода', 'b-teal'],
    ['awaiting_next_turn', 'Перенесены в следующий ход', 'b-grey', 'штрафа за время суток нет'],
    ['superseded', 'Перезаписаны более поздней версией', 'b-orange'],
    ['rejected', 'Отклонены', 'b-red'],
    ['failed', 'Сбой применения при симуляции', 'b-red'],
  ];

  function PendingScreen(el) {
    const s = S();
    const p = s.pending;
    if (!p) { el.appendChild(h('div', { class: 'muted', text: 'загрузка…' })); return; }
    el.appendChild(h('div', { class: 'muted small', text: p.note || '' }));
    const cols = h('div', { class: 'bucket-columns' });
    for (const [key, title, cls, hint] of BUCKETS) {
      const items = p[key] || [];
      const bucket = h('div', { class: 'bucket' },
        h('h3', {}, P.badge([cls, title]), items.length ? h('span', { class: 'muted small', text: ` (${items.length})` }) : null),
        hint ? h('div', { class: 'muted small', text: hint }) : null);
      for (const r of items) {
        bucket.appendChild(h('div', { class: 'rev-item' },
          h('div', {}, h('a', { href: '#/laws/' + encodeURIComponent(r.law_id), text: r.action }), ' · закон ', h('code', { text: r.law_id.slice(0, 8) })),
          h('div', { class: 'muted small', text: 'получена: ' + (r.received_at || '—') }),
          h('div', { class: 'small', text: (r.raw_text || '').slice(0, 140) }),
          h('div', { class: 'actions' },
            h('a', { href: '#/laws/' + encodeURIComponent(r.law_id), text: 'открыть карточку' }),
            r.status === 'Pending' ? h('a', {
              href: 'javascript:void(0)', text: 'отменить',
              onclick: () => api.cancelRevision(r.law_id, r.id)
                .then(() => refreshPending()).catch(P.toastError) }) : null)));
      }
      if (!items.length) bucket.appendChild(h('div', { class: 'muted small', text: 'пусто' }));
      cols.appendChild(bucket);
    }
    el.appendChild(cols);

    // Секция «Снимок хода» — что читает симуляция прямо сейчас.
    el.appendChild(h('h2', {}, 'Снимок хода'));
    const snap = s.snapshot;
    if (!snap || !snap.civilizations.length) {
      el.appendChild(h('div', { class: 'muted' }, 'снимок ещё не готовился (ход не открывался)'));
    } else {
      el.appendChild(h('pre', { class: 'terminal', text: JSON.stringify(snap, null, 2) }));
      el.appendChild(h('div', { class: 'muted small', text: snap.note || '' }));
    }
  }

  /* =============================== ДИПЛОМАТИЯ ============================ */
  /* §2.8 — договоры/посольства (обратный отсчёт в годах), войны (усталость). */
  function DiplomacyScreen(el) {
    const s = S();
    const view = s.report && s.report.report ? s.report.report.view : null;
    const civ = s.sessionCiv || api.civId;

    el.appendChild(h('h2', {}, 'Активные договоры и посольства'));
    const treaties = (view && view.diplomacy) || [];
    if (!treaties.length) el.appendChild(h('div', { class: 'muted' }, 'нет активных договоров'));
    for (const t of treaties) {
      el.appendChild(h('div', { class: 'card' },
        h('b', { text: t.kind }), ' · ',
        h('span', { text: t.treaty_id ? 'договор' : 'посольство' }),
        t.other_party ? h('span', { class: 'muted', text: ' · сторона: ' + t.other_party }) : null,
        h('div', {}, 'осталось: ', h('b', P.fmtYears(t.remaining_years)))));
    }

    el.appendChild(h('h2', {}, 'Войны'));
    const wars = (view && view.wars) || [];
    if (!wars.length) el.appendChild(h('div', { class: 'muted' }, 'войн нет'));
    for (const w of wars) {
      el.appendChild(h('div', { class: 'card' },
        h('b', { text: w.war_id ? String(w.war_id).slice(0, 8) : '' }),
        Number(w.active) ? P.badge(['b-red', 'идёт']) : P.badge(['b-grey', 'завершена']),
        h('div', { class: 'muted small', text: 'усталость наступавшей' }),
        h('div', { class: 'bar fatigue-a' }, h('i', { style: `width:${(w.fatigue_attacker || 0) * 100}%` })),
        h('div', { class: 'muted small', text: 'усталость оборонявшейся' }),
        h('div', { class: 'bar fatigue-d' }, h('i', { style: `width:${(w.fatigue_defender || 0) * 100}%` }))));
    }

    // Формы заключения (длительность — в игровых годах, §2.8).
    const partyA = h('input', { type: 'text', placeholder: 'цивилизация A', value: civ || '' });
    const partyB = h('input', { type: 'text', placeholder: 'цивилизация B' });
    const dur = h('input', { type: 'number', min: '1', value: '10' });
    const kind = h('select', {}, ['peace', 'trade', 'alliance'].map((k) => h('option', { value: k, text: k })));
    el.appendChild(h('h2', {}, 'Заключить договор'));
    el.appendChild(h('div', { class: 'card' },
      h('div', { class: 'row' }, partyA, partyB,
        h('div', {}, h('label', { class: 'field', text: 'длительность, лет' }), dur), kind,
        h('button', {
          text: 'POST /treaties', onclick: async () => {
            if (!partyA.value || !partyB.value) { P.toast('укажите обе стороны', 'error'); return; }
            try {
              const res = await api.addTreaty({
                world_id: api.worldId, civilization_id: partyA.value,
                kind: kind.value, duration_years: parseFloat(dur.value) });
              P.toast(`договор ${String(res.treaty_id).slice(0, 8)} на ${P.fmtYears(res.duration_years)}`, 'ok');
              refreshReport();
            } catch (e) { P.toastError(e); }
          } }),
      ),
    ));

    const warA = h('input', { type: 'text', placeholder: 'наступатель' });
    const warB = h('input', { type: 'text', placeholder: 'оборонитель' });
    el.appendChild(h('h2', {}, 'Объявить войну'));
    el.appendChild(h('div', { class: 'card' },
      h('div', { class: 'row' }, warA, warB,
        h('button', { class: 'danger', text: 'POST /wars', onclick: async () => {
          try {
            const res = await api.declareWar({
              world_id: api.worldId, attacker_id: warA.value, defender_id: warB.value });
            P.toast('война объявлена (' + String(res.war_id).slice(0, 8) + ')', 'ok');
            refreshReport();
          } catch (e) { P.toastError(e); }
        } }))));
  }

  /* ================================ ГАЛЕРЕЯ ============================== */
  /* §2.9 — очередь задач рендера; картинки только с origin-сервера. */
  const assetCache = new Map();   // task_id -> dataURL (память сессии, не localStorage)

  function galleryCard(task, compact) {
    const slot = h('div', { class: 'slot' });
    const card = h('div', { class: 'task-card' }, slot,
      h('div', { class: 'muted small', text: (task.payload && task.payload.kind) || 'hero' }),
      h('div', { class: 'small', text: (task.prompt || '').slice(0, compact ? 80 : 140) }));
    if (task.status === 'done') {
      if (assetCache.has(task.id)) {
        slot.appendChild(h('img', { src: assetCache.get(task.id), alt: task.prompt || '' }));
      } else {
        slot.innerHTML = '<div class="spinner"></div> загрузка…';
        api.assetDataUrl(task.id).then((url) => {
          assetCache.set(task.id, url);
          clear(slot); slot.appendChild(h('img', { src: url, alt: task.prompt || '' }));
        }).catch(() => { clear(slot); slot.textContent = 'файл недоступен'; });
      }
    } else if (task.status === 'queued') {
      slot.innerHTML = '<div><div class="spinner" style="display:inline-block"></div> ждёт рендера</div>';
      slot.appendChild(h('div', { class: 'muted small', text: task.prompt || '' }));
    } else if (task.status === 'running') {
      slot.innerHTML = '<div><div class="spinner" style="display:inline-block"></div> рисуется на видеокарте сервера</div>';
      if (task.claimed_by) slot.appendChild(h('div', { class: 'muted small', text: 'воркер: ' + task.claimed_by }));
    } else if (task.status === 'failed') {
      slot.appendChild(h('div', { class: 'badge b-red', text: 'ошибка: ' + (task.error || 'сбой рендера') }));
    }
    return card;
  }

  function GalleryScreen(el) {
    const s = S();
    const data = s.images || { tasks: [], pending: 0 };
    el.appendChild(h('div', { class: 'muted small', text:
      `задач в очереди: ${data.pending}; изображения берутся только с этого сервера (air-gap)` }));
    const grid = h('div', { class: 'task-grid' });
    for (const t of data.tasks) grid.appendChild(galleryCard(t));
    if (!data.tasks.length) el.appendChild(h('div', { class: 'muted' },
      'иллюстраций пока нет — они ставятся в очередь при просимулированном ходе'));
    el.appendChild(grid);
  }

  /* ============================== АДМИН =================================== */
  /* §2.10 — метрики очереди, регистрация цивилизаций, цикл хода (стенд). */
  function AdminQueueScreen(el) {
    const m = S().metrics;
    if (!m) { el.appendChild(h('div', { class: 'muted', text: 'загрузка…' })); return; }
    el.appendChild(h('h1', {}, 'Очередь рендеринга'));
    el.appendChild(h('div', { class: 'grid-cards' },
      statCard('глубина queued', m.backlog),
      statCard('running', m.counts.running),
      statCard('done', m.counts.done),
      statCard('failed', m.counts.failed),
      statCard('активных воркеров', m.active_workers),
      statCard('возвраты осиротевших аренд', m.reclaimed_leases),
      statCard('среднее ожидание, с', m.avg_wait_seconds),
      statCard('средний рендер, с', m.avg_render_seconds),
      statCard('доля успеха', m.success_rate == null ? '—' : (m.success_rate * 100).toFixed(0) + '%')));
    el.appendChild(h('h2', {}, 'by_worker'));
    el.appendChild(h('pre', { class: 'terminal', text: JSON.stringify(m.by_worker, null, 2) }));
  }
  const statCard = (label, v) => h('div', { class: 'card' },
    h('div', { class: 'muted small', text: label }), h('b', { text: String(v ?? '—') }));

  function AdminWorldScreen(el) {
    el.appendChild(h('h1', {}, 'Настройка мира'));
    const idIn = h('input', { placeholder: 'id' });
    const nameIn = h('input', { placeholder: 'название' });
    const progIn = h('input', { type: 'number', step: '0.05', value: '0.3' });
    const err = h('div', { class: 'inline-error' });
    el.appendChild(h('div', { class: 'card' },
      h('h2', { style: 'margin-top:0' }, 'Цивилизации'),
      h('div', { class: 'muted small', text: 'эпоха и интервал хода пересчитываются сразу (§9)' }),
      h('div', { class: 'row', style: 'margin-top:8px' }, idIn, nameIn,
        h('div', {}, h('label', { class: 'field', text: 'progress' }), progIn),
        h('button', {
          text: 'Зарегистрировать', onclick: async () => {
            clear(err); 
            try {
              await api.createCiv({ id: idIn.value, name: nameIn.value, progress: parseFloat(progIn.value) });
              P.toast('цивилизация зарегистрирована', 'ok');
              invalidateAll();
            } catch (e) { err.textContent = e.message; }   // повторный id → инлайн (§6)
          } })),
      err));

    const tokName = h('input', { placeholder: 'имя образа (например, «царь Аргон»)' });
    const tokKind = h('select', {}, ['person', 'place', 'artifact', 'painting'].map((k) => h('option', { value: k, text: k })));
    const tokDesc = h('input', { placeholder: 'descriptor для рендереров' });
    el.appendChild(h('div', { class: 'card' },
      h('h2', { style: 'margin-top:0' }, 'Visual tokens (TASK-067)'),
      h('div', { class: 'row' }, tokKind, tokName, tokDesc,
        h('button', { text: 'POST /tokens', onclick: async () => {
          try {
            await api.createToken({ kind: tokKind.value, name: tokName.value, descriptor: tokDesc.value });
            P.toast('образ сохранён в памяти мира', 'ok');
            invalidateTokens();
          } catch (e) { P.toastError(e); }
        } })),
      (() => {
        const box = h('div', {});
        const list = (S().tokens && S().tokens.tokens) || [];
        for (const t of list) box.appendChild(h('span', { class: 'chip' },
          `${t.kind}: ${t.name}`,
          h('a', { href: 'javascript:void(0)', style: 'margin-left:6px;color:#e08a7c', text: '×',
            onclick: () => api.deleteToken(t.id).then(invalidateTokens).catch(P.toastError) })));
        if (!list.length) box.appendChild(h('span', { class: 'muted small', text: 'реестр пуст' }));
        return box;
      })()));

    // Цикл хода (тестовый стенд): open/run с формой расписания.
    const nowIso = () => new Date().toISOString();
    const startedIn = h('input', { value: nowIso() });
    const cutoffIn = h('input', { value: new Date(Date.now() + 3600e3).toISOString() });
    const seedIn = h('input', { type: 'number', value: '7' });
    const runLog = h('pre', { class: 'terminal', style: 'display:none' });
    el.appendChild(h('div', { class: 'card' },
      h('h2', { style: 'margin-top:0' }, 'Цикл хода (стенд)'),
      h('div', { class: 'muted small', text: 'simulation_cutoff_at — техническое расписание; штрафов за время суток нет' }),
      h('div', { class: 'row', style: 'margin-top:8px' },
        h('div', {}, h('label', { class: 'field', text: 'started_at' }), startedIn),
        h('div', {}, h('label', { class: 'field', text: 'simulation_cutoff_at' }), cutoffIn),
        h('div', {}, h('label', { class: 'field', text: 'seed' }), seedIn)),
      h('div', { style: 'display:flex;gap:10px;margin-top:10px' },
        h('button', { class: 'secondary', text: 'turns/open', onclick: async () => {
          try {
            const r = await api.openTurn({ started_at: startedIn.value, simulation_cutoff_at: cutoffIn.value, seed: parseInt(seedIn.value) });
            P.toast('ход открыт: ' + String(r.turn_id).slice(0, 8), 'ok');
            invalidateAll();
          } catch (e) { P.toastError(e); }
        } }),
        h('button', { text: 'turns/run', onclick: async () => {
          try {
            const r = await api.runTurn({ started_at: startedIn.value, simulation_cutoff_at: cutoffIn.value, seed: parseInt(seedIn.value) });
            runLog.style.display = 'block';
            runLog.textContent = JSON.stringify({
              turn_number: r.turn_number, applied: r.applied_revisions,
              deferred: r.deferred_revisions, superseded: r.superseded_revisions,
              notices: r.notices, image_tasks: (r.image_tasks || []).length }, null, 2);
            invalidateAll();
          } catch (e) { P.toastError(e); }
        } })),
      runLog));
  }

  /* ================================ КАРТА ================================ */
  /* #/map — ui-client.md §2.11: карта территорий через призму знаний
     цивилизации игрока. Клиент рисует ТОЛЬКО то, что прислал сервер: свои
     земли подробно, соседи по понятности речи, неведомые — Terra Incognita
     с мифами народа; внизу — куда можно послать экспедицию в этом ходу. */
  const PROJECTION_LABELS = {
    equirectangular: 'Прямая (равнопромежуточная)',
    mercator: 'Меркатор',
    conic: 'Коническая',
  };
  let mapProjection = localStorage.getItem('poem.map.projection') || 'equirectangular';

  function MapScreen(el) {
    const s = S();
    const m = s.map && s.map.map ? s.map.map : null;
    if (!m || !m.civilization_id) {
      el.appendChild(h('h1', {}, '🗺 Карта'));
      el.appendChild(h('div', { class: 'muted' },
        m && !m.civilization_id ? 'в мире нет цивилизаций — карта пуста'
                                : 'Выберите цивилизацию в шапке — карта строится через её призму знаний.'));
      return;
    }
    const head = h('div', { class: 'row', style: 'align-items:center' },
      h('h1', { style: 'margin:0' }, '🗺 Карта '),
      h('span', { class: 'chip', text: `${m.civilization_name} · ход ${m.turn} · ${m.world_year} г.` }),
      m.narrative ? P.badge(['b-orange', '⚠ летописная картина (искажена лестью двора)'])
                  : P.badge(['b-teal', 'канон']),
      h('div', { class: 'spacer' }));
    const projSel = h('select', { title: 'картографическая проекция' });
    for (const p of (m.projections_available || ['equirectangular'])) {
      projSel.appendChild(h('option', { value: p, selected: p === m.projection,
        text: PROJECTION_LABELS[p] || p }));
    }
    projSel.addEventListener('change', () => {
      mapProjection = projSel.value;
      localStorage.setItem('poem.map.projection', mapProjection);
      refreshMap().then(rerender).catch(P.toastError);
    });
    head.appendChild(projSel);
    el.appendChild(head);
    el.appendChild(h('div', { class: 'muted small', text: m.note || '' }));

    /* --- сетка тайлов: x/y нормализуем в 0..9 по известному диапазону --- */
    const tiles = m.tiles || [];
    const xs = tiles.map((t) => t.xy[0]), ys = tiles.map((t) => t.xy[1]);
    const norm = (v, arr) => {
      const lo = Math.min(...arr), hi = Math.max(...arr);
      return hi > lo ? Math.round(((v - lo) / (hi - lo)) * 9) : 4;
    };
    const grid = h('div', { class: 'map-grid card' });
    const byCell = {};
    for (const t of tiles) byCell[`${norm(t.xy[0], xs)}:${norm(t.xy[1], ys)}`] = t;
    for (let gy = 0; gy < 10; gy++) {
      const rowEl = h('div', { class: 'map-row' });
      for (let gx = 0; gx < 10; gx++) {
        const t = byCell[`${gx}:${gy}`];
        let cell;
        if (!t) cell = h('div', { class: 'map-cell unknown terra', title: 'за пределом слова', text: '☁' });
        else if (t.knowing === 'unknown')
          cell = h('div', { class: 'map-cell unknown myth', title: t.myth || '', text: '⁂' });
        else cell = h('div', { class: `map-cell k-${t.knowing}` },
          h('span', { class: 'glyph', text: t.glyph || '•' }),
          h('span', { class: 'place', text: t.place_name || t.lore_name || '?' }),
          t.lore_name ? h('span', { class: 'epithet', text: t.lore_name }) : null);
        if (t) cell.title = [t.place_name || t.lore_name,
                             t.knowing === 'unknown' ? (t.myth || '') : ''].filter(Boolean).join(' · ');
        rowEl.appendChild(cell);
      }
      grid.appendChild(rowEl);
    }
    el.appendChild(grid);

    /* --- легенда уровней познания --- */
    const legend = h('div', { class: 'row' });
    for (const lv of ((m.legend && m.legend.levels) || [])) {
      legend.appendChild(h('span', { class: `legend k-${lv.level}`, title: lv.hint },
        `${lv.label} — ${lv.hint}`));
    }
    el.appendChild(h('div', { class: 'card' }, h('h2', { style: 'margin-top:0' }, 'Рубежи познания'), legend));

    /* --- знания, слухи и мифы --- */
    const knLayer = (m.layers || []).find((l) => l.id === 'knowledge');
    if (knLayer && knLayer.items.length) {
      const ul = h('ul', { class: 'map-lore' });
      for (const it of knLayer.items) {
        ul.appendChild(h('li', { class: 'lore-' + (it.kind || 'note') },
          h('b', { text: { terra_incognita: 'Terra Incognita', rumor: 'слухи',
                           law_measure: 'измерение закона', chronicle: 'хроника' }[it.kind] || it.kind }),
          ' — ', it.text || ''));
      }
      el.appendChild(h('div', { class: 'card' }, h('h2', { style: 'margin-top:0' }, 'Знания, слухи и предания'), ul));
    }

    /* --- куда можно пойти в этом ходу --- */
    const tr = m.travel || {};
    el.appendChild(h('div', { class: 'card' },
      h('h2', { style: 'margin-top:0' }, `Экспедиции этого хода: до ${tr.visits_per_turn ?? '—'} мест`),
      h('div', { class: 'muted small', text: tr.explanation || '' }),
      tableVisits(m.visits && m.visits.options)));
  }

  function tableVisits(options) {
    const tbl = h('table');
    tbl.appendChild(h('tr', {}, h('th', { text: 'место' }), h('th', { text: 'познание' }),
      h('th', { text: 'дни пути' }), h('th', { text: 'посетить' })));
    for (const v of options || []) {
      tbl.appendChild(h('tr', {},
        h('td', { text: v.name }),
        h('td', {}, h('span', { class: `legend k-${v.knowing}`, text: v.knowing === 'known' ? 'изведано' : v.knowing === 'dim' ? 'смутно' : 'неведомо' })),
        h('td', { text: String(v.travel_days) }),
        h('td', {}, v.reachable_this_turn
          ? h('button', { class: 'secondary', text: 'Посетить', onclick: () => visitPlace(v) })
          : h('span', { class: 'muted small', text: v.reason || 'недоступно' }))));
    }
    if (!(options || []).length) return h('div', { class: 'muted', text: 'известных для посещений мест пока нет' });
    return tbl;
  }

  async function visitPlace(v) {
    // Посещение — действие над состоянием мира; сервер ещё не принимает
    // POST /visits, поэтому честно говорим клиенту, что запись появится,
    // когда маршрут будет добавлен (тонкий клиент ничего не симулирует).
    P.toast(`Экспедиция в «${v.name}» (${v.travel_days} дн. пути): приём заявок на посещение будет на сервере`, 'error');
  }

  /* ================================ ЛОГИН ================================ */
  /* #/login — вход в мир: проверка доступности GET chronology (§8). */
  function LoginScreen(el) {
    const worldIn = h('input', { value: api.worldId || 'world' });
    const civIn = h('input', { placeholder: 'ваша цивилизация (необязательно)', value: api.civId });
    const tokIn = h('input', { type: 'password', placeholder: 'админ-токен (для прав; пусто = наблюдатель)' });
    const err = h('div', { class: 'inline-error' });
    el.appendChild(h('div', { class: 'login-hero card' },
      h('h1', {}, 'Поэма — вход в мир'),
      h('div', { class: 'muted', text: 'Клиент тонкий: в браузере хранятся только токен, выбранный мир и UI-настройки.' }),
      h('label', { class: 'field', text: 'Мир (world id)' }), worldIn,
      h('label', { class: 'field', text: 'Цивилизация' }), civIn,
      h('label', { class: 'field', text: 'Токен администратора' }), tokIn, err,
      h('button', {
        style: 'margin-top:14px', text: 'Проверить мир и войти', onclick: async () => {
          clear(err);
          api.setSession({ world: worldIn.value, civ: civIn.value, token: tokIn.value });
          try {
            await api.chronology();               // минимальная проверка доступа (§8)
            enterApp();
          } catch (e) {
            err.textContent = e.status === 0
              ? 'Сервер недоступен: ' + e.message
              : `Мир «${worldIn.value}» не отвечает: ${e.message}`;
          }
        } })));
  }

  window.Screens = {
    DashboardScreen, ChronicleScreen, LawsScreen, LawCardScreen,
    DraftsScreen, DraftEditorScreen, PendingScreen, DiplomacyScreen,
    GalleryScreen, MapScreen, AdminQueueScreen, AdminWorldScreen, LoginScreen,
  };
})();
