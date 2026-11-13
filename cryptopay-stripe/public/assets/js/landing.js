// Главная: кнопка «Попробовать оплату» создаёт страницу оплаты от демо-магазина и переводит на неё.

import { api } from './api.js';
import { $, setBusy } from './ui.js';

$('#try-demo').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  const error = $('#demo-error');
  error.hidden = true;
  setBusy(button, true, 'Создаём страницу оплаты…');
  try {
    const { redirect_url: url } = await api('/api/demo/checkout', { method: 'POST' });
    location.assign(url);
  } catch (failure) {
    setBusy(button, false);
    error.textContent =
      failure.code === 'resource_missing' ? 'Демо-аккаунт на этом сервере выключен (DEMO_ACCOUNT=false).' : failure.message;
    error.hidden = false;
  }
});
