# REST API CryptoPay

API повторяет стиль Stripe, чтобы им можно было пользоваться «по памяти»: те же объекты, те же списки, те же ошибки и идемпотентность. Живая документация со всеми схемами и кнопкой «Try it out» — **Swagger UI на `/docs`** (спецификация OpenAPI — `/api/openapi.json`).

Все примеры ниже сняты с настоящего сервера, только id сокращены для читаемости.

## Авторизация

Секретный ключ `sk_test_…` выпускается в кабинете: **Разработчикам → Выпустить ключ**. Ключ показывается один раз; в базе хранится только его SHA-256, поэтому восстановить его нельзя — только выпустить новый (старый при этом перестаёт работать).

Ключ можно передать двумя способами, оба как у Stripe:

```bash
# Bearer
curl https://cryptopay-xxxx.onrender.com/v1/balance -H "Authorization: Bearer sk_test_…"

# Basic: ключ — логин, пароль пустой (обратите внимание на двоеточие)
curl https://cryptopay-xxxx.onrender.com/v1/balance -u sk_test_…:
```

Методы `/v1` принимают и токен сессии кабинета — так личный кабинет пользуется тем же API, что и магазины.

## Деньги

Суммы — **целые числа в минимальных единицах валюты**, в JSON — строкой (числа больше 2⁵³ JavaScript не умеет хранить точно, а 1 ETH = 10¹⁸ wei):

| Валюта | `currency` | Знаков | 1 монета = | Минимум |
|---|---|---|---|---|
| Tether | `usdt` | 6 | `1000000` | `500000` (0.50) |
| USD Coin | `usdc` | 6 | `1000000` | `500000` (0.50) |
| Bitcoin | `btc` | 8 | `100000000` | `1000` (0.00001) |
| Ether | `eth` | 18 | `1000000000000000000` | `100000000000000` (0.0001) |

## Методы

### Страницы оплаты — Checkout Sessions

| Метод | Путь | Что делает |
|---|---|---|
| `POST` | `/v1/checkout/sessions` | Создать страницу оплаты (вместе с ней создаётся PaymentIntent) |
| `GET` | `/v1/checkout/sessions` | Список, новые сверху |
| `GET` | `/v1/checkout/sessions/{id}` | Получить сессию |
| `POST` | `/v1/checkout/sessions/{id}/expire` | Закрыть страницу досрочно (платёж отменяется) |

Параметры создания:

| Поле | Обязательное | Описание |
|---|---|---|
| `amount` | да | Сумма в минимальных единицах, строкой или числом |
| `currency` | да | `usdt`, `usdc`, `btc` или `eth` |
| `description` | нет | Что покупают — видно покупателю (до 500 символов) |
| `success_url` | нет | Куда вернуть покупателя после оплаты. `{CHECKOUT_SESSION_ID}` заменится на id сессии |
| `cancel_url` | нет | Ссылка «Вернуться в магазин» |
| `metadata` | нет | До 20 пар «ключ — значение» для ваших нужд, например `order_id` |
| `expires_in_minutes` | нет | Срок жизни страницы: от 5 до 1440 минут, по умолчанию 60 |

### Платежи — Payment Intents

| Метод | Путь | Что делает |
|---|---|---|
| `GET` | `/v1/payment_intents` | Список; фильтр `?status=succeeded` |
| `GET` | `/v1/payment_intents/{id}` | Получить платёж |
| `POST` | `/v1/payment_intents/{id}/cancel` | Отменить (если ещё не оплачен) |

### Возвраты — Refunds

| Метод | Путь | Что делает |
|---|---|---|
| `POST` | `/v1/refunds` | Вернуть деньги: `payment_intent`, необязательно `amount` и `reason` |
| `GET` | `/v1/refunds` | Список; фильтр `?payment_intent=pi_…` |
| `GET` | `/v1/refunds/{id}` | Получить возврат |

Без `amount` возвращается весь остаток. Можно делать несколько частичных возвратов, пока сумма не исчерпана. `reason` — `requested_by_customer`, `duplicate` или `fraudulent`.

### Баланс

| Метод | Путь | Что делает |
|---|---|---|
| `GET` | `/v1/balance` | Доступный баланс по каждой валюте |
| `GET` | `/v1/balance_transactions` | Журнал проводок: оплаты, возвраты, отменённые возвраты |

### События и вебхуки

| Метод | Путь | Что делает |
|---|---|---|
| `GET` | `/v1/events` | Журнал событий; фильтр `?type=checkout.session.completed` |
| `GET` | `/v1/events/{id}` | Событие со снимком объекта |
| `GET` | `/v1/events/{id}/deliveries` | Попытки доставки этого события на ваши адреса |
| `POST` | `/v1/webhook_endpoints` | Добавить адрес; в ответе — секрет `whsec_…` (показывается один раз) |
| `GET` | `/v1/webhook_endpoints` | Ваши адреса (до 5) |
| `DELETE` | `/v1/webhook_endpoints/{id}` | Удалить адрес |
| `POST` | `/v1/webhook_endpoints/{id}/test` | Отправить тестовое событие `webhook_endpoint.test` |

### Служебные и внутренние методы

Эти методы нужны интерфейсу, а не магазинам, поэтому в Swagger они в отдельных разделах:

| Путь | Для чего |
|---|---|
| `POST /api/auth/register`, `POST /api/auth/login` | Регистрация и вход в кабинет (возвращают JWT) |
| `GET/PATCH /api/account`, `POST /api/account/api_key` | Аккаунт и выпуск ключа |
| `GET /api/dashboard/overview` | Главная кабинета: баланс, оборот за 7 дней, платежи по статусам |
| `GET /api/checkout/{id}`, `POST /api/checkout/{id}/pay` | Данные для страницы оплаты и кнопка «Оплатить» — без ключа, по id сессии |
| `POST /api/demo/checkout` | Кнопка «Попробовать оплату» на главной: страница на 25 USDT от демо-магазина |
| `GET /healthz` | Проверка работоспособности для Render и Docker |
| `/wallet/…` | Страницы и API имитатора кошелька; это «чужая система», в Swagger её нет |

## Пример: полный путь платежа

**1. Сервер магазина создаёт страницу оплаты:**

```bash
curl https://cryptopay-xxxx.onrender.com/v1/checkout/sessions \
  -u sk_test_…: \
  -H "Idempotency-Key: order-1042" \
  -H "Content-Type: application/json" \
  -d '{
        "amount": "25000000",
        "currency": "usdt",
        "description": "Худи «Пиксель»",
        "success_url": "https://shop.example/thanks?session={CHECKOUT_SESSION_ID}",
        "cancel_url": "https://shop.example/cart",
        "metadata": {"order_id": "1042"}
      }'
```

```json
{
  "id": "cs_w5hZ2WmXUSTI…",
  "object": "checkout.session",
  "url": "https://cryptopay-xxxx.onrender.com/checkout/cs_w5hZ2WmXUSTI…",
  "status": "open",
  "payment_status": "unpaid",
  "payment_intent": "pi_sMhAfVnQ018B…",
  "amount_total": "25000000",
  "currency": "usdt",
  "description": "Худи «Пиксель»",
  "success_url": "https://shop.example/thanks?session={CHECKOUT_SESSION_ID}",
  "cancel_url": "https://shop.example/cart",
  "created": 1794219532,
  "expires_at": 1794223132,
  "completed_at": null,
  "livemode": false
}
```

Магазин перенаправляет покупателя на `url`. Повтор этого же запроса с тем же `Idempotency-Key` вернёт тот же ответ и заголовок `Idempotent-Replayed: true` — вторая страница оплаты не создастся.

**2. Покупатель платит** на странице оплаты, подтверждает в кошельке, ждёт 3 подтверждения.

**3. Платёж после оплаты** (`GET /v1/payment_intents/pi_…`):

```json
{
  "id": "pi_sMhAfVnQ018B…",
  "object": "payment_intent",
  "amount": "25000000",
  "amount_received": "25000000",
  "amount_refunded": "0",
  "currency": "usdt",
  "status": "succeeded",
  "description": "Худи «Пиксель»",
  "metadata": {"order_id": "1042"},
  "checkout_session": "cs_w5hZ2WmXUSTI…",
  "customer_email": "buyer@example.com",
  "payment_method": {
    "type": "crypto_wallet",
    "wallet_request": "wpr_obUwlQhhGymy…",
    "transaction": "0x62840a12d36666f8e1119f21cd61c1a0c99d5698b2725b73ca75ff70f7f3d865",
    "payer_address": "0x376ae29277390a4e9f08cf7a54607245445e2419",
    "confirmations": 3,
    "required_confirmations": 3
  },
  "last_payment_error": null,
  "cancellation_reason": null,
  "created": 1794219532,
  "succeeded_at": 1794219546,
  "canceled_at": null,
  "livemode": false
}
```

После оплаты у сессии `url` становится `null` — страница больше не принимает оплату, как у Stripe.

**4. Магазин получает вебхук** `checkout.session.completed` (см. ниже) и отмечает заказ 1042 оплаченным.

**5. Частичный возврат 5 USDT:**

```bash
curl https://cryptopay-xxxx.onrender.com/v1/refunds -u sk_test_…: \
  -H "Idempotency-Key: refund-1042-1" -H "Content-Type: application/json" \
  -d '{"payment_intent": "pi_sMhAfVnQ018B…", "amount": "5000000", "reason": "requested_by_customer"}'
```

```json
{
  "id": "re_4NRcMtfXYzpu…",
  "object": "refund",
  "amount": "5000000",
  "currency": "usdt",
  "payment_intent": "pi_sMhAfVnQ018B…",
  "reason": "requested_by_customer",
  "status": "pending",
  "destination_address": "0x376ae29277390a4e9f08cf7a54607245445e2419",
  "transaction": "0x6aec4ad3eb97…",
  "failure_reason": null,
  "created": 1794219549,
  "completed_at": null
}
```

Через несколько блоков статус станет `succeeded`, и придёт событие `refund.succeeded`.

**6. Журнал баланса** (`GET /v1/balance_transactions`):

```json
{
  "object": "list",
  "url": "/v1/balance_transactions",
  "has_more": false,
  "data": [
    {"id": "txn_ZEok…", "object": "balance_transaction", "type": "refund",  "source": "re_4NRc…",
     "amount": "-5000000", "fee": "0",      "net": "-5000000", "currency": "usdt", "description": "Возврат"},
    {"id": "txn_8QLI…", "object": "balance_transaction", "type": "payment", "source": "pi_sMhA…",
     "amount": "25000000", "fee": "250000", "net": "24750000", "currency": "usdt", "description": "Оплата"}
  ]
}
```

Комиссия платформы — 1 % (`PLATFORM_FEE_BPS=100`), при возврате она не возвращается. Баланс — сумма `net`: 24.75 − 5 = **19.75 USDT**.

## Списки и пагинация

Все списки — объект `list`, как у Stripe:

```json
{"object": "list", "url": "/v1/payment_intents", "has_more": true, "data": [ … ]}
```

| Параметр | Описание |
|---|---|
| `limit` | Сколько записей вернуть: 1–100, по умолчанию 10 |
| `starting_after` | id последней записи предыдущей страницы — получить следующую |

Пагинация курсорная, по `(created, id)`: если в начало списка добавятся новые платежи, вторая страница не «съедет» и записи не задвоятся.

## Ошибки

Все ошибки — в одном формате:

```json
{"error": {"type": "invalid_request_error", "code": "amount_too_small",
           "message": "Минимальная сумма платежа в USDT — 500000 минимальных единиц", "param": "amount"}}
```

| HTTP | `type` | Примеры `code` | Когда |
|---|---|---|---|
| 400 | `invalid_request_error` | `parameter_missing`, `parameter_invalid` (в том числе неподдерживаемая валюта), `amount_too_small`, `amount_too_large`, `payment_intent_unexpected_state`, `checkout_session_inactive`, `balance_insufficient`, `too_many_endpoints` | Неверные параметры или нарушено бизнес-правило |
| 400 | `idempotency_error` | `idempotency_key_reused` | Тот же ключ, но другие параметры |
| 401 | `authentication_error` | `authentication_required`, `invalid_api_key`, `session_expired` | Нет ключа, ключ неверный или сессия кабинета закончилась |
| 404 | `invalid_request_error` | `resource_missing` | Объекта нет — или он принадлежит другому продавцу |
| 409 | `idempotency_error` / `invalid_request_error` | `idempotency_key_in_use`, `email_taken`, `lock_timeout` | Конфликт: параллельный запрос, занятый e-mail |
| 413 | `invalid_request_error` | `request_too_large` | Тело больше 32 КБ |
| 429 | `rate_limit_error` | `rate_limit` | Слишком много запросов (заголовок `Retry-After`) |
| 503 | `api_error` | `wallet_unavailable` | Кошелёк не отвечает — можно повторить |

Чужой объект возвращает **404, а не 403**: так нельзя по ответу понять, существует ли платёж с таким id у другого продавца.

## Вебхуки

### События

| Событие | Когда |
|---|---|
| `checkout.session.completed` | Страница оплачена — **главное событие для магазина** |
| `checkout.session.expired` | Срок страницы истёк или её закрыли |
| `payment_intent.created` | Платёж создан |
| `payment_intent.requires_action` | Покупатель ушёл подтверждать в кошелёк |
| `payment_intent.processing` | Транзакция отправлена в сеть |
| `payment_intent.succeeded` | Деньги получены |
| `payment_intent.payment_failed` | Попытка не удалась (покупатель может повторить) |
| `payment_intent.canceled` | Платёж отменён |
| `refund.created` / `refund.succeeded` / `refund.failed` | Жизнь возврата |

При добавлении адреса можно подписаться на все события (`["*"]`) или выбрать нужные.

### Что приходит

```http
POST /webhook HTTP/1.1
content-type: application/json
user-agent: CryptoPay-Webhooks/2.0
cryptopay-event: checkout.session.completed
cryptopay-delivery: whd_OcDotF23uTbD…
cryptopay-signature: t=1794219546,v1=5f0c2a…e91

{"id":"evt_g3w81tLLALMo…","object":"event","type":"checkout.session.completed","api_version":"2026-11-01",
 "created":1794219546,"data":{"object":{"id":"cs_w5hZ2WmXUSTI…","object":"checkout.session","status":"complete",
 "payment_status":"paid","payment_intent":"pi_sMhAfVnQ018B…","amount_total":"25000000","currency":"usdt", …}},
 "livemode":false}
```

### Как проверить подпись

`v1` = hex(HMAC-SHA256(секрет `whsec_…`, `"<t>.<сырое тело запроса>"`)). Python, только стандартная библиотека:

```python
import hashlib, hmac, time

def verify(header: str, raw_body: bytes, secret: str, tolerance: int = 300) -> bool:
    parts = dict(item.split("=", 1) for item in header.split(",") if "=" in item)
    timestamp = int(parts["t"])
    if abs(time.time() - timestamp) > tolerance:      # старая подпись — возможен replay
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts["v1"])  # сравнение за постоянное время
```

Три правила для магазина:

1. Подписывается **сырое тело**. Не разбирайте JSON и не собирайте его заново перед проверкой — порядок ключей и пробелы изменятся, подпись не сойдётся.
2. **Не доверяйте возврату покупателя на `success_url`**: эту страницу можно открыть и без оплаты. Заказ оплачен, только когда пришёл подписанный вебхук (или когда вы сами запросили сессию через API).
3. Одно событие может прийти **повторно** (например, если ваш сервер ответил слишком поздно). Запоминайте `id` обработанных событий — так сделано в `examples/shop.py`.

### Повторы

Ответ 2xx — доставлено. Любой другой ответ, тайм-аут или ошибка соединения — повтор через **1 мин → 5 мин → 15 мин → 1 ч → 6 ч**. После шестой неудачной попытки доставка получает статус `failed`. Историю попыток видно в кабинете и через `GET /v1/events/{id}/deliveries`:

```json
{
  "id": "whd_OcDotF23uTbD…",
  "object": "webhook_delivery",
  "event": "evt_g3w81tLLALMo…",
  "url": "http://localhost:4000/webhook",
  "status": "pending",
  "attempts": 1,
  "next_attempt_at": 1794219606,
  "last_error": "Не удалось подключиться: All connection attempts failed",
  "last_status_code": null,
  "delivered_at": null
}
```

## Готовые примеры

- `examples/shop.py` — магазин на стандартной библиотеке Python: создаёт сессию, принимает вебхук, проверяет подпись, отмечает заказ. Его же запускают сквозные тесты.
- `examples/create-session.sh` — создание страницы оплаты одной командой `curl`.
