'use strict';

/* Клиент интерфейса заявок. Без сборки и внешних зависимостей —
   один файл, который можно прочитать целиком. */

const STATUS = {
  queued:       { label: 'В очереди',           tone: '',        step: 0 },
  working:      { label: 'В работе',            tone: 'active',  step: 1 },
  needs_input:  { label: 'Нужен ваш ответ',     tone: 'warn',    step: 1 },
  checking:     { label: 'Идёт проверка',       tone: 'active',  step: 2 },
  tests_failed: { label: 'Проверка не прошла',  tone: 'danger',  step: 2 },
  review:       { label: 'Ждёт подтверждения',  tone: 'warn',    step: 3 },
  merging:      { label: 'Применяю',            tone: 'active',  step: 4 },
  deploying:    { label: 'Выкатываю на сайт',   tone: 'active',  step: 4 },
  done:         { label: 'Готово',              tone: 'success', step: 5 },
  no_changes:   { label: 'Без изменений',       tone: '',        step: 5 },
  failed:       { label: 'Ошибка',              tone: 'danger',  step: -1 },
  cancelled:    { label: 'Отменена',            tone: '',        step: -1 },
};

const STEPS = ['Принята', 'В работе', 'Проверка', 'Подтверждение', 'Выкатка', 'Готово'];
const ATTENTION = new Set(['needs_input', 'review', 'done', 'failed', 'tests_failed']);

const state = {
  token: '',
  me: null,
  requests: [],
  selectedId: null,
  events: [],
};

/* ---------- вспомогательное ---------- */

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined && value !== false) node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child) node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}

function when(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  const diff = (Date.now() - date.getTime()) / 1000;
  if (diff < 60) return 'только что';
  if (diff < 3600) return `${Math.floor(diff / 60)} мин назад`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} ч назад`;
  return date.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' });
}

function clockTime(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}

function statusOf(request) {
  return STATUS[request.status] || { label: request.status, tone: '', step: 0 };
}

function pill(request) {
  const meta = statusOf(request);
  const busy = meta.tone === 'active';
  return el('span', { class: `pill ${meta.tone}` }, [
    busy ? el('span', { class: 'dot' }) : null,
    meta.label,
  ]);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${state.token}`,
      ...(options.headers || {}),
    },
  });
  if (response.status === 401) {
    localStorage.removeItem('gateway_token');
    showGate('Ссылка больше не действует. Попросите новую у администратора.');
    throw new Error('unauthorized');
  }
  const payload = response.status === 204 ? {} : await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Ошибка ${response.status}`);
  return payload;
}

function showGate(message) {
  document.getElementById('shell').hidden = true;
  document.getElementById('gate').hidden = false;
  document.getElementById('gateError').textContent = message || '';
}

/* ---------- список ---------- */

function renderList() {
  const list = document.getElementById('list');
  list.textContent = '';
  document.getElementById('listEmpty').hidden = state.requests.length > 0;
  document.getElementById('listCounter').textContent =
    state.requests.length ? `${state.requests.length}` : '';

  for (const request of state.requests) {
    const title = request.title || request.body.split('\n')[0];
    const card = el('button', {
      class: 'card',
      type: 'button',
      'aria-current': String(request.id === state.selectedId),
      onclick: () => select(request.id),
    }, [
      el('span', { class: 'card-title', text: title }),
      el('span', { class: 'card-foot' }, [
        pill(request),
        el('span', {
          class: 'card-date',
          text: (state.me.role === 'admin' ? `${request.author} · ` : '') + when(request.created_at),
        }),
      ]),
    ]);
    list.appendChild(card);
  }
}

/* ---------- деталь ---------- */

function renderSteps(request) {
  const meta = statusOf(request);
  const failed = meta.step === -1;
  return el('div', { class: 'steps' }, STEPS.map((label, index) => {
    let cls = 'step';
    if (failed) cls += index === 0 ? ' failed' : '';
    else if (index < meta.step) cls += ' done';
    else if (index === meta.step) cls += ' current';
    return el('div', { class: cls }, [el('div', { class: 'bar' }), label]);
  }));
}

function renderChanges(request) {
  if (!request.text_changes || !request.text_changes.length) return null;
  return el('div', { class: 'section' }, [
    el('h3', { text: 'Что изменилось в тексте' }),
    el('div', { class: 'changes' }, request.text_changes.slice(0, 12).map((change) =>
      el('div', { class: 'change' }, [
        el('div', { class: 'file', text: change.file }),
        change.before ? el('div', { class: 'before', text: change.before }) : null,
        change.after ? el('div', { class: 'after', text: change.after }) : null,
      ])
    )),
    request.files && request.files.length
      ? el('details', { class: 'files' }, [
          el('summary', { text: `Затронуто файлов: ${request.files.length}` }),
          el('ul', {}, request.files.map((file) => el('li', { text: file }))),
        ])
      : null,
  ]);
}

function renderSummary(request) {
  if (!request.summary && !(request.user_visible || []).length) return null;
  return el('div', { class: 'section' }, [
    el('h3', { text: 'Что сделано' }),
    request.summary ? el('p', { class: 'summary', text: request.summary }) : null,
    (request.user_visible || []).length
      ? el('ul', { class: 'bullets' }, request.user_visible.map((item) => el('li', { text: String(item) })))
      : null,
    request.notes ? el('p', { class: 'hint', text: `Важно: ${request.notes}` }) : null,
  ]);
}

function renderAction(request) {
  const status = request.status;

  if (status === 'needs_input') {
    const field = el('textarea', { placeholder: 'Ваш ответ' });
    const send = el('button', {
      class: 'primary',
      text: 'Ответить',
      onclick: async () => {
        if (!field.value.trim()) return;
        send.disabled = true;
        await api(`/api/requests/${request.id}/answer`, {
          method: 'POST',
          body: JSON.stringify({ text: field.value.trim() }),
        }).catch((error) => alert(error.message));
        field.value = '';
        send.disabled = false;
      },
    });
    return el('div', { class: 'section' }, [
      el('div', { class: 'callout attention' }, [
        el('p', { html: `<strong>Вопрос по заявке:</strong> ${escapeHtml(request.question || '')}` }),
        field,
        el('div', { class: 'row' }, [send]),
      ]),
    ]);
  }

  if (status === 'review') {
    const approve = el('button', {
      class: 'primary wide',
      text: 'Выкатить на сайт',
      onclick: async () => {
        approve.disabled = true;
        approve.textContent = 'Выкатываю…';
        await api(`/api/requests/${request.id}/approve`, { method: 'POST' })
          .catch((error) => { alert(error.message); approve.disabled = false; approve.textContent = 'Выкатить на сайт'; });
      },
    });
    return el('div', { class: 'section' }, [
      el('div', { class: 'callout success' }, [
        el('p', { text: 'Правка готова и прошла автоматическую проверку. После подтверждения она появится на сайте в течение пары минут.' }),
        approve,
        el('p', { class: 'hint', text: 'Увидеть результат можно будет уже на самом сайте. Если что-то окажется не так — отправьте заявку «верни как было», это делается так же быстро.' }),
      ]),
      el('div', { class: 'actions' }, [
        el('button', {
          class: 'secondary',
          text: 'Отменить заявку',
          onclick: () => cancelRequest(request.id),
        }),
      ]),
    ]);
  }

  if (status === 'done') {
    return el('div', { class: 'section' }, [
      el('div', { class: 'callout success' }, [
        el('p', { text: 'Готово — правка на сайте.' }),
        state.me.project.site
          ? el('div', { class: 'row' }, [
              el('a', { class: 'primary', href: state.me.project.site, target: '_blank', rel: 'noopener', text: 'Открыть сайт' }),
            ])
          : null,
      ]),
    ]);
  }

  if (status === 'failed' || status === 'tests_failed' || status === 'no_changes') {
    const tone = status === 'no_changes' ? '' : 'danger';
    return el('div', { class: 'section' }, [
      el('div', { class: `callout ${tone}` }, [
        el('p', { text: request.error || request.summary || 'Заявка завершилась без изменений.' }),
        request.checks_detail ? el('p', { class: 'hint', text: request.checks_detail }) : null,
        el('div', { class: 'row' }, [
          el('button', {
            class: 'secondary',
            text: 'Отправить заново',
            onclick: async () => {
              const created = await api(`/api/requests/${request.id}/retry`, { method: 'POST' })
                .catch((error) => alert(error.message));
              if (created && created.id) { await refresh(); select(created.id); }
            },
          }),
        ]),
      ]),
    ]);
  }

  if (['queued', 'working', 'checking', 'merging', 'deploying'].includes(status)) {
    return el('div', { class: 'actions' }, [
      el('button', { class: 'secondary', text: 'Отменить заявку', onclick: () => cancelRequest(request.id) }),
    ]);
  }
  return null;
}

function renderFeed() {
  if (!state.events.length) return null;
  const items = state.events.slice(-80).map((event) =>
    el('div', { class: `feed-item ${event.kind}` }, [
      el('span', { class: 'feed-time', text: clockTime(event.ts) }),
      el('span', { class: 'feed-text', text: event.text }),
    ])
  );
  return el('div', { class: 'section' }, [
    el('h3', { text: 'Ход работы' }),
    el('div', { class: 'feed', id: 'feed' }, items),
  ]);
}

function renderDetail() {
  const pane = document.getElementById('detail');
  const request = state.requests.find((item) => item.id === state.selectedId);
  pane.textContent = '';
  if (!request) {
    document.body.classList.remove('detail-open');
    pane.appendChild(el('div', { class: 'placeholder' }, [
      el('div', { class: 'placeholder-mark', text: '✳' }),
      el('p', { text: 'Выберите заявку слева, чтобы посмотреть, что происходит.' }),
    ]));
    return;
  }

  pane.appendChild(el('div', { class: 'detail-head' }, [
    el('button', {
      class: 'ghost-link detail-back', text: '← К списку',
      onclick: () => { state.selectedId = null; renderList(); renderDetail(); },
    }),
    el('h2', { class: 'detail-title', text: request.title || request.body.split('\n')[0] }),
    el('div', { class: 'detail-meta' }, [
      pill(request),
      el('span', { text: `Заявка №${request.id}` }),
      el('span', { text: when(request.created_at) }),
      state.me.role === 'admin' ? el('span', { text: request.author }) : null,
      state.me.role === 'admin' && request.pr_url
        ? el('a', { class: 'ghost-link', href: request.pr_url, target: '_blank', rel: 'noopener', text: 'PR на GitHub' })
        : null,
    ]),
  ]));

  const steps = renderSteps(request);
  pane.appendChild(steps);
  const current = steps.querySelector('.step.current');
  if (current) current.scrollIntoView({ block: 'nearest', inline: 'center' });
  pane.appendChild(el('div', { class: 'section' }, [
    el('h3', { text: 'Просьба' }),
    el('div', { class: 'quote', text: request.body }),
  ]));

  const action = renderAction(request);
  if (action) pane.appendChild(action);

  const summary = renderSummary(request);
  if (summary) pane.appendChild(summary);

  const changes = renderChanges(request);
  if (changes) pane.appendChild(changes);

  const feed = renderFeed();
  if (feed) pane.appendChild(feed);

  const feedBox = document.getElementById('feed');
  if (feedBox) feedBox.scrollTop = feedBox.scrollHeight;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
}

/* ---------- действия ---------- */

async function cancelRequest(id) {
  if (!confirm('Отменить заявку? Правка не попадёт на сайт.')) return;
  await api(`/api/requests/${id}/cancel`, { method: 'POST' }).catch((error) => alert(error.message));
  await refresh();
}

async function select(id) {
  state.selectedId = id;
  document.body.classList.add('detail-open');
  renderList();
  const data = await api(`/api/requests/${id}`).catch(() => null);
  if (data) {
    state.events = data.events;
    upsert(data.request);
  }
  renderDetail();
}

function upsert(request) {
  const index = state.requests.findIndex((item) => item.id === request.id);
  if (index === -1) state.requests.unshift(request);
  else state.requests[index] = { ...state.requests[index], ...request };
}

async function refresh() {
  const data = await api('/api/requests');
  state.requests = data.requests;
  renderList();
  renderDetail();
}

/* ---------- живые обновления ---------- */

async function listen() {
  while (true) {
    try {
      const response = await fetch('/api/stream', { headers: { Authorization: `Bearer ${state.token}` } });
      if (!response.ok || !response.body) throw new Error('stream');
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const chunks = buffer.split('\n\n');
        buffer = chunks.pop() || '';
        for (const chunk of chunks) {
          const line = chunk.split('\n').find((item) => item.startsWith('data: '));
          if (line) handleEvent(JSON.parse(line.slice(6)));
        }
      }
    } catch (error) {
      /* обрыв связи — переподключаемся */
    }
    await new Promise((resolve) => setTimeout(resolve, 3000));
  }
}

function handleEvent(payload) {
  if (payload.type === 'request') {
    const previous = state.requests.find((item) => item.id === payload.request.id);
    const changed = !previous || previous.status !== payload.request.status;
    upsert(payload.request);
    renderList();
    if (payload.request.id === state.selectedId) renderDetail();
    if (changed && ATTENTION.has(payload.request.status)) notify(payload.request);
  } else if (payload.type === 'event') {
    if (payload.request_id === state.selectedId) {
      state.events.push(payload);
      renderDetail();
    }
  }
}

function notify(request) {
  const title = request.title || request.body.split('\n')[0];
  const text = `${statusOf(request).label}: ${title}`;
  document.title = `● ${state.me.brand.name}`;
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  if (document.visibilityState === 'visible') return;
  new Notification(state.me.brand.name, { body: text });
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && state.me) document.title = state.me.brand.name;
});

/* ---------- админка ---------- */

async function openAdmin() {
  const dialog = document.getElementById('adminDialog');
  const body = document.getElementById('adminBody');
  body.textContent = 'Загружаю…';
  dialog.showModal();
  const data = await api('/api/admin/overview').catch((error) => ({ error: error.message }));
  body.textContent = '';

  if (data.error) {
    body.appendChild(el('div', { class: 'problems' }, [
      el('strong', { text: 'Не удалось получить состояние: ' }), String(data.error),
    ]));
  }

  if (data.config_problems && data.config_problems.length) {
    body.appendChild(el('div', { class: 'problems' }, [
      el('strong', { text: 'Не хватает настроек:' }),
      el('ul', {}, data.config_problems.map((item) => el('li', { text: item }))),
    ]));
  }

  const rows = [
    ['Репозиторий', data.github && data.github.ok ? `${data.github.repo} (${data.github.can_push ? 'есть право записи' : 'НЕТ права записи'})` : (data.github && data.github.error) || '—'],
    ['Основная ветка', data.repo && data.repo.ok ? `${data.repo.base} · ${data.repo.head} · ${data.repo.last_commit}` : (data.repo && data.repo.error) || '—'],
    ['Песочница агента', data.sandbox ? `${data.sandbox.mode}, сеть ${data.sandbox.network ? 'включена' : 'выключена'}, модель ${data.sandbox.model}` : '—'],
    ['Слежение за выкаткой', { dokploy: 'через Dokploy API', healthcheck: 'по health-адресу сайта', none: 'не настроено' }[data.deploy_mode] || '—'],
    ['Параллельных заявок', data.runtime ? data.runtime.max_concurrent : '—'],
    ['Локальная копия', data.runtime && data.runtime.repo_ready ? 'готова' : (data.runtime && data.runtime.repo_error) || 'готовится'],
  ];
  const kv = el('dl', { class: 'kv' });
  for (const [key, value] of rows) {
    kv.appendChild(el('dt', { text: key }));
    kv.appendChild(el('dd', { text: String(value) }));
  }
  body.appendChild(kv);

  if (data.access_links) {
    const box = el('div', {}, [el('h3', { text: 'Персональные ссылки' })]);
    for (const person of data.access_links) {
      box.appendChild(el('div', { class: 'link-row' }, [
        el('span', { text: `${person.display_name}${person.role === 'admin' ? ' (админ)' : ''}` }),
        el('code', { text: person.link }),
        el('button', {
          class: 'ghost-link', text: 'Копировать',
          onclick: (event) => {
            navigator.clipboard.writeText(person.link);
            event.target.textContent = 'Скопировано';
          },
        }),
      ]));
    }
    body.appendChild(box);
  }
}

/* ---------- запуск ---------- */

async function boot() {
  const url = new URL(window.location.href);
  const fromLink = url.searchParams.get('k');
  if (fromLink) {
    localStorage.setItem('gateway_token', fromLink);
    url.searchParams.delete('k');
    window.history.replaceState({}, '', url.pathname + url.search);
  }
  state.token = localStorage.getItem('gateway_token') || '';
  if (!state.token) return showGate('');

  try {
    state.me = await api('/api/me');
  } catch (error) {
    return;
  }

  document.documentElement.style.setProperty('--accent', state.me.brand.accent);
  document.getElementById('brandName').textContent = state.me.brand.name;
  document.getElementById('brandSubtitle').textContent = state.me.brand.subtitle;
  document.getElementById('whoami').textContent = state.me.display_name;
  document.title = state.me.brand.name;
  if (state.me.project.site) {
    const link = document.getElementById('siteLink');
    link.href = state.me.project.site;
    link.hidden = false;
  }
  if (state.me.role === 'admin') {
    const button = document.getElementById('adminBtn');
    button.hidden = false;
    button.onclick = openAdmin;
    document.getElementById('adminClose').onclick = () => document.getElementById('adminDialog').close();
  }

  document.getElementById('gate').hidden = true;
  document.getElementById('shell').hidden = false;

  document.getElementById('newForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const field = document.getElementById('newBody');
    const button = document.getElementById('submitBtn');
    const text = field.value.trim();
    if (text.length < 5) return;
    button.disabled = true;
    try {
      const created = await api('/api/requests', { method: 'POST', body: JSON.stringify({ body: text }) });
      field.value = '';
      upsert(created);
      renderList();
      select(created.id);
      if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission();
    } catch (error) {
      alert(error.message);
    } finally {
      button.disabled = false;
    }
  });

  await refresh();
  listen();
  setInterval(() => renderList(), 60000);
}

boot();
