#!/usr/bin/env bash
# Сервер магазина создаёт страницу оплаты через API CryptoPay и получает ссылку для покупателя.
#   CRYPTOPAY_URL=https://cryptopay-xxxx.onrender.com CRYPTOPAY_API_KEY=sk_test_… ./examples/create-session.sh 25000000 usdt 1042
# Сумма — в минимальных единицах валюты: 25 USDT = 25000000 (6 знаков после запятой).
set -euo pipefail

URL="${CRYPTOPAY_URL:-http://localhost:3000}"
KEY="${CRYPTOPAY_API_KEY:?Укажите CRYPTOPAY_API_KEY (кабинет → «Разработчикам» → «Выпустить ключ»)}"
AMOUNT="${1:-25000000}"
CURRENCY="${2:-usdt}"
ORDER_ID="${3:-$(date +%s)}"

curl -sS "$URL/v1/checkout/sessions" \
  -u "$KEY:" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: order-$ORDER_ID" \
  -d "{\"amount\":\"$AMOUNT\",\"currency\":\"$CURRENCY\",\"description\":\"Заказ №$ORDER_ID\",\"metadata\":{\"order_id\":\"$ORDER_ID\"}}"
echo
