// Страница оплаты для покупателя (как Stripe Checkout).
// Состояние страницы целиком определяется ответом GET /api/checkout/{id}: сервер на каждый запрос
// сверяет платёж с кошельком, а мы просто показываем нужный экран и опрашиваем, пока ждём сеть.

import { api } from './api.js';
import { formatCountdown, formatUnits, shortAddress, shortHash } from './format.js';
import { $, setBusy } from './ui.js';

const sessionId = location.pathname.split('/').filter(Boolean).pop();
const STATES = ['pay', 'wallet', 'processing', 'paid', 'closed', 'error'];
let current = null;
let pollTimer = null;
let wasPaid = false;

function show(name) {
  for (const state of STATES) $(`#state-${state}`).hidden = state !== name;
  $('#checkout').setAttribute('aria-busy', 'false');
}

function amountText(c) {
  return `${formatUnits(c.amount, c.decimals)} ${c.currency_symbol}`;
}

function renderSummary(c) {
  document.title = `Оплата ${amountText(c)} — ${c.business_name}`;
  $('#merchant-name').textContent = c.business_name;
  $('#order-description').textContent = c.description ?? '';
  $('#order-amount').textContent = formatUnits(c.amount, c.decimals);
  $('#order-currency').textContent = c.currency_symbol;
  $('#order-currency-name').textContent = `${c.currency_name} в тестовой сети MockChain`;
  const back = $('#back-link');
  back.hidden = !c.cancel_url || c.payment.status === 'succeeded';
  if (c.cancel_url) back.href = c.cancel_url;
}

function render(c) {
  current = c;
  renderSummary(c);
  updateCountdown();
  const p = c.payment;

  if (p.status === 'succeeded') return renderPaid(c);
  if (c.status !== 'open' || p.status === 'canceled') return renderClosed(c);
  if (p.status === 'processing') return renderProcessing(c);
  if (p.status === 'requires_action' && p.approve_url) {
    $('#wallet-link').href = p.approve_url;
    return show('wallet');
  }

  // requires_payment_method: форма; если прошлая попытка не удалась — объясняем почему
  const error = $('#pay-error');
  error.hidden = !p.last_payment_error;
  error.textContent = p.last_payment_error ? `${p.last_payment_error.message}. Попробуйте ещё раз или выберите другой кошелёк.` : '';
  $('#pay-button').textContent = p.last_payment_error ? `Попробовать снова — ${amountText(c)}` : `Оплатить ${amountText(c)}`;
  show('pay');
}

function renderProcessing(c) {
  const p = c.payment;
  const done = Math.min(p.confirmations, p.required_confirmations);
  const progress = $('#progress');
  progress.setAttribute('aria-valuemax', String(p.required_confirmations));
  progress.setAttribute('aria-valuenow', String(done));
  $('#progress-bar').style.width = `${Math.max(6, (done / p.required_confirmations) * 100)}%`;
  $('#progress-label').textContent = `Подтверждений: ${done} из ${p.required_confirmations}`;
  $('#processing-tx').textContent = shortHash(p.transaction);
  $('#processing-tx').title = p.transaction ?? '';
  show('processing');
}

function renderPaid(c) {
  const p = c.payment;
  $('#paid-amount').textContent = amountText(c);
  $('#paid-tx').textContent = shortHash(p.transaction);
  $('#paid-tx').title = p.transaction ?? '';
  $('#paid-payer').textContent = shortAddress(p.payer_address);
  $('#paid-payer').title = p.payer_address ?? '';
  const stamp = $('#stamp');
  stamp.hidden = false;
  $('.slip').classList.add('is-paid');
  if (!wasPaid) {
    stamp.classList.add('is-landing'); // печать «падает» один раз — в момент, когда оплата прошла
    wasPaid = true;
  }
  const link = $('#success-link');
  if (c.success_url) {
    link.href = c.success_url;
    link.hidden = false;
    startRedirect(c.success_url);
  }
  show('paid');
}

function renderClosed(c) {
  const canceled = c.payment.status === 'canceled' && c.status !== 'expired';
  $('#closed-title').textContent = canceled ? 'Платёж отменён магазином' : 'Срок оплаты истёк';
  $('#closed-text').textContent = 'Если вы всё ещё хотите оплатить, попросите магазин прислать новую ссылку.';
  show('closed');
}

let redirectStarted = false;
function startRedirect(url) {
  if (redirectStarted) return;
  redirectStarted = true;
  const note = $('#redirect-note');
  note.hidden = false;
  let left = 5;
  const tick = () => {
    note.textContent = `Через ${left} с вернём вас в магазин.`;
    if (left-- <= 0) location.assign(url);
    else setTimeout(tick, 1000);
  };
  tick();
}

function updateCountdown() {
  if (!current || current.status !== 'open') return;
  const left = current.expires_at * 1000 - Date.now();
  $('#expires').textContent = left > 0 ? `Страница действует ещё ${formatCountdown(left)}` : 'Срок оплаты истёк';
}

async function refresh() {
  try {
    render(await api(`/api/checkout/${encodeURIComponent(sessionId)}`));
  } catch (error) {
    if (!current) {
      $('#merchant-name').textContent = 'CryptoPay';
      $('#load-error').textContent =
        error.status === 404 ? 'Такой страницы оплаты нет. Проверьте ссылку.' : error.message;
      show('error');
    }
  }
  schedule();
}

function schedule() {
  clearTimeout(pollTimer);
  const status = current?.payment.status;
  const waiting = status === 'processing' || status === 'requires_action';
  if (current && (current.status === 'open' || waiting)) pollTimer = setTimeout(refresh, waiting ? 2000 : 15000);
}

$('#pay-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('#pay-button');
  const email = $('#email');
  email.removeAttribute('aria-invalid');
  setBusy(button, true, 'Открываем кошелёк…');
  try {
    const result = await api(`/api/checkout/${encodeURIComponent(sessionId)}/pay`, {
      method: 'POST',
      body: { customer_email: email.value.trim() || null },
    });
    location.assign(result.redirect_url); // как редирект на страницу банка при 3-D Secure
  } catch (error) {
    setBusy(button, false);
    if (error.param === 'email') email.setAttribute('aria-invalid', 'true');
    const box = $('#pay-error');
    box.textContent = error.message;
    box.hidden = false;
    refresh();
  }
});

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') refresh();
});
setInterval(updateCountdown, 1000);
refresh();
