// Страница кошелька: покупатель выбирает тестовый кошелёк и подтверждает (или отклоняет) платёж.
// В настоящей жизни это было бы окно расширения MetaMask.

import { api } from './api.js';
import { formatCountdown, formatUnits, shortAddress, shortHash } from './format.js';
import { $, fill, h, setBusy, sleep } from './ui.js';

const requestId = location.pathname.split('/').filter(Boolean).pop();
let data = null;

const RESULT_TEXT = {
  approved: ['✓', 'Платёж подтверждён', 'Транзакция отправлена в сеть. Возвращаем вас в магазин…'],
  rejected: ['×', 'Вы отклонили платёж', 'Деньги не списаны. Возвращаем вас в магазин…'],
  failed: ['!', 'Платёж не прошёл', ''],
  expired: ['!', 'Время на подтверждение истекло', 'Вернитесь в магазин и начните оплату заново.'],
  canceled: ['!', 'Магазин отменил запрос', 'Эта оплата больше не нужна.'],
};

function show(id) {
  for (const screen of ['screen-loading', 'screen-request', 'screen-result']) $(`#${screen}`).hidden = screen !== id;
}

function showResult(request, { redirect = false } = {}) {
  const [mark, title, text] = RESULT_TEXT[request.status] ?? ['!', 'Запрос уже обработан', ''];
  const markNode = $('#result-mark');
  markNode.textContent = mark;
  markNode.classList.toggle('is-bad', request.status !== 'approved');
  $('#result-title').textContent = title;
  $('#result-text').textContent = request.status === 'failed' ? request.failure_message : text;
  $('#result-tx').textContent = request.tx_id ? `Транзакция ${shortHash(request.tx_id)}` : '';
  $('#result-back').href = request.return_url;
  show('screen-result');
  if (redirect && ['approved', 'rejected'].includes(request.status)) {
    sleep(1600).then(() => location.assign(request.return_url));
  }
}

function renderRequest() {
  const { request, accounts, decimals } = data;
  const places = decimals[request.currency];
  $('#height').textContent = `#${data.height}`;
  $('#merchant').textContent = request.merchant_name;
  $('#amount').textContent = `${formatUnits(request.amount, places)} ${request.currency.toUpperCase()}`;
  $('#description').textContent = request.description ?? '';
  $('#recipient').textContent = shortAddress(request.to_address);
  $('#recipient').title = request.to_address;

  fill(
    $('#accounts'),
    accounts.map((account, index) =>
      h(
        'label',
        { class: 'account' },
        h('input', { type: 'radio', name: 'account', value: account.id, checked: index === 0 }),
        h('span', { class: 'account-name' }, account.name),
        h(
          'span',
          { class: 'account-balance' },
          `${formatUnits(account.balances[request.currency] ?? '0', places)} ${request.currency.toUpperCase()}`,
        ),
        h('span', { class: 'account-outcome' }, account.outcome),
      ),
    ),
  );
  show('screen-request');
  updateExpiry();
}

function updateExpiry() {
  if (!data || data.request.status !== 'pending') return;
  const left = Date.parse(data.request.expires_at) - Date.now();
  $('#expiry').textContent = left > 0 ? `Запрос действует ещё ${formatCountdown(left)}` : 'Время на подтверждение истекло';
}

async function decide(action) {
  const approve = $('#approve');
  const reject = $('#reject');
  const button = action === 'approve' ? approve : reject;
  approve.disabled = true;
  reject.disabled = true;
  setBusy(button, true, action === 'approve' ? 'Подписываем…' : 'Отклоняем…');
  const account = document.querySelector('input[name="account"]:checked')?.value;
  try {
    const result = await api(`/wallet/api/requests/${encodeURIComponent(requestId)}/${action}`, {
      method: 'POST',
      body: action === 'approve' ? { account_id: account } : undefined,
    });
    showResult(result.request, { redirect: true });
  } catch (error) {
    setBusy(button, false);
    approve.disabled = false;
    reject.disabled = false;
    await load(); // запрос мог истечь или уже быть обработан в другой вкладке
    if (data?.request.status === 'pending') alertInline(error.message);
  }
}

function alertInline(message) {
  const note = h('p', { class: 'dim small request-expiry', role: 'alert' }, message);
  $('#screen-request').append(note);
  setTimeout(() => note.remove(), 5000);
}

async function load() {
  try {
    data = await api(`/wallet/api/requests/${encodeURIComponent(requestId)}`);
  } catch (error) {
    $('#result-mark').textContent = '?';
    $('#result-title').textContent = 'Запрос не найден';
    $('#result-text').textContent = error.message;
    $('#result-back').href = '/';
    show('screen-result');
    return;
  }
  if (data.request.status === 'pending') renderRequest();
  else showResult(data.request);
}

$('#screen-request').addEventListener('submit', (event) => {
  event.preventDefault();
  decide('approve');
});
$('#reject').addEventListener('click', () => decide('reject'));
setInterval(updateExpiry, 1000);
load();
