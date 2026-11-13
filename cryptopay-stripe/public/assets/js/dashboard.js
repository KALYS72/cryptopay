// Кабинет продавца. Одностраничное приложение без сборки: разделы переключаются по #хэшу,
// все данные — из того же REST API /v1, которым пользуется сервер магазина (с токеном сессии вместо ключа).

import { api, loadConfig, session } from './api.js';
import {
  DELIVERY_STATUS,
  EVENT_LABELS,
  REFUND_STATUS,
  SESSION_STATUS,
  badge,
  formatDateTime,
  formatDay,
  formatUnits,
  intentBadge,
  money,
  shortAddress,
  shortHash,
  toMinorUnits,
} from './format.js';
import { $, copyButton, copyText, fill, h, setBusy, toast } from './ui.js';

let config = null;
let account = null;
let refreshTimer = null;
let drawerTimer = null;

const TITLES = {
  home: 'Главная',
  payments: 'Платежи',
  balance: 'Баланс',
  developers: 'Разработчикам',
  settings: 'Настройки',
};

const currencies = () => config.currencies;
const amountOf = (value, currency) => money(value, currency, currencies());
const decimalsOf = (currency) => currencies().find((c) => c.code === currency)?.decimals ?? 6;

// ─────────────────────────── вход ───────────────────────────

let registering = false;

function setAuthMode(register) {
  registering = register;
  $('#auth-title').textContent = register ? 'Регистрация продавца' : 'Вход в кабинет';
  $('#auth-submit').textContent = register ? 'Создать аккаунт' : 'Войти';
  $('#field-business').hidden = !register;
  $('#password-hint').hidden = !register;
  $('#password').autocomplete = register ? 'new-password' : 'current-password';
  for (const [id, active] of [['#tab-login', !register], ['#tab-register', register]]) {
    $(id).setAttribute('aria-selected', String(active));
    $(id).setAttribute('aria-pressed', String(active));
  }
  $('#auth-error').hidden = true;
}

async function signIn(path, body) {
  const button = $('#auth-submit');
  setBusy(button, true, registering ? 'Создаём…' : 'Входим…');
  $('#auth-error').hidden = true;
  try {
    const result = await api(path, { method: 'POST', body });
    session.set(result.token);
    account = result.account;
    await showApp();
  } catch (error) {
    const box = $('#auth-error');
    box.textContent = error.message;
    box.hidden = false;
  } finally {
    setBusy(button, false);
  }
}

function showAuth() {
  clearTimeout(refreshTimer);
  $('#app').hidden = true;
  $('#auth').hidden = false;
  document.title = 'Вход — CryptoPay';
  if (config.demo) {
    $('#demo-box').hidden = false;
    $('#demo-email').textContent = config.demo.email;
    $('#demo-password').textContent = config.demo.password;
  }
}

$('#tab-login').addEventListener('click', () => setAuthMode(false));
$('#tab-register').addEventListener('click', () => setAuthMode(true));
$('#auth-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const email = $('#email').value.trim();
  const password = $('#password').value;
  if (registering) {
    signIn('/api/auth/register', { email, password, business_name: $('#business_name').value.trim() });
  } else {
    signIn('/api/auth/login', { email, password });
  }
});
$('#demo-login').addEventListener('click', () => {
  setAuthMode(false);
  $('#email').value = config.demo.email;
  $('#password').value = config.demo.password;
  signIn('/api/auth/login', { email: config.demo.email, password: config.demo.password });
});
$('#logout').addEventListener('click', () => {
  session.clear();
  account = null;
  location.hash = '';
  showAuth();
});

// ─────────────────────────── каркас ───────────────────────────

async function showApp() {
  $('#auth').hidden = true;
  $('#app').hidden = false;
  $('#shop-name').textContent = account.business_name;
  $('#demo-storage').hidden = config.storage !== 'memory';
  await route();
}

async function route() {
  clearTimeout(refreshTimer);
  const [view = 'home', id] = location.hash.replace(/^#/, '').split('/');
  const name = TITLES[view] ? view : 'home';
  for (const link of document.querySelectorAll('.nav a')) {
    if (link.dataset.view === name) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  $('#view-title').textContent = TITLES[name];
  document.title = `${TITLES[name]} — CryptoPay`;
  try {
    await VIEWS[name]();
  } catch (error) {
    if (error.status === 401) return expired();
    fill($('#view'), h('div', { class: 'notice notice--error' }, error.message));
  }
  if (name === 'payments' && id) openPayment(id);
}

function expired() {
  session.clear();
  toast('Сессия истекла — войдите снова');
  showAuth();
}

window.addEventListener('hashchange', () => {
  if (account) route();
});

function card(title, note, ...children) {
  return h(
    'section',
    { class: 'paper card' },
    h('div', { class: 'card-head' }, h('h2', { class: 'card-title' }, title), note),
    ...children,
  );
}

// ─────────────────────────── главная ───────────────────────────

async function renderHome() {
  const [overview, recent] = await Promise.all([
    api('/api/dashboard/overview', { auth: true }),
    api('/v1/payment_intents?limit=5', { auth: true }),
  ]);
  const balanceCells = currencies().map((c) =>
    h(
      'div',
      { class: 'balance-cell' },
      h('div', { class: 'balance-amount' }, formatUnits(overview.balances[c.code] ?? '0', c.decimals)),
      h('div', { class: 'balance-currency' }, `${c.symbol} — ${c.name}`),
    ),
  );

  const withVolume = currencies().filter((c) => overview.volume[c.code]);
  let chartCurrency = withVolume[0]?.code ?? 'usdt';
  const chartBox = h('div');
  const switcher = h('div', { class: 'segmented', role: 'radiogroup', 'aria-label': 'Валюта графика' });
  const drawChart = () => {
    fill(
      switcher,
      currencies().map((c) =>
        h(
          'button',
          {
            type: 'button',
            role: 'radio',
            'aria-checked': String(c.code === chartCurrency),
            onclick: () => {
              chartCurrency = c.code;
              drawChart();
            },
          },
          c.symbol,
        ),
      ),
    );
    fill(chartBox, volumeChart(overview.days, overview.volume[chartCurrency] ?? {}, chartCurrency));
  };
  drawChart();

  const counts = overview.payments_by_status;
  const statusLinks = [
    ['succeeded', 'Оплачены'],
    ['processing', 'В сети'],
    ['requires_action', 'Ждут кошелька'],
    ['requires_payment_method', 'Ожидают оплаты'],
    ['canceled', 'Отменены'],
  ].map(([status, label]) =>
    h(
      'a',
      { class: 'status-count', href: `#payments`, onclick: () => (paymentsFilter = status) },
      h('strong', {}, counts[status] ?? 0),
      h('span', { class: `status status--${status}` }, label),
    ),
  );

  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  fill(
    $('#view'),
    total === 0 ? quickStart() : null,
    card('Баланс', h('span', { class: 'card-note' }, `за вычетом комиссии платформы ${config.fee_bps / 100} %`), h('div', { class: 'balances' }, balanceCells)),
    card('Оборот за 7 дней', switcher, chartBox),
    card('Платежи по статусам', null, h('div', { class: 'status-counts' }, statusLinks)),
    card('Последние платежи', h('a', { href: '#payments', class: 'small' }, 'Все платежи'), paymentsTable(recent.data)),
  );
  scheduleRefresh(recent.data, renderHome);
}

function quickStart() {
  return card(
    'Как принять первый платёж',
    null,
    h(
      'ol',
      { class: 'steps' },
      h('li', {}, h('strong', {}, 'Создайте ссылку'), 'Кнопка «Новый платёж» вверху или POST /v1/checkout/sessions с сервера магазина.'),
      h('li', {}, h('strong', {}, 'Оплатите её'), 'Откройте ссылку, выберите тестовый кошелёк Алисы и подтвердите платёж.'),
      h('li', {}, h('strong', {}, 'Посмотрите результат'), 'Платёж появится здесь, а на ваш сервер придёт вебхук payment_intent.succeeded.'),
    ),
  );
}

function volumeChart(days, volume, currency) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  const width = 700;
  const height = 190;
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('class', 'chart');
  svg.setAttribute('role', 'img');
  const values = days.map((d) => BigInt(volume[d] ?? '0'));
  const max = values.reduce((a, b) => (b > a ? b : a), 0n);
  const decimals = decimalsOf(currency);
  const summary = days.map((d, i) => `${formatDay(d)}: ${formatUnits(values[i], decimals)}`).join('; ');
  svg.setAttribute('aria-label', `Оборот в ${currency.toUpperCase()} по дням. ${summary}`);
  const slot = width / days.length;
  const plot = height - 44;
  days.forEach((day, i) => {
    const ratio = max === 0n ? 0 : Number((values[i] * 1000n) / max) / 1000;
    const barHeight = Math.max(3, ratio * plot);
    const x = i * slot + slot * 0.2;
    const rect = document.createElementNS(ns, 'rect');
    rect.setAttribute('x', String(x));
    rect.setAttribute('y', String(22 + plot - barHeight));
    rect.setAttribute('width', String(slot * 0.6));
    rect.setAttribute('height', String(barHeight));
    rect.setAttribute('rx', '3');
    rect.setAttribute('class', values[i] > 0n ? 'bar' : 'bar-empty');
    svg.append(rect);
    if (values[i] > 0n) {
      const label = document.createElementNS(ns, 'text');
      label.setAttribute('x', String(i * slot + slot / 2));
      label.setAttribute('y', String(16 + plot - barHeight));
      label.setAttribute('text-anchor', 'middle');
      label.setAttribute('class', 'bar-value');
      label.textContent = formatUnits(values[i], decimals, { minFraction: 0, maxFraction: 4 });
      svg.append(label);
    }
    const dayLabel = document.createElementNS(ns, 'text');
    dayLabel.setAttribute('x', String(i * slot + slot / 2));
    dayLabel.setAttribute('y', String(height - 6));
    dayLabel.setAttribute('text-anchor', 'middle');
    dayLabel.textContent = formatDay(day);
    svg.append(dayLabel);
  });
  return svg;
}

// ─────────────────────────── платежи ───────────────────────────

let paymentsFilter = null;
const FILTERS = [
  [null, 'Все'],
  ['succeeded', 'Оплачены'],
  ['processing', 'В сети'],
  ['requires_action', 'Ждут кошелька'],
  ['requires_payment_method', 'Ожидают оплаты'],
  ['canceled', 'Отменены'],
];

function paymentsTable(items, emptyText = 'Платежей пока нет. Нажмите «Новый платёж», чтобы создать ссылку на оплату.') {
  if (items.length === 0) return h('p', { class: 'empty' }, emptyText);
  return h(
    'div',
    { class: 'table-scroll' },
    h(
      'table',
      { class: 'ledger' },
      h(
        'thead',
        {},
        h('tr', {}, h('th', { class: 'amount' }, 'Сумма'), h('th', {}, 'Статус'), h('th', {}, 'Описание'), h('th', {}, 'Покупатель'), h('th', {}, 'Создан')),
      ),
      h('tbody', {}, items.map(paymentRow)),
    ),
  );
}

function paymentRow(pi) {
  const open = () => {
    location.hash = `#payments/${pi.id}`;
  };
  return h(
    'tr',
    {
      class: 'is-clickable',
      tabindex: '0',
      onclick: open,
      onkeydown: (event) => {
        if (event.key === 'Enter') open();
      },
    },
    h('td', { class: 'amount' }, amountOf(pi.amount, pi.currency)),
    h('td', {}, intentBadge(pi)),
    h('td', {}, pi.description ?? h('span', { class: 'muted' }, 'без описания'), h('span', { class: 'cell-sub mono' }, pi.id)),
    h('td', {}, pi.customer_email ?? (pi.payment_method.payer_address ? shortAddress(pi.payment_method.payer_address) : '—')),
    h('td', { class: 'num' }, formatDateTime(pi.created)),
  );
}

async function renderPayments() {
  const tableBox = h('div');
  const moreBox = h('div', { class: 'more' });
  let items = [];

  const load = async (after) => {
    const params = new URLSearchParams({ limit: '20' });
    if (paymentsFilter) params.set('status', paymentsFilter);
    if (after) params.set('starting_after', after);
    const page = await api(`/v1/payment_intents?${params}`, { auth: true });
    items = after ? [...items, ...page.data] : page.data;
    fill(tableBox, paymentsTable(items, 'Здесь пока пусто.'));
    fill(
      moreBox,
      page.has_more
        ? h('button', { type: 'button', class: 'btn btn--quiet', onclick: () => load(items.at(-1).id) }, 'Показать ещё')
        : null,
    );
    scheduleRefresh(items, () => (location.hash.startsWith('#payments') ? load() : null));
  };

  const filters = h(
    'div',
    { class: 'segmented filters', role: 'radiogroup', 'aria-label': 'Фильтр по статусу' },
    FILTERS.map(([status, label]) =>
      h(
        'button',
        {
          type: 'button',
          role: 'radio',
          'aria-checked': String(paymentsFilter === status),
          onclick: () => {
            paymentsFilter = status;
            renderPayments();
          },
        },
        label,
      ),
    ),
  );
  fill($('#view'), h('section', { class: 'paper card' }, h('div', { class: 'card-head' }, filters), tableBox, moreBox));
  await load();
}

/** Пока есть платежи «в пути», обновляем раздел — видно, как статус меняется в реальном времени. */
function scheduleRefresh(items, again) {
  clearTimeout(refreshTimer);
  if (items.some((pi) => pi.status === 'processing' || pi.status === 'requires_action')) {
    refreshTimer = setTimeout(() => {
      if (document.visibilityState === 'visible' && !$('#drawer').open) again();
      else scheduleRefresh(items, again);
    }, 3000);
  }
}

// ─────────────────────────── карточка платежа ───────────────────────────

const EVENT_ORDER = Object.fromEntries(
  [
    'payment_intent.created',
    'payment_intent.requires_action',
    'payment_intent.processing',
    'payment_intent.payment_failed',
    'payment_intent.succeeded',
    'checkout.session.completed',
    'payment_intent.canceled',
    'checkout.session.expired',
    'refund.created',
    'refund.succeeded',
    'refund.failed',
  ].map((type, index) => [type, index]),
);

async function openPayment(id) {
  const drawer = $('#drawer');
  if (!drawer.open) drawer.showModal();
  await renderDrawer(id);
}

async function renderDrawer(id) {
  clearTimeout(drawerTimer);
  let pi;
  let events;
  let refunds;
  try {
    [pi, events, refunds] = await Promise.all([
      api(`/v1/payment_intents/${encodeURIComponent(id)}`, { auth: true }),
      api(`/v1/events?related_object=${encodeURIComponent(id)}&limit=50`, { auth: true }),
      api(`/v1/refunds?payment_intent=${encodeURIComponent(id)}&limit=20`, { auth: true }),
    ]);
  } catch (error) {
    fill($('#drawer-body'), h('div', { class: 'notice notice--error' }, error.message));
    return;
  }
  let checkout = null;
  const related = [...(pi.checkout_session ? [pi.checkout_session] : []), ...refunds.data.map((r) => r.id)];
  const [relatedEvents] = await Promise.all([
    Promise.all(related.map((object) => api(`/v1/events?related_object=${encodeURIComponent(object)}&limit=20`, { auth: true }))),
    pi.checkout_session
      ? api(`/v1/checkout/sessions/${encodeURIComponent(pi.checkout_session)}`, { auth: true }).then((s) => (checkout = s))
      : null,
  ]);
  // created — с точностью до секунды; события одной секунды упорядочиваем по жизненному циклу платежа
  const allEvents = [...events.data, ...relatedEvents.flatMap((page) => page.data)].sort(
    (a, b) => a.created - b.created || (EVENT_ORDER[a.type] ?? 99) - (EVENT_ORDER[b.type] ?? 99),
  );
  const pm = pi.payment_method;

  const details = [
    ['Идентификатор', h('span', { class: 'mono' }, pi.id)],
    ['Описание', pi.description ?? '—'],
    ['Покупатель', pi.customer_email ?? '—'],
    ['Кошелёк покупателя', pm.payer_address ? h('span', { class: 'mono', title: pm.payer_address }, shortAddress(pm.payer_address)) : '—'],
    ['Транзакция', pm.transaction ? h('a', { class: 'mono', href: '/wallet', target: '_blank', rel: 'noopener', title: pm.transaction }, shortHash(pm.transaction)) : '—'],
    ['Подтверждения', `${pm.confirmations} из ${pm.required_confirmations}`],
    ['Получено', amountOf(pi.amount_received, pi.currency)],
    ['Возвращено', amountOf(pi.amount_refunded, pi.currency)],
    ['Страница оплаты', checkout ? h('span', {}, badge(checkout.status, SESSION_STATUS), ' ', h('span', { class: 'mono' }, checkout.id)) : '—'],
    ...Object.entries(pi.metadata).map(([key, value]) => [`metadata.${key}`, h('span', { class: 'mono' }, value)]),
  ];
  if (pi.last_payment_error) details.splice(2, 0, ['Последняя ошибка', h('span', { class: 'negative' }, pi.last_payment_error.message)]);

  const actions = [];
  if (checkout?.url) {
    actions.push(copyButton(checkout.url, 'Скопировать ссылку на оплату', 'Ссылка скопирована'));
    actions.push(h('a', { class: 'btn btn--quiet btn--small', href: checkout.url, target: '_blank', rel: 'noopener' }, 'Открыть страницу оплаты'));
  }
  if (['requires_payment_method', 'requires_action'].includes(pi.status)) {
    actions.push(
      h('button', { type: 'button', class: 'btn btn--danger btn--small', onclick: (event) => cancelPayment(pi, event.currentTarget) }, 'Отменить платёж'),
    );
  }

  const refundable = BigInt(pi.amount_received) - BigInt(pi.amount_refunded);
  fill(
    $('#drawer-body'),
    pi.status === 'succeeded' ? h('div', { class: 'stamp', 'aria-hidden': 'true' }, 'Оплачено') : null,
    h('div', {}, h('div', { class: 'drawer-amount' }, amountOf(pi.amount, pi.currency)), intentBadge(pi)),
    actions.length ? h('div', { class: 'drawer-actions' }, actions) : null,
    h('section', {}, h('h3', { class: 'section-label' }, 'Подробности'), h('dl', { class: 'details' }, details.map(([k, v]) => h('div', {}, h('dt', {}, k), h('dd', {}, v))))),
    h(
      'section',
      {},
      h('h3', { class: 'section-label' }, 'Возвраты'),
      refunds.data.length
        ? h(
            'table',
            { class: 'ledger' },
            h('tbody', {}, refunds.data.map((r) => h('tr', {}, h('td', { class: 'amount' }, amountOf(r.amount, r.currency)), h('td', {}, badge(r.status, REFUND_STATUS)), h('td', { class: 'num' }, formatDateTime(r.created))))),
          )
        : h('p', { class: 'muted small' }, pi.status === 'succeeded' ? 'Возвратов не было.' : 'Вернуть можно только успешный платёж.'),
      pi.status === 'succeeded' && refundable > 0n ? refundForm(pi, refundable) : null,
    ),
    h(
      'section',
      {},
      h('h3', { class: 'section-label' }, 'История'),
      h(
        'ol',
        { class: 'timeline' },
        allEvents.map((e) =>
          h(
            'li',
            { class: /failed|canceled|expired/.test(e.type) ? 'is-failure' : '' },
            EVENT_LABELS[e.type] ?? e.type,
            e.type === 'payment_intent.payment_failed' && e.data.object.last_payment_error ? h('span', { class: 'cell-sub' }, e.data.object.last_payment_error.message) : null,
            h('time', {}, formatDateTime(e.created), ' ', h('span', { class: 'mono' }, e.type)),
          ),
        ),
      ),
    ),
    h('details', { class: 'raw' }, h('summary', {}, 'Объект PaymentIntent в JSON'), h('pre', { class: 'code' }, JSON.stringify(pi, null, 2))),
  );
  $('#drawer-title').textContent = `Платёж ${shortHash(pi.id)}`;

  if (['processing', 'requires_action'].includes(pi.status) || refunds.data.some((r) => r.status === 'pending')) {
    drawerTimer = setTimeout(() => {
      if ($('#drawer').open) renderDrawer(id);
    }, 2500);
  }
}

function refundForm(pi, refundable) {
  const decimals = decimalsOf(pi.currency);
  const input = h('input', {
    inputmode: 'decimal',
    'aria-label': 'Сумма возврата',
    placeholder: `Вся сумма: ${formatUnits(refundable, decimals, { minFraction: 0 })}`,
  });
  const button = h('button', { type: 'submit', class: 'btn btn--quiet' }, 'Вернуть деньги');
  return h(
    'form',
    {
      class: 'refund-form',
      novalidate: true,
      onsubmit: async (event) => {
        event.preventDefault();
        const text = input.value.trim();
        const amount = text ? toMinorUnits(text, decimals) : null;
        if (text && amount === null) {
          toast(`Сумма — положительное число, не больше ${decimals} знаков после запятой`, 'error');
          return;
        }
        setBusy(button, true, 'Отправляем…');
        try {
          await api('/v1/refunds', {
            method: 'POST',
            auth: true,
            headers: { 'idempotency-key': crypto.randomUUID() },
            body: { payment_intent: pi.id, amount: amount ?? undefined },
          });
          toast('Возврат создан — деньги уходят покупателю');
          renderDrawer(pi.id);
        } catch (error) {
          toast(error.message, 'error');
          setBusy(button, false);
        }
      },
    },
    input,
    button,
  );
}

async function cancelPayment(pi, button) {
  setBusy(button, true, 'Отменяем…');
  try {
    await api(`/v1/payment_intents/${encodeURIComponent(pi.id)}/cancel`, {
      method: 'POST',
      auth: true,
      body: { cancellation_reason: 'requested_by_customer' },
    });
    toast('Платёж отменён');
    renderDrawer(pi.id);
  } catch (error) {
    toast(error.message, 'error');
    setBusy(button, false);
  }
}

$('#drawer').addEventListener('close', () => {
  clearTimeout(drawerTimer);
  if (location.hash.startsWith('#payments/')) history.replaceState(null, '', '#payments');
  if (account) route();
});

// ─────────────────────────── баланс ───────────────────────────

const ENTRY_TYPES = { payment: 'Оплата', refund: 'Возврат', refund_failure: 'Возврат не прошёл' };

async function renderBalance() {
  const [balance, entries] = await Promise.all([
    api('/v1/balance', { auth: true }),
    api('/v1/balance_transactions?limit=50', { auth: true }),
  ]);
  const rows = entries.data.map((e) =>
    h(
      'tr',
      {},
      h('td', {}, ENTRY_TYPES[e.type] ?? e.type, h('span', { class: 'cell-sub mono' }, e.source)),
      h('td', { class: `amount${e.amount.startsWith('-') ? ' negative' : ''}` }, amountOf(e.amount, e.currency)),
      h('td', { class: 'amount muted' }, e.fee === '0' ? '—' : amountOf(e.fee, e.currency)),
      h('td', { class: `amount${e.net.startsWith('-') ? ' negative' : ''}` }, amountOf(e.net, e.currency)),
      h('td', { class: 'num' }, formatDateTime(e.created)),
    ),
  );
  fill(
    $('#view'),
    card(
      'Доступно',
      h('span', { class: 'card-note' }, 'Баланс — это сумма всех проводок ниже'),
      h(
        'div',
        { class: 'balances' },
        balance.available.map((b) =>
          h('div', { class: 'balance-cell' }, h('div', { class: 'balance-amount' }, formatUnits(b.amount, decimalsOf(b.currency))), h('div', { class: 'balance-currency' }, b.currency.toUpperCase())),
        ),
      ),
    ),
    card(
      'Журнал проводок',
      h('span', { class: 'card-note' }, `Комиссия платформы — ${config.fee_bps / 100} % от каждой оплаты`),
      rows.length
        ? h('div', { class: 'table-scroll' }, h('table', { class: 'ledger' }, h('thead', {}, h('tr', {}, h('th', {}, 'Операция'), h('th', { class: 'amount' }, 'Сумма'), h('th', { class: 'amount' }, 'Комиссия'), h('th', { class: 'amount' }, 'Итого'), h('th', {}, 'Когда'))), h('tbody', {}, rows)))
        : h('p', { class: 'empty' }, 'Проводок пока нет: они появляются после первой успешной оплаты.'),
    ),
  );
}

// ─────────────────────────── разработчикам ───────────────────────────

const EVENT_CHOICES = [
  'payment_intent.succeeded',
  'payment_intent.payment_failed',
  'payment_intent.processing',
  'payment_intent.canceled',
  'checkout.session.completed',
  'checkout.session.expired',
  'refund.succeeded',
  'refund.failed',
];

function showSecret(title, text, value) {
  $('#secret-title').textContent = title;
  $('#secret-text').textContent = text;
  $('#secret-value').textContent = value;
  $('#copy-secret').onclick = async () => toast((await copyText(value)) ? 'Скопировано' : 'Не удалось скопировать');
  $('#secret-dialog').showModal();
}

async function renderDevelopers() {
  const [me, endpoints, events] = await Promise.all([
    api('/api/account', { auth: true }),
    api('/v1/webhook_endpoints', { auth: true }),
    api('/v1/events?limit=15', { auth: true }),
  ]);
  account = me;
  const keyPreview = me.api_key ? me.api_key.preview : 'Ключ ещё не выпущен';
  const rollButton = h(
    'button',
    {
      type: 'button',
      class: 'btn btn--quiet',
      onclick: async (event) => {
        const button = event.currentTarget;
        setBusy(button, true, 'Выпускаем…');
        try {
          const result = await api('/api/account/api_key', { method: 'POST', auth: true });
          showSecret('Новый секретный ключ', 'Скопируйте его сейчас: больше мы его не покажем. Предыдущий ключ уже не работает.', result.secret);
          renderDevelopers();
        } catch (error) {
          toast(error.message, 'error');
          setBusy(button, false);
        }
      },
    },
    me.api_key ? 'Выпустить новый ключ' : 'Выпустить ключ',
  );

  const sample = [
    `curl ${location.origin}/v1/checkout/sessions \\`,
    '  -u sk_test_ваш_секретный_ключ: \\',
    '  -H "Content-Type: application/json" \\',
    `  -H "Idempotency-Key: order-1042" \\`,
    `  -d '{"amount": "25000000", "currency": "usdt", "description": "Худи", "success_url": "https://shop.example/thanks"}'`,
  ].join('\n');

  fill(
    $('#view'),
    card(
      'Секретный ключ API',
      h('span', { class: 'card-note' }, 'Для сервера магазина. Никогда не вставляйте его в код страниц сайта.'),
      h('div', { class: 'key-row' }, h('span', { class: `key-preview${me.api_key ? ' mono' : ' muted'}` }, keyPreview), rollButton),
      h('pre', { class: 'code', 'aria-label': 'Пример запроса' }, sample),
    ),
    webhooksCard(endpoints.data),
    eventsCard(events.data),
  );
}

function webhooksCard(endpoints) {
  const list = endpoints.length
    ? endpoints.map((e) =>
        h(
          'div',
          { class: 'endpoint' },
          h('div', {}, h('div', { class: 'mono' }, e.url), h('div', { class: 'cell-sub' }, e.enabled_events.includes('*') ? 'Все события' : e.enabled_events.join(', '))),
          h(
            'div',
            { class: 'endpoint-actions' },
            h('button', { type: 'button', class: 'btn btn--quiet btn--small', onclick: () => testEndpoint(e) }, 'Отправить тест'),
            h('button', { type: 'button', class: 'btn btn--danger btn--small', onclick: () => deleteEndpoint(e) }, 'Удалить'),
          ),
        ),
      )
    : [h('p', { class: 'muted small' }, 'Адресов пока нет. Добавьте URL вашего сервера, и CryptoPay будет присылать туда события.')];

  const url = h('input', { id: 'we-url', type: 'url', placeholder: 'https://shop.example/webhooks/cryptopay', required: true });
  const all = h('input', { type: 'checkbox', checked: true, value: '*' });
  const picks = EVENT_CHOICES.map((type) => h('label', {}, h('input', { type: 'checkbox', value: type }), h('span', { class: 'mono' }, type)));
  const submit = h('button', { type: 'submit', class: 'btn' }, 'Добавить адрес');
  const form = h(
    'form',
    {
      class: 'inline-form',
      novalidate: true,
      onsubmit: async (event) => {
        event.preventDefault();
        const chosen = all.checked ? ['*'] : picks.map((p) => p.querySelector('input')).filter((i) => i.checked).map((i) => i.value);
        setBusy(submit, true, 'Добавляем…');
        try {
          const created = await api('/v1/webhook_endpoints', { method: 'POST', auth: true, body: { url: url.value.trim(), enabled_events: chosen } });
          showSecret('Секрет подписи вебхуков', 'Им ваш сервер проверяет заголовок CryptoPay-Signature. Скопируйте его сейчас — больше мы его не покажем.', created.secret);
          renderDevelopers();
        } catch (error) {
          toast(error.message, 'error');
          setBusy(submit, false);
        }
      },
    },
    h('div', { class: 'field' }, h('label', { for: 'we-url' }, 'URL на вашем сервере'), url),
    h('div', { class: 'field' }, h('span', { class: 'label' }, 'События'), h('label', { class: 'small' }, all, ' Все события'), h('div', { class: 'events-picker' }, picks)),
    h('div', { class: 'actions' }, submit),
  );
  return card('Вебхуки', h('span', { class: 'card-note' }, 'Подписаны HMAC-SHA256, повторяются при ошибке'), ...list, form);
}

async function testEndpoint(endpoint) {
  try {
    await api(`/v1/webhook_endpoints/${encodeURIComponent(endpoint.id)}/test`, { method: 'POST', auth: true });
    toast('Тестовое событие поставлено в очередь — уйдёт в течение нескольких секунд');
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function deleteEndpoint(endpoint) {
  if (!window.confirm(`Удалить адрес ${endpoint.url}? События туда больше не придут.`)) return;
  try {
    await api(`/v1/webhook_endpoints/${encodeURIComponent(endpoint.id)}`, { method: 'DELETE', auth: true });
    toast('Адрес удалён');
    renderDevelopers();
  } catch (error) {
    toast(error.message, 'error');
  }
}

function eventsCard(events) {
  const rows = events.map((e) => {
    const details = h('td', { colspan: '3' });
    const row = h('tr', { hidden: true }, details);
    const toggle = async () => {
      row.hidden = !row.hidden;
      if (row.hidden || details.childElementCount) return;
      const deliveries = await api(`/v1/events/${encodeURIComponent(e.id)}/deliveries`, { auth: true });
      fill(
        details,
        deliveries.data.length
          ? h('ul', { class: 'small' }, deliveries.data.map((d) => h('li', {}, badge(d.status, DELIVERY_STATUS), ` ${d.url}, попыток: ${d.attempts}`, d.last_error ? h('span', { class: 'cell-sub' }, d.last_error) : null)))
          : h('p', { class: 'muted small' }, 'Это событие никуда не отправлялось: нет подходящих адресов вебхуков.'),
        h('pre', { class: 'code' }, JSON.stringify(e, null, 2)),
      );
    };
    return [
      h('tr', { class: 'is-clickable', tabindex: '0', onclick: toggle, onkeydown: (ev) => ev.key === 'Enter' && toggle() }, h('td', { class: 'mono' }, e.type), h('td', { class: 'mono muted' }, e.data.object.id ?? ''), h('td', { class: 'num' }, formatDateTime(e.created))),
      row,
    ];
  });
  return card(
    'Журнал событий',
    h('span', { class: 'card-note' }, 'Нажмите на событие, чтобы увидеть JSON и доставки'),
    rows.length ? h('div', { class: 'table-scroll' }, h('table', { class: 'ledger' }, h('tbody', {}, rows))) : h('p', { class: 'empty' }, 'Событий пока нет.'),
  );
}

// ─────────────────────────── настройки ───────────────────────────

async function renderSettings() {
  account = await api('/api/account', { auth: true });
  const name = h('input', { id: 'settings-name', value: account.business_name, maxlength: '100' });
  const save = h('button', { type: 'submit', class: 'btn' }, 'Сохранить');
  fill(
    $('#view'),
    card(
      'Магазин',
      null,
      h(
        'form',
        {
          novalidate: true,
          onsubmit: async (event) => {
            event.preventDefault();
            setBusy(save, true, 'Сохраняем…');
            try {
              account = await api('/api/account', { method: 'PATCH', auth: true, body: { business_name: name.value } });
              $('#shop-name').textContent = account.business_name;
              toast('Сохранено');
            } catch (error) {
              toast(error.message, 'error');
            } finally {
              setBusy(save, false);
            }
          },
        },
        h('div', { class: 'field' }, h('label', { for: 'settings-name' }, 'Название — его видят покупатели на странице оплаты'), name),
        h('div', { class: 'dialog-actions' }, save),
      ),
    ),
    card('Аккаунт', null, h('dl', { class: 'details' }, h('div', {}, h('dt', {}, 'E-mail'), h('dd', {}, account.email)), h('div', {}, h('dt', {}, 'Идентификатор'), h('dd', { class: 'mono' }, account.id)), h('div', {}, h('dt', {}, 'Создан'), h('dd', {}, formatDateTime(account.created))))),
  );
}

const VIEWS = { home: renderHome, payments: renderPayments, balance: renderBalance, developers: renderDevelopers, settings: renderSettings };

// ─────────────────────────── новый платёж ───────────────────────────

function openPaymentDialog() {
  const dialog = $('#payment-dialog');
  $('#payment-form').hidden = false;
  $('#payment-created').hidden = true;
  $('#payment-error').hidden = true;
  fill($('#pf-currency'), currencies().map((c) => h('option', { value: c.code }, c.symbol)));
  dialog.showModal();
  $('#pf-amount').focus();
}

$('#new-payment').addEventListener('click', openPaymentDialog);
for (const button of document.querySelectorAll('[data-close]')) {
  button.addEventListener('click', () => button.closest('dialog').close());
}

$('#payment-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const currency = $('#pf-currency').value;
  const amountInput = $('#pf-amount');
  const amount = toMinorUnits(amountInput.value, decimalsOf(currency));
  const error = $('#payment-error');
  error.hidden = true;
  amountInput.removeAttribute('aria-invalid');
  if (amount === null) {
    amountInput.setAttribute('aria-invalid', 'true');
    error.textContent = `Укажите сумму числом, не больше ${decimalsOf(currency)} знаков после запятой`;
    error.hidden = false;
    return;
  }
  const button = $('#payment-submit');
  setBusy(button, true, 'Создаём…');
  try {
    const created = await api('/v1/checkout/sessions', {
      method: 'POST',
      auth: true,
      headers: { 'idempotency-key': crypto.randomUUID() },
      body: {
        amount,
        currency,
        description: $('#pf-description').value.trim() || null,
        success_url: $('#pf-success').value.trim() || null,
      },
    });
    $('#payment-form').hidden = true;
    $('#payment-created').hidden = false;
    $('#created-url').textContent = created.url;
    $('#open-created').href = created.url;
    $('#copy-created').onclick = async () => toast((await copyText(created.url)) ? 'Ссылка скопирована' : 'Не удалось скопировать');
    $('#payment-form').reset();
    if (!location.hash.startsWith('#payments/')) route();
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
  } finally {
    setBusy(button, false);
  }
});

// ─────────────────────────── старт ───────────────────────────

async function boot() {
  config = await loadConfig();
  if (!session.get()) return showAuth();
  try {
    account = await api('/api/account', { auth: true });
    await showApp();
  } catch {
    session.clear();
    showAuth();
  }
}

boot();
