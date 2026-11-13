// Страница «Как это работает»: схема частей системы и настоящий платёж по шагам.
// Каждый шаг делает настоящие запросы к этому серверу и подсвечивает на схеме части, которые работают.

import { api, ApiError, loadConfig } from './api.js';
import { $, h, fill, sleep, setBusy } from './ui.js';
import { formatUnits } from './format.js';

/* ───────────── Части системы ───────────── */

const PARTS = {
  shop: {
    title: 'Сервер магазина',
    role: 'Клиент нашего API. Создаёт оплату и узнаёт о ней из вебхука.',
    how: [
      'Отправляет POST /v1/checkout/sessions с секретным ключом sk_test_… и получает ссылку на страницу оплаты.',
      'Принимает вебхук payment_intent.succeeded и проверяет подпись HMAC-SHA256, прежде чем отметить заказ оплаченным.',
      'Ничего не знает о кошельках и блокчейне: для него это обычный платёжный API, как Stripe.',
    ],
    code: 'examples/shop.py',
  },
  page: {
    title: 'Страница оплаты',
    role: 'То, что видит покупатель: магазин, сумма, кнопка «Оплатить» и ход подтверждений.',
    how: [
      'Получает от сервера только публичные данные. Metadata и комиссия магазина сюда не попадают.',
      'По кнопке «Оплатить» сервер создаёт запрос в кошельке, и покупатель уходит его подтверждать.',
      'Сама ничего не решает: статус платежа всегда берётся с сервера.',
    ],
    code: 'public/checkout.html, public/assets/js/checkout.js',
  },
  api: {
    title: 'CryptoPay API',
    role: 'Ядро системы: HTTP-слой, семь сервисов и доменная модель.',
    how: [
      'FastAPI принимает запрос и передаёт его сервису: CheckoutService, PaymentService, RefundService и другим.',
      'Главная сущность — PaymentIntent. Его автомат состояний не даёт перейти в «успешно», минуя «обрабатывается».',
      'Сервисы зависят только от портов (интерфейсов). Конкретные классы подставляет container.py — это DIP.',
    ],
    code: 'cryptopay/domain, cryptopay/application, cryptopay/infrastructure/http',
  },
  wallet: {
    title: 'Кошелёк MockWallet',
    role: 'Внешняя система, которая имитирует криптокошелёк вроде MetaMask.',
    how: [
      'CryptoPay вызывает его только через порт CryptoWallet — пять методов. Перевод на язык кошелька делает MockWalletAdapter.',
      'У тестовых кошельков заранее известный исход, как у тестовых карт Stripe: Алиса платит, у Боба не хватает денег, у Евы транзакция падает.',
      'MockWallet не импортирует ни строчки CryptoPay — это проверяет тест. Настоящий кошелёк — это ещё один адаптер.',
    ],
    code: 'cryptopay/mockwallet, cryptopay/application/ports/wallet.py, cryptopay/infrastructure/wallet/mock_wallet_adapter.py',
    link: { href: '/wallet', text: 'Открыть обозреватель сети' },
  },
  chain: {
    title: 'Сеть MockChain',
    role: 'Имитация блокчейна, в который кошелёк отправляет транзакции.',
    how: [
      'Блок появляется раз в 3 секунды. Высота сети — это время с запуска, делённое на 3 секунды.',
      'Подтверждения считаются как в Ethereum: текущая высота минус блок транзакции плюс один.',
      'Платёж засчитывается после трёх подтверждений.',
    ],
    code: 'cryptopay/mockwallet/chain.py',
    link: { href: '/wallet', text: 'Посмотреть блоки и транзакции' },
  },
  jobs: {
    title: 'Фоновые задачи',
    role: 'Работают сами по себе, раз в 3 секунды, даже если покупатель закрыл вкладку.',
    how: [
      'Сверка платежей и возвратов: спрашивают кошелёк через порт, сколько подтверждений у транзакции.',
      'Доставка вебхуков: подписывают событие и отправляют магазину, а при ошибке повторяют через 1, 5, 15, 60 и 360 минут.',
      'Истечение неоплаченных сессий и очистка старых ключей идемпотентности.',
    ],
    code: 'cryptopay/application/jobs, cryptopay/infrastructure/scheduler.py',
  },
  db: {
    title: 'База данных PostgreSQL',
    role: 'Девять таблиц: платежи, сессии, возвраты, журнал баланса, события, вебхуки, ключи идемпотентности и аккаунты.',
    how: [
      'Сервисы работают с ней через порт UnitOfWork: всё, что относится к одной операции, пишется одной транзакцией.',
      'Защита от двойного зачисления: версия строки (оптимистическая блокировка) и уникальные индексы.',
      'Есть вторая реализация — в памяти. Обе проходят одни и те же 16 контрактных тестов: это LSP.',
    ],
    code: 'cryptopay/infrastructure/postgres, migrations/001_initial_schema.sql',
  },
};

const scheme = $('#scheme');
const partCard = $('#part-card');

function showPart(id) {
  const part = PARTS[id];
  scheme.querySelectorAll('.part').forEach((node) => node.classList.toggle('is-selected', node.dataset.part === id));
  fill(
    partCard,
    h('h2', { class: 'part-card-title' }, part.title),
    h('p', { class: 'part-card-role' }, part.role),
    h('ul', { class: 'part-card-how' }, part.how.map((line) => h('li', {}, line))),
    h('p', { class: 'part-card-code small' }, h('span', { class: 'muted' }, 'Где в коде: '), h('span', { class: 'mono' }, part.code)),
    part.link ? h('p', { class: 'small' }, h('a', { href: part.link.href, target: '_blank', rel: 'noopener' }, part.link.text)) : null,
  );
}

scheme.querySelectorAll('.part').forEach((node) => {
  node.addEventListener('click', () => showPart(node.dataset.part));
  node.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      showPart(node.dataset.part);
    }
  });
});

/** Подсветить на схеме части и связи, которые работают на текущем шаге. */
function highlight(parts = [], edges = []) {
  scheme.querySelectorAll('.part').forEach((node) => node.classList.toggle('is-active', parts.includes(node.dataset.part)));
  scheme.querySelectorAll('.edge').forEach((path) => {
    const on = edges.includes(path.dataset.edge);
    path.classList.toggle('is-active', on);
    path.setAttribute('marker-end', on ? 'url(#arrow-active)' : 'url(#arrow)');
  });
  scheme.querySelectorAll('[data-edge-label]').forEach((label) => {
    label.classList.toggle('is-active', edges.includes(label.dataset.edgeLabel));
  });
}

/* ───────────── Платёж по шагам ───────────── */

const ctx = {};
const pick = (object, keys) => Object.fromEntries(keys.filter((key) => key in object).map((key) => [key, object[key]]));
const usdt = (minor) => `${formatUnits(minor, 6)} USDT`;

async function asMerchant(path, options = {}) {
  return api(path, { ...options, headers: { authorization: `Bearer ${ctx.token}`, ...(options.headers ?? {}) } });
}

const STEPS = [
  {
    title: 'Магазин создаёт платёж',
    parts: ['shop', 'api', 'db'],
    edges: ['shop-api', 'api-db'],
    text: 'Сервер магазина отправляет запрос с секретным ключом. CryptoPay создаёт сессию оплаты (cs_) и намерение оплатить (pi_) и сохраняет их в базе одной транзакцией. Заголовок Idempotency-Key защищает от двойного создания, если магазин повторит запрос.',
    async run() {
      const config = await loadConfig();
      if (!config.demo) throw new Error('Демо-аккаунт на этом сервере выключен (DEMO_ACCOUNT=false).');
      ctx.config = config;
      const login = await api('/api/auth/login', { method: 'POST', body: config.demo });
      ctx.token = login.token;
      const key = `how-${crypto.randomUUID().slice(0, 8)}`;
      const body = {
        amount: '25000000',
        currency: 'usdt',
        description: 'Худи «Пиксель», размер M',
        metadata: { source: 'how-it-works' },
      };
      const session = await asMerchant('/v1/checkout/sessions', { method: 'POST', body, headers: { 'idempotency-key': key } });
      ctx.session = session.id;
      ctx.intent = session.payment_intent;
      return {
        request: `POST /v1/checkout/sessions\nAuthorization: Bearer …\nIdempotency-Key: ${key}\n\n${JSON.stringify(body, null, 2)}`,
        response: pick(session, ['id', 'object', 'status', 'payment_intent', 'amount_total', 'currency', 'url']),
        note: 'В роли магазина здесь выступает эта страница: она вошла в демо-аккаунт и отправила тот же запрос, что отправил бы сервер магазина.',
      };
    },
  },
  {
    title: 'Покупатель открывает страницу оплаты',
    parts: ['page', 'api', 'db'],
    edges: ['page-api', 'api-db'],
    text: 'Страница оплаты получает только то, что можно показывать покупателю: магазин, сумму и статус. Платёж пока ждёт способ оплаты — requires_payment_method.',
    async run() {
      const view = await api(`/api/checkout/${ctx.session}`);
      return {
        request: `GET /api/checkout/${ctx.session}`,
        response: {
          status: view.status,
          business_name: view.business_name,
          description: view.description,
          amount_display: `${view.amount_display} ${view.currency_symbol}`,
          payment: pick(view.payment, ['status', 'required_confirmations']),
        },
        link: { href: `/checkout/${ctx.session}`, text: 'Посмотреть эту страницу оплаты' },
      };
    },
  },
  {
    title: 'Покупатель нажимает «Оплатить»',
    parts: ['page', 'api', 'wallet'],
    edges: ['page-api', 'api-wallet', 'page-wallet'],
    text: 'PaymentService вызывает порт CryptoWallet. Адаптер MockWalletAdapter переводит вызов на язык кошелька, и кошелёк создаёт запрос на оплату (wpr_). Платёж переходит в requires_action — ждёт, когда покупатель подтвердит перевод.',
    async run() {
      const { redirect_url: url } = await api(`/api/checkout/${ctx.session}/pay`, {
        method: 'POST',
        body: { customer_email: 'buyer@example.com' },
      });
      ctx.request = url.split('/approve/')[1];
      const [wallet, view] = await Promise.all([api(`/wallet/api/requests/${ctx.request}`), api(`/api/checkout/${ctx.session}`)]);
      return {
        request: `POST /api/checkout/${ctx.session}/pay\nGET /wallet/api/requests/${ctx.request}`,
        response: {
          wallet_request: pick(wallet.request, ['id', 'status', 'amount', 'currency', 'to_address']),
          payment_status: view.payment.status,
        },
      };
    },
  },
  {
    title: 'Покупатель подтверждает перевод в кошельке',
    parts: ['wallet', 'chain'],
    edges: ['page-wallet', 'wallet-chain'],
    text: 'Покупатель выбирает кошелёк Алисы и нажимает «Подтвердить». Кошелёк списывает 25 USDT и отправляет транзакцию в сеть. В жизни это делает MetaMask.',
    async run() {
      const result = await api(`/wallet/api/requests/${ctx.request}/approve`, { method: 'POST', body: { account_id: 'alice' } });
      ctx.tx = result.request.tx_id;
      return {
        request: `POST /wallet/api/requests/${ctx.request}/approve\n\n{ "account_id": "alice" }`,
        response: pick(result.request, ['id', 'status', 'tx_id', 'payer_address']),
        link: { href: '/wallet', text: 'Найти транзакцию в обозревателе сети' },
      };
    },
  },
  {
    title: 'Сеть подтверждает транзакцию',
    parts: ['chain', 'wallet', 'jobs', 'api'],
    edges: ['wallet-chain', 'jobs-api', 'api-wallet'],
    text: 'Блок появляется раз в 3 секунды. Фоновая задача сверки раз в 3 секунды спрашивает кошелёк через порт, сколько подтверждений у транзакции. Платёж засчитывается после трёх подтверждений и только если сумма и валюта совпали.',
    live: true,
    async run(update) {
      const needed = ctx.config.required_confirmations;
      const deadline = Date.now() + 90_000;
      let view;
      for (;;) {
        const [current, chain] = await Promise.all([api(`/api/checkout/${ctx.session}`), api('/wallet/api/explorer')]);
        view = current;
        $('#chain-sub').textContent = `блок № ${chain.height}`;
        const { status, confirmations } = view.payment;
        update(`Блок № ${chain.height} · подтверждений ${Math.min(confirmations ?? 0, needed)} из ${needed} · статус ${status}`);
        if (status === 'succeeded') break;
        if (status === 'requires_payment_method') throw new Error('Транзакция не прошла в сети. Нажмите «Сначала» и попробуйте ещё раз.');
        if (Date.now() > deadline) throw new Error('Сеть не подтвердила транзакцию за полторы минуты. Попробуйте повторить шаг.');
        await sleep(1500);
      }
      return {
        request: `GET /api/checkout/${ctx.session}   (повторяется, пока платёж не станет succeeded)`,
        response: pick(view.payment, ['status', 'confirmations', 'required_confirmations', 'transaction']),
      };
    },
  },
  {
    title: 'Деньги зачислены на баланс продавца',
    parts: ['api', 'db'],
    edges: ['api-db'],
    text: 'В одной транзакции базы платёж становится succeeded, сессия — complete, в журнал баланса пишется проводка за вычетом комиссии 1 %, а события встают в очередь. Записывается либо всё сразу, либо ничего.',
    async run() {
      const [intent, ledger] = await Promise.all([
        asMerchant(`/v1/payment_intents/${ctx.intent}`),
        asMerchant('/v1/balance_transactions?limit=20'),
      ]);
      const entry = ledger.data.find((row) => row.source === ctx.intent);
      return {
        request: `GET /v1/payment_intents/${ctx.intent}\nGET /v1/balance_transactions`,
        response: {
          payment_intent: pick(intent, ['id', 'status', 'amount_received', 'currency']),
          balance_transaction: entry ? pick(entry, ['id', 'type', 'amount', 'fee', 'net']) : null,
        },
        note: entry ? `${usdt(entry.amount)} − комиссия ${usdt(entry.fee)} = ${usdt(entry.net)} на балансе продавца.` : null,
      };
    },
  },
  {
    title: 'Магазин узнаёт об оплате',
    parts: ['jobs', 'shop', 'db'],
    edges: ['jobs-db', 'jobs-shop'],
    text: 'Каждое событие сохраняется в базе вместе с оплатой — это паттерн transactional outbox. Фоновая задача берёт его оттуда, подписывает HMAC-SHA256 и отправляет на адрес вебхука магазина. Если магазин не ответил, отправка повторится через 1, 5, 15, 60 и 360 минут.',
    async run() {
      const events = await asMerchant(`/v1/events?related_object=${ctx.intent}&limit=20`);
      const types = events.data.map((event) => event.type).reverse();
      return {
        request: `GET /v1/events?related_object=${ctx.intent}`,
        response: { events: types },
        note: 'Это события, которые получает магазин. Подписанные вебхуки и их доставку видно в кабинете продавца, во вкладке «Разработчикам».',
      };
    },
  },
];

const list = $('#walk-steps');
const nextButton = $('#walk-next');
const resetButton = $('#walk-reset');
const walkError = $('#walk-error');
let current = -1;
let running = false;

function renderSteps() {
  fill(
    list,
    STEPS.map((step, index) =>
      h(
        'li',
        { class: 'walk-step', dataset: { index: String(index) } },
        h('h3', { class: 'walk-step-title' }, step.title),
        h('div', { class: 'walk-step-body' }),
      ),
    ),
  );
}

function stepNode(index) {
  return list.children[index];
}

function setButton() {
  if (current >= STEPS.length - 1) nextButton.textContent = 'Провести ещё один платёж';
  else if (current < 0) nextButton.textContent = 'Начать платёж';
  else nextButton.textContent = 'Следующий шаг';
  resetButton.hidden = current < 0;
}

async function runStep(index) {
  const step = STEPS[index];
  const node = stepNode(index);
  const body = node.querySelector('.walk-step-body');
  [...list.children].forEach((item, i) => item.classList.toggle('is-current', i === index));
  node.classList.remove('is-failed');
  node.classList.add('is-open');
  highlight(step.parts, step.edges);
  const live = h('p', { class: 'walk-live mono small' }, 'Выполняется…');
  fill(body, h('p', { class: 'walk-text' }, step.text), live);
  node.scrollIntoView({ block: 'nearest', behavior: 'smooth' });

  const result = await step.run((text) => {
    live.textContent = text;
  });
  if (!step.live) live.remove();
  const parts = [
    h('p', { class: 'walk-label small muted' }, 'Запрос'),
    h('pre', { class: 'code walk-code' }, h('code', {}, result.request)),
    h('p', { class: 'walk-label small muted' }, 'Ответ сервера'),
    h('pre', { class: 'code walk-code' }, h('code', {}, JSON.stringify(result.response, null, 2))),
    result.note ? h('p', { class: 'walk-note small' }, result.note) : null,
    result.link ? h('p', { class: 'small' }, h('a', { href: result.link.href, target: '_blank', rel: 'noopener' }, result.link.text)) : null,
  ];
  body.append(...parts.filter(Boolean));
  node.classList.add('is-done');
}

function reset() {
  current = -1;
  Object.keys(ctx).forEach((key) => delete ctx[key]);
  $('#chain-sub').textContent = 'MockChain';
  walkError.hidden = true;
  highlight();
  renderSteps();
  setButton();
}

nextButton.addEventListener('click', async () => {
  if (running) return;
  if (current >= STEPS.length - 1) reset();
  const index = current + 1;
  running = true;
  walkError.hidden = true;
  setBusy(nextButton, true, 'Выполняется…');
  try {
    await runStep(index);
    current = index;
  } catch (failure) {
    walkError.textContent =
      failure instanceof ApiError || failure instanceof Error ? failure.message : 'Не удалось выполнить шаг. Попробуйте ещё раз.';
    walkError.hidden = false;
    stepNode(index).classList.add('is-failed');
  } finally {
    running = false;
    setBusy(nextButton, false);
    setButton();
  }
});

resetButton.addEventListener('click', () => {
  if (!running) reset();
});

reset();
showPart('api');
