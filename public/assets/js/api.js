// Тонкая обёртка над fetch. Ошибки сервера приходят в формате Stripe:
// { error: { type, code, message, param } }.

const TOKEN_KEY = 'cryptopay.session';

export class ApiError extends Error {
  constructor(status, body) {
    super(body?.error?.message ?? `Сервер ответил с ошибкой ${status}`);
    this.status = status;
    this.type = body?.error?.type ?? 'api_error';
    this.code = body?.error?.code ?? 'http_error';
    this.param = body?.error?.param ?? null;
  }
}

export const session = {
  get() {
    try {
      return localStorage.getItem(TOKEN_KEY);
    } catch {
      return null;
    }
  },
  set(token) {
    try {
      localStorage.setItem(TOKEN_KEY, token);
    } catch {
      /* приватный режим браузера — сессия проживёт до перезагрузки страницы */
    }
  },
  clear() {
    try {
      localStorage.removeItem(TOKEN_KEY);
    } catch {
      /* ignore */
    }
  },
};

export async function api(path, { method = 'GET', body, auth = false, headers: extra = {} } = {}) {
  const headers = { accept: 'application/json', ...extra };
  if (body !== undefined) headers['content-type'] = 'application/json';
  if (auth) {
    const token = session.get();
    if (token) headers.authorization = `Bearer ${token}`;
  }
  let response;
  try {
    response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, {
      error: { code: 'network', message: 'Нет связи с сервером. Проверьте интернет и повторите попытку.' },
    });
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(response.status, data);
  return data;
}

let configPromise = null;
/** Настройки сервера (валюты, число подтверждений, демо-аккаунт) — не меняются, пока страница открыта. */
export function loadConfig() {
  configPromise ??= api('/api/config');
  return configPromise;
}
