// Форматирование денег, дат и статусов. Деньги считаем в BigInt: у Number всего ~15 значащих цифр,
// а 1 ETH = 10^18 wei. Ни одного float в денежной арифметике.

import { h } from './ui.js';

/** "25000000", 6 → "25.00"; дробную часть показываем минимум с двумя знаками. */
export function formatUnits(value, decimals, { minFraction = 2, maxFraction = 8 } = {}) {
  const amount = BigInt(value);
  const negative = amount < 0n;
  const abs = negative ? -amount : amount;
  const base = 10n ** BigInt(decimals);
  const whole = (abs / base).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
  let fraction = (abs % base).toString().padStart(decimals, '0').replace(/0+$/, '');
  if (fraction.length < minFraction && decimals >= minFraction) fraction = fraction.padEnd(minFraction, '0');
  if (fraction.length > maxFraction) fraction = fraction.slice(0, maxFraction);
  return `${negative ? '−' : ''}${whole}${fraction ? `,${fraction}` : ''}`;
}

/** "12,5" или "12.5" → "12500000" (строкой, без потери точности). null — если ввод некорректный. */
export function toMinorUnits(text, decimals) {
  const normalized = String(text).trim().replace(',', '.');
  if (!/^[0-9]+(\.[0-9]+)?$/.test(normalized)) return null;
  const [whole, fraction = ''] = normalized.split('.');
  if (fraction.length > decimals) return null;
  const value = BigInt(whole) * 10n ** BigInt(decimals) + BigInt(fraction.padEnd(decimals, '0') || '0');
  return value > 0n ? value.toString() : null;
}

export function money(amount, currency, currencies) {
  const info = currencies?.find((c) => c.code === currency);
  const decimals = info?.decimals ?? 6;
  return `${formatUnits(amount, decimals)} ${currency.toUpperCase()}`;
}

const dateTime = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
const timeOnly = new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const dayOnly = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short' });

export const formatDateTime = (unix) => (unix ? dateTime.format(new Date(unix * 1000)) : '—');
export const formatTime = (unix) => (unix ? timeOnly.format(new Date(unix * 1000)) : '—');
export const formatDay = (isoDate) => dayOnly.format(new Date(`${isoDate}T12:00:00Z`));

export const shortHash = (hash) => (hash ? `${hash.slice(0, 10)}…${hash.slice(-6)}` : '');
export const shortAddress = (address) => (address ? `${address.slice(0, 6)}…${address.slice(-4)}` : '');

export function formatCountdown(ms) {
  if (ms <= 0) return '0:00';
  const total = Math.floor(ms / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return hours > 0
    ? `${hours} ч ${String(minutes).padStart(2, '0')} мин`
    : `${minutes}:${String(seconds).padStart(2, '0')}`;
}

export const INTENT_STATUS = {
  requires_payment_method: 'Ожидает оплаты',
  requires_action: 'Ждёт подтверждения в кошельке',
  processing: 'Подтверждается в сети',
  succeeded: 'Оплачен',
  canceled: 'Отменён',
};

/** Бейдж статуса платежа. Неудачная попытка показывается отдельно: «Не прошёл». */
export function intentBadge(intent) {
  if (intent.status === 'requires_payment_method' && intent.last_payment_error) {
    return h('span', { class: 'status status--failed' }, 'Попытка не прошла');
  }
  if (intent.status === 'succeeded' && intent.amount_refunded !== '0') {
    const full = intent.amount_refunded === intent.amount_received;
    return h('span', { class: 'status' }, full ? 'Возвращён' : 'Частично возвращён');
  }
  return h('span', { class: `status status--${intent.status}` }, INTENT_STATUS[intent.status] ?? intent.status);
}

export const REFUND_STATUS = { pending: 'Отправляется', succeeded: 'Возвращён', failed: 'Не прошёл' };
export const SESSION_STATUS = { open: 'Открыта', complete: 'Оплачена', expired: 'Истекла' };
export const DELIVERY_STATUS = { pending: 'В очереди', delivered: 'Доставлен', failed: 'Не доставлен' };

export function badge(status, labels) {
  return h('span', { class: `status status--${status}` }, labels[status] ?? status);
}

export const EVENT_LABELS = {
  'payment_intent.created': 'Платёж создан',
  'payment_intent.requires_action': 'Отправлен запрос в кошелёк покупателя',
  'payment_intent.processing': 'Покупатель подтвердил, транзакция в сети',
  'payment_intent.succeeded': 'Платёж успешен',
  'payment_intent.payment_failed': 'Попытка оплаты не прошла',
  'payment_intent.canceled': 'Платёж отменён',
  'checkout.session.completed': 'Страница оплаты завершена',
  'checkout.session.expired': 'Страница оплаты истекла',
  'refund.created': 'Создан возврат',
  'refund.succeeded': 'Возврат дошёл до покупателя',
  'refund.failed': 'Возврат не прошёл',
  'webhook_endpoint.test': 'Тестовое событие',
};
