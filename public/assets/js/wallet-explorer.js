// Обозреватель имитируемой сети: блоки, тестовые кошельки и транзакции. Обновляется с каждым блоком.

import { api } from './api.js';
import { formatTime, formatUnits, shortAddress, shortHash } from './format.js';
import { $, fill, h, toast } from './ui.js';

const KIND = { payment: 'Оплата', refund: 'Возврат' };
const STATUS = { pending: 'В мемпуле', confirmed: 'Подтверждена', failed: 'Провалилась' };
let lastHeight = null;
let names = {};

function amount(value, currency, decimals) {
  return `${formatUnits(value, decimals[currency] ?? 6)} ${currency.toUpperCase()}`;
}

function render(data) {
  const { height, decimals } = data;
  names = Object.fromEntries(data.accounts.map((a) => [a.address, a.name]));
  $('#chain-info').textContent = `Высота ${height}. Новый блок каждые ${data.block_time_seconds} с.`;

  const perBlock = {};
  for (const tx of data.transactions) perBlock[tx.included_height] = (perBlock[tx.included_height] ?? 0) + 1;
  const shown = Math.min(height + 1, 8);
  const isNew = lastHeight !== null && height > lastHeight;
  fill(
    $('#blocks'),
    Array.from({ length: shown }, (_, i) => height - i).map((n) =>
      h(
        'li',
        { class: `block${n === height ? ' is-latest' : ''}${isNew && n === height ? ' is-new' : ''}` },
        h('div', { class: 'block-height' }, `#${n}`),
        h('div', { class: 'block-txs' }, perBlock[n] ? `транзакций: ${perBlock[n]}` : 'пустой'),
      ),
    ),
  );
  lastHeight = height;

  fill(
    $('#accounts'),
    data.accounts.map((a) =>
      h(
        'tr',
        {},
        h('td', {}, a.name),
        h('td', { class: 'mono', title: a.address }, shortAddress(a.address)),
        h('td', { class: 'dim' }, a.outcome),
        ['usdt', 'usdc', 'btc', 'eth'].map((c) =>
          h('td', { class: 'amount' }, formatUnits(a.balances[c] ?? '0', decimals[c], { minFraction: 0, maxFraction: 6 })),
        ),
      ),
    ),
  );

  $('#tx-empty').hidden = data.transactions.length > 0;
  fill(
    $('#transactions'),
    data.transactions.map((tx) => {
      const status = tx.status === 'confirmed' ? `${STATUS.confirmed} (${tx.confirmations})` : STATUS[tx.status];
      return h(
        'tr',
        {},
        h('td', { class: 'mono', title: tx.id }, shortHash(tx.id)),
        h('td', {}, KIND[tx.kind] ?? tx.kind),
        h('td', { title: tx.from_address }, names[tx.from_address] ?? shortAddress(tx.from_address)),
        h('td', { title: tx.to_address }, names[tx.to_address] ?? shortAddress(tx.to_address)),
        h('td', { class: 'amount' }, amount(tx.amount, tx.currency, decimals)),
        h('td', { class: 'num' }, `#${tx.included_height}`, h('span', { class: 'dim' }, ` ${formatTime(Date.parse(tx.created_at) / 1000)}`)),
        h('td', { class: `tx-status tx-status--${tx.status}`, title: tx.failure_reason ?? '' }, status),
      );
    }),
  );
  return data.block_time_seconds;
}

async function tick() {
  let delay = 3;
  try {
    delay = render(await api('/wallet/api/explorer'));
  } catch (error) {
    toast(error.message, 'error');
  }
  setTimeout(tick, Math.max(1, delay) * 1000);
}

$('#reset').addEventListener('click', async () => {
  if (!window.confirm('Вернуть все тестовые балансы к исходным и очистить историю транзакций?')) return;
  try {
    await api('/wallet/api/reset', { method: 'POST' });
    toast('Тестовые балансы сброшены');
  } catch (error) {
    toast(error.message, 'error');
  }
});

tick();
