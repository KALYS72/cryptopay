"""Пример МАГАЗИНА, который принимает оплату через CryptoPay — как интеграция со Stripe Checkout.

Только стандартная библиотека Python, без зависимостей:

    CRYPTOPAY_URL=http://localhost:3000 \\
    CRYPTOPAY_API_KEY=sk_test_… \\
    WEBHOOK_SECRET=whsec_… \\
    python examples/shop.py

Что делает магазин:

1. ``POST /buy`` — сервер магазина создаёт Checkout Session секретным ключом (с Idempotency-Key)
   и перенаправляет покупателя на страницу оплаты CryptoPay;
2. покупатель платит и возвращается на ``/thanks``;
3. CryptoPay присылает вебхук ``checkout.session.completed`` на ``/webhook`` — магазин проверяет
   подпись и только после этого считает заказ оплаченным. Возврату покупателя на /thanks
   не верим: его адрес можно открыть и без оплаты.

Адрес вебхука добавьте в кабинете CryptoPay → «Разработчикам» → «Вебхуки»:
``http://localhost:4000/webhook`` (локально http разрешён, пока APP_ENV не production).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

CRYPTOPAY_URL = os.environ.get("CRYPTOPAY_URL", "http://localhost:3000").rstrip("/")
API_KEY = os.environ.get("CRYPTOPAY_API_KEY", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
PORT = int(os.environ.get("PORT", "4000"))
SHOP_URL = os.environ.get("SHOP_URL", f"http://localhost:{PORT}")
TOLERANCE_SECONDS = 5 * 60

PRODUCT = {"name": "Худи «Пиксель»", "amount": "25000000", "currency": "usdt", "price": "25 USDT"}
orders: dict[str, dict[str, str]] = {}  # номер заказа → {"status", "session"}
processed_events: set[str] = set()  # идемпотентность: одно событие могут прислать повторно


def verify_signature(header: str | None, raw_body: bytes, secret: str, now: float | None = None) -> str | None:
    """Проверка CryptoPay-Signature: t=<unix>,v1=<hex HMAC-SHA256("<t>.<тело>")>. None — подпись верна."""
    parts = dict(item.split("=", 1) for item in (header or "").split(",") if "=" in item)
    if not parts.get("t", "").isdigit() or not parts.get("v1"):
        return "нет подписи"
    timestamp = int(parts["t"])
    if abs((now or time.time()) - timestamp) > TOLERANCE_SECONDS:
        return "подпись устарела (возможен replay)"
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256).hexdigest()
    # compare_digest сравнивает за постоянное время — по времени ответа подпись не подобрать.
    return None if hmac.compare_digest(expected, parts["v1"]) else "подпись не совпала"


def create_checkout_session(order_id: str) -> dict[str, str]:
    body = json.dumps(
        {
            "amount": PRODUCT["amount"],
            "currency": PRODUCT["currency"],
            "description": PRODUCT["name"],
            "success_url": f"{SHOP_URL}/thanks?order={order_id}",
            "cancel_url": f"{SHOP_URL}/",
            "metadata": {"order_id": order_id},
        }
    ).encode()
    request = urllib.request.Request(  # адрес CryptoPay задаёт владелец магазина
        f"{CRYPTOPAY_URL}/v1/checkout/sessions",
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            # как у Stripe: ключ можно передать Basic-авторизацией (логин — ключ, пароль пустой)
            "authorization": "Basic " + base64.b64encode(f"{API_KEY}:".encode()).decode(),
            # повтор запроса после обрыва связи не создаст вторую страницу оплаты
            "idempotency-key": f"order-{order_id}",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        session: dict[str, str] = json.loads(response.read())
    return session


PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{refresh}<title>Магазин «Пиксель»</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 34rem; margin: 3rem auto; padding: 0 1rem">{body}</body></html>"""


class ShopHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/":
            self._page(
                f"<h1>Магазин «Пиксель»</h1><p>{html.escape(PRODUCT['name'])} — <b>{PRODUCT['price']}</b></p>"
                '<form method="post" action="/buy"><button id="buy">Купить через CryptoPay</button></form>'
            )
        elif url.path == "/thanks":
            order_id = parse_qs(url.query).get("order", [""])[0]
            order = orders.get(order_id)
            if order is None:
                self._page("<h1>Заказ не найден</h1>", status=404)
            elif order["status"] == "paid":
                self._page(
                    f'<h1 id="order-status">Заказ {html.escape(order_id)} оплачен</h1><p>Скоро отправим худи.</p>'
                )
            else:  # покупатель вернулся раньше вебхука — просто ждём его
                self._page(
                    f'<h1 id="order-status">Заказ {html.escape(order_id)}: ждём подтверждения оплаты…</h1>', refresh=2
                )
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/buy":
            order_id = str(1000 + len(orders) + 1)
            try:
                session = create_checkout_session(order_id)
            except urllib.error.HTTPError as error:
                self._page(
                    f"<h1>Не удалось создать оплату</h1><pre>{html.escape(error.read().decode())}</pre>", status=502
                )
                return
            orders[order_id] = {"status": "pending", "session": session["id"]}
            self.send_response(303)
            self.send_header("location", session["url"])
            self.end_headers()
        elif self.path == "/webhook":
            self._webhook()
        else:
            self.send_error(404)

    def _webhook(self) -> None:
        raw = self.rfile.read(int(self.headers.get("content-length", "0")))
        problem = verify_signature(self.headers.get("cryptopay-signature"), raw, WEBHOOK_SECRET)
        if problem:
            print(f"✗ вебхук отклонён: {problem}", flush=True)
            self._reply(400, problem)
            return
        event = json.loads(raw)
        if event["id"] not in processed_events:
            processed_events.add(event["id"])
            obj = event["data"]["object"]
            if event["type"] == "checkout.session.completed":
                for order_id, order in orders.items():
                    if order["session"] == obj["id"]:
                        order["status"] = "paid"
                        print(f"✓ Заказ {order_id} оплачен ({obj['amount_total']} {obj['currency']})", flush=True)
            else:
                print(f"• {event['type']} {obj.get('id', '')}", flush=True)
        self._reply(200, "ok")

    def _page(self, body: str, status: int = 200, refresh: int | None = None) -> None:
        meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
        self._reply(status, PAGE.format(body=body, refresh=meta), "text/html")

    def _reply(self, status: int, text: str, content_type: str = "text/plain") -> None:
        data = text.encode()
        self.send_response(status)
        self.send_header("content-type", f"{content_type}; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:  # тишина вместо стандартного лога доступа
        pass


if __name__ == "__main__":
    if not API_KEY.startswith("sk_test_") or not WEBHOOK_SECRET.startswith("whsec_"):
        sys.exit(
            "Укажите CRYPTOPAY_API_KEY (sk_test_…) и WEBHOOK_SECRET (whsec_…) из кабинета CryptoPay → «Разработчикам»"
        )
    print(f"Магазин открыт: {SHOP_URL}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), ShopHandler).serve_forever()
