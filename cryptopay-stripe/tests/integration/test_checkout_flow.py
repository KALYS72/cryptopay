"""Полный путь оплаты через HTTP — как его проходят магазин, покупатель и кошелёк.

Каждый тест — один сценарий из таблицы тестовых кошельков (как тестовые карты Stripe).
"""

from __future__ import annotations

import json

from tests.support.harness import Harness, auth


async def test_happy_path_from_session_to_webhook(h: Harness) -> None:
    token = await h.register()
    key = await h.api_key(token)
    endpoint = await h.client.post(
        "/v1/webhook_endpoints",
        json={"url": "https://shop.example/hooks", "enabled_events": ["*"]},
        headers=auth(key),
    )
    assert endpoint.status_code == 200, endpoint.text
    assert endpoint.json()["secret"].startswith("whsec_")

    session = await h.create_session(
        key, success_url="https://shop.example/thanks?session={CHECKOUT_SESSION_ID}", metadata={"order_id": "1042"}
    )
    assert session["object"] == "checkout.session"
    assert session["url"] == f"http://localhost:3000/checkout/{session['id']}"
    assert session["status"] == "open" and session["payment_status"] == "unpaid"

    page = await h.pay(session["id"], "alice")
    assert page["payment"]["status"] == "processing"
    assert page["payment"]["confirmations"] == 0

    await h.mine(1)
    page = (await h.client.get(f"/api/checkout/{session['id']}")).json()
    assert page["payment"]["status"] == "processing"
    assert page["payment"]["confirmations"] == 1

    await h.mine(2)
    page = (await h.client.get(f"/api/checkout/{session['id']}")).json()
    assert page["status"] == "complete"
    assert page["payment"]["status"] == "succeeded"
    assert page["success_url"] == f"https://shop.example/thanks?session={session['id']}"

    intent = (await h.client.get(f"/v1/payment_intents/{session['payment_intent']}", headers=auth(key))).json()
    assert intent["status"] == "succeeded"
    assert intent["amount_received"] == "25000000"
    assert intent["metadata"] == {"order_id": "1042"}
    assert intent["payment_method"]["payer_address"] == h.wallet.customer_accounts()[0].address

    balance = (await h.client.get("/v1/balance", headers=auth(key))).json()
    usdt = next(b for b in balance["available"] if b["currency"] == "usdt")
    assert usdt["amount"] == "24750000"  # 25 USDT минус 1 % комиссии платформы

    await h.run_jobs()
    assert h.webhooks.event_types() == [
        "payment_intent.created",
        "payment_intent.requires_action",
        "payment_intent.processing",
        "payment_intent.succeeded",
        "checkout.session.completed",
    ]


async def test_customer_retries_with_another_wallet(h: Harness) -> None:
    key = await h.api_key(await h.register())
    session = await h.create_session(key)
    page = await h.pay(session["id"], "bob")
    assert page["status"] == "open"
    assert page["payment"]["status"] == "requires_payment_method"
    assert page["payment"]["last_payment_error"] == {
        "code": "insufficient_funds",
        "message": "Недостаточно средств в кошельке",
    }
    page = await h.pay(session["id"], "alice")
    assert page["payment"]["status"] == "processing" and page["payment"]["last_payment_error"] is None
    await h.mine(3)
    page = (await h.client.get(f"/api/checkout/{session['id']}")).json()
    assert page["payment"]["status"] == "succeeded"
    failed = (await h.client.get("/v1/events?type=payment_intent.payment_failed", headers=auth(key))).json()["data"]
    assert len(failed) == 1


async def test_failed_onchain_transaction_is_visible_to_customer(h: Harness) -> None:
    key = await h.api_key(await h.register())
    session = await h.create_session(key)
    await h.pay(session["id"], "eve")
    await h.mine(1)
    page = (await h.client.get(f"/api/checkout/{session['id']}")).json()
    assert page["payment"]["status"] == "requires_payment_method"
    assert page["payment"]["last_payment_error"]["code"] == "transaction_failed"


async def test_webhook_object_equals_api_object(h: Harness) -> None:
    key = await h.api_key(await h.register())
    await h.client.post(
        "/v1/webhook_endpoints",
        json={"url": "https://shop.example/hooks", "enabled_events": ["checkout.session.completed"]},
        headers=auth(key),
    )
    session = await h.create_session(key)
    await h.pay(session["id"])
    await h.mine(3)
    await h.run_jobs()
    sent = json.loads(h.webhooks.sent[0].body)
    api_object = (await h.client.get(f"/v1/checkout/sessions/{session['id']}", headers=auth(key))).json()
    assert sent["type"] == "checkout.session.completed"
    assert sent["data"]["object"] == api_object
