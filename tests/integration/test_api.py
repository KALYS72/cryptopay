"""REST API через настоящий HTTP-стек FastAPI (httpx + ASGITransport, без сети и сервера)."""

from __future__ import annotations

import base64

import pytest

from tests.support.harness import Harness, auth, create_harness

# ───────────────────────────── аутентификация ─────────────────────────────


async def test_register_login_and_account(h: Harness) -> None:
    token = await h.register()
    me = await h.client.get("/api/account", headers=auth(token))
    assert me.status_code == 200
    assert me.json()["object"] == "account" and me.json()["email"] == "shop@example.com"
    assert me.json()["api_key"] is None

    login = await h.client.post("/api/auth/login", json={"email": "shop@example.com", "password": "correct-horse"})
    assert login.status_code == 200 and login.json()["token"]
    wrong = await h.client.post("/api/auth/login", json={"email": "shop@example.com", "password": "nope-nope"})
    assert wrong.status_code == 401
    assert wrong.json()["error"] == {
        "type": "authentication_error",
        "code": "invalid_credentials",
        "message": "Неверный e-mail или пароль",
    }
    duplicate = await h.client.post(
        "/api/auth/register", json={"email": "SHOP@example.com", "password": "another-pass", "business_name": "X Y"}
    )
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "email_taken"


async def test_api_key_works_as_bearer_and_basic_like_stripe(h: Harness) -> None:
    key = await h.api_key(await h.register())
    bearer = await h.client.get("/v1/payment_intents", headers=auth(key))
    basic = await h.client.get(
        "/v1/payment_intents", headers={"authorization": "Basic " + base64.b64encode(f"{key}:".encode()).decode()}
    )
    assert bearer.status_code == basic.status_code == 200
    assert bearer.json() == {"object": "list", "url": "/v1/payment_intents", "has_more": False, "data": []}


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ({}, "authentication_required"),
        ({"authorization": "Bearer sk_test_wrong"}, "invalid_api_key"),
        ({"authorization": "Bearer not-a-jwt"}, "session_expired"),
    ],
)
async def test_unauthorized(h: Harness, headers: dict[str, str], code: str) -> None:
    response = await h.client.get("/v1/balance", headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == code


async def test_api_key_cannot_manage_account(h: Harness) -> None:
    key = await h.api_key(await h.register())
    response = await h.client.post("/api/account/api_key", headers=auth(key))
    assert response.status_code == 403 and response.json()["error"]["type"] == "permission_error"


# ───────────────────────────── ошибки в формате Stripe ─────────────────────────────


async def test_validation_errors_name_the_parameter(h: Harness) -> None:
    key = await h.api_key(await h.register())
    missing = await h.client.post("/v1/checkout/sessions", json={"currency": "usdt"}, headers=auth(key))
    assert missing.status_code == 400
    assert missing.json()["error"] == {
        "type": "invalid_request_error",
        "code": "parameter_missing",
        "message": "amount: обязательный параметр",
        "param": "amount",
    }
    bad_amount = await h.client.post(
        "/v1/checkout/sessions", json={"amount": "12.50", "currency": "usdt"}, headers=auth(key)
    )
    assert bad_amount.json()["error"]["param"] == "amount"
    bad_currency = await h.client.post(
        "/v1/checkout/sessions", json={"amount": "25000000", "currency": "doge"}, headers=auth(key)
    )
    assert bad_currency.json()["error"]["param"] == "currency"
    too_small = await h.client.post("/v1/checkout/sessions", json={"amount": 1, "currency": "usdt"}, headers=auth(key))
    assert too_small.json()["error"]["code"] == "amount_too_small"
    unknown_field = await h.client.post(
        "/v1/checkout/sessions", json={"amount": 1, "currency": "usdt", "hack": 1}, headers=auth(key)
    )
    assert unknown_field.json()["error"]["message"] == "hack: неизвестный параметр"
    broken = await h.client.post(
        "/v1/checkout/sessions", content=b"{not json", headers={**auth(key), "content-type": "application/json"}
    )
    assert broken.status_code == 400 and broken.json()["error"]["message"] == "Тело запроса — некорректный JSON"


async def test_not_found_is_json_for_api_and_html_for_pages(h: Harness) -> None:
    key = await h.api_key(await h.register())
    missing = await h.client.get("/v1/payment_intents/pi_missing", headers=auth(key))
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "resource_missing"
    route = await h.client.get("/v1/nothing", headers=auth(key))
    assert route.status_code == 404 and route.json()["error"]["type"] == "invalid_request_error"
    page = await h.client.get("/no-such-page")
    assert page.status_code == 404 and "text/html" in page.headers["content-type"]
    method = await h.client.put("/v1/balance", headers=auth(key))
    assert method.status_code == 405


async def test_other_merchant_objects_are_invisible(h: Harness) -> None:
    owner_key = await h.api_key(await h.register())
    stranger_key = await h.api_key(await h.register("other@example.com"))
    session = await h.create_session(owner_key)
    for path in (f"/v1/checkout/sessions/{session['id']}", f"/v1/payment_intents/{session['payment_intent']}"):
        response = await h.client.get(path, headers=auth(stranger_key))
        assert response.status_code == 404


# ───────────────────────────── идемпотентность ─────────────────────────────


async def test_idempotency_key_replays_response(h: Harness) -> None:
    key = await h.api_key(await h.register())
    headers = {**auth(key), "idempotency-key": "order-1042"}
    body = {"amount": "25000000", "currency": "usdt"}
    first = await h.client.post("/v1/checkout/sessions", json=body, headers=headers)
    second = await h.client.post("/v1/checkout/sessions", json=body, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert "idempotent-replayed" not in first.headers and second.headers["idempotent-replayed"] == "true"
    sessions = await h.client.get("/v1/checkout/sessions", headers=auth(key))
    assert len(sessions.json()["data"]) == 1

    reused = await h.client.post("/v1/checkout/sessions", json={**body, "amount": "26000000"}, headers=headers)
    assert reused.status_code == 400
    assert reused.json()["error"] == {
        "type": "idempotency_error",
        "code": "idempotency_key_reused",
        "message": "Этот Idempotency-Key уже использован для запроса с другими параметрами",
    }


async def test_failed_request_does_not_burn_the_key(h: Harness) -> None:
    key = await h.api_key(await h.register())
    headers = {**auth(key), "idempotency-key": "k-1"}
    bad = await h.client.post("/v1/checkout/sessions", json={"amount": "1", "currency": "usdt"}, headers=headers)
    assert bad.status_code == 400
    retry = await h.client.post("/v1/checkout/sessions", json={"amount": "1", "currency": "usdt"}, headers=headers)
    assert retry.status_code == 400 and "idempotent-replayed" not in retry.headers


# ───────────────────────────── объекты API ─────────────────────────────


async def test_pagination_with_starting_after(h: Harness) -> None:
    key = await h.api_key(await h.register())
    created = []
    for _ in range(3):
        created.append(await h.create_session(key))
        h.clock.advance_seconds(1)
    first = (await h.client.get("/v1/payment_intents?limit=2", headers=auth(key))).json()
    assert first["has_more"] is True
    assert [p["id"] for p in first["data"]] == [created[2]["payment_intent"], created[1]["payment_intent"]]
    second = (
        await h.client.get(f"/v1/payment_intents?limit=2&starting_after={first['data'][-1]['id']}", headers=auth(key))
    ).json()
    assert [p["id"] for p in second["data"]] == [created[0]["payment_intent"]] and second["has_more"] is False
    too_big = await h.client.get("/v1/payment_intents?limit=1000", headers=auth(key))
    assert too_big.status_code == 400


async def test_refund_through_api(h: Harness) -> None:
    key = await h.api_key(await h.register())
    session = await h.create_session(key)
    await h.pay(session["id"])
    await h.mine(3)
    refund = await h.client.post(
        "/v1/refunds",
        json={"payment_intent": session["payment_intent"], "amount": "5000000", "reason": "requested_by_customer"},
        headers=auth(key),
    )
    assert refund.status_code == 200, refund.text
    assert refund.json()["object"] == "refund" and refund.json()["status"] == "pending"
    await h.mine(3)
    found = (await h.client.get(f"/v1/refunds/{refund.json()['id']}", headers=auth(key))).json()
    assert found["status"] == "succeeded" and found["transaction"].startswith("0x")
    intent = (await h.client.get(f"/v1/payment_intents/{session['payment_intent']}", headers=auth(key))).json()
    assert intent["amount_refunded"] == "5000000"
    entries = (await h.client.get("/v1/balance_transactions", headers=auth(key))).json()["data"]
    assert [(e["type"], e["amount"], e["fee"], e["net"]) for e in entries] == [
        ("refund", "-5000000", "0", "-5000000"),
        ("payment", "25000000", "250000", "24750000"),
    ]
    listed = (await h.client.get(f"/v1/refunds?payment_intent={session['payment_intent']}", headers=auth(key))).json()
    assert [r["id"] for r in listed["data"]] == [refund.json()["id"]]


async def test_cancel_and_expire(h: Harness) -> None:
    key = await h.api_key(await h.register())
    first = await h.create_session(key)
    canceled = await h.client.post(
        f"/v1/payment_intents/{first['payment_intent']}/cancel",
        json={"cancellation_reason": "duplicate"},
        headers=auth(key),
    )
    assert canceled.status_code == 200 and canceled.json()["status"] == "canceled"
    assert canceled.json()["cancellation_reason"] == "duplicate"
    again = await h.client.post(f"/v1/payment_intents/{first['payment_intent']}/cancel", headers=auth(key))
    assert again.status_code == 400 and again.json()["error"]["code"] == "payment_intent_unexpected_state"

    second = await h.create_session(key)
    expired = await h.client.post(f"/v1/checkout/sessions/{second['id']}/expire", headers=auth(key))
    assert expired.status_code == 200
    assert expired.json()["status"] == "expired" and expired.json()["url"] is None
    page = await h.client.get(f"/api/checkout/{second['id']}")
    assert page.json()["status"] == "expired" and page.json()["payment"]["status"] == "canceled"
    pay = await h.client.post(f"/api/checkout/{second['id']}/pay", json={})
    assert pay.status_code == 400 and pay.json()["error"]["code"] == "checkout_session_inactive"


async def test_events_and_deliveries(h: Harness) -> None:
    key = await h.api_key(await h.register())
    endpoint = await h.client.post(
        "/v1/webhook_endpoints",
        json={
            "url": "https://shop.example/hooks",
            "enabled_events": ["payment_intent.created"],
            "description": "Магазин",
        },
        headers=auth(key),
    )
    assert endpoint.json()["enabled_events"] == ["payment_intent.created"]
    session = await h.create_session(key)
    await h.run_jobs()
    events = (await h.client.get("/v1/events?type=payment_intent.created", headers=auth(key))).json()["data"]
    assert len(events) == 1 and events[0]["data"]["object"]["id"] == session["payment_intent"]
    assert events[0]["api_version"] and events[0]["livemode"] is False
    deliveries = (await h.client.get(f"/v1/events/{events[0]['id']}/deliveries", headers=auth(key))).json()["data"]
    assert [(d["status"], d["attempts"], d["last_status_code"]) for d in deliveries] == [("delivered", 1, 200)]

    listed = (await h.client.get("/v1/webhook_endpoints", headers=auth(key))).json()["data"]
    assert "secret" not in listed[0]  # секрет показывается только при создании
    test = await h.client.post(f"/v1/webhook_endpoints/{listed[0]['id']}/test", headers=auth(key))
    assert test.status_code == 200 and test.json()["status"] == "pending"
    deleted = await h.client.delete(f"/v1/webhook_endpoints/{listed[0]['id']}", headers=auth(key))
    assert deleted.json() == {"id": listed[0]["id"], "object": "webhook_endpoint", "deleted": True}


async def test_webhook_url_is_validated(h: Harness) -> None:
    key = await h.api_key(await h.register())
    for url, events in (("not a url", ["*"]), ("ftp://shop.example", ["*"]), ("https://shop.example", ["x.y"])):
        response = await h.client.post(
            "/v1/webhook_endpoints", json={"url": url, "enabled_events": events}, headers=auth(key)
        )
        assert response.status_code == 400, url


async def test_overview_and_config(h: Harness) -> None:
    token = await h.register()
    key = await h.api_key(token)
    session = await h.create_session(key)
    await h.pay(session["id"])
    await h.mine(3)
    overview = (await h.client.get("/api/dashboard/overview", headers=auth(token))).json()
    assert overview["balances"] == {"usdt": "24750000"}
    assert overview["payments_by_status"]["succeeded"] == 1
    assert len(overview["days"]) == 7 and overview["volume"]["usdt"][overview["days"][-1]] == "25000000"
    config = (await h.client.get("/api/config")).json()
    assert [c["code"] for c in config["currencies"]] == ["usdt", "usdc", "btc", "eth"]
    assert config["required_confirmations"] == 3 and config["demo"] is None


# ───────────────────────────── страница оплаты и кошелёк ─────────────────────────────


async def test_public_checkout_hides_merchant_internals(h: Harness) -> None:
    key = await h.api_key(await h.register())
    session = await h.create_session(key, metadata={"secret_note": "VIP"})
    page = (await h.client.get(f"/api/checkout/{session['id']}")).json()
    assert page["business_name"] == "Магазин «Пиксель»"
    assert page["amount_display"] == "25" and page["currency_symbol"] == "USDT"
    assert "metadata" not in page and "VIP" not in str(page)
    missing = await h.client.get("/api/checkout/cs_missing")
    assert missing.status_code == 404


async def test_wallet_api_errors(h: Harness) -> None:
    missing = await h.client.get("/wallet/api/requests/wpr_missing")
    assert missing.status_code == 404 and missing.json()["error"]["type"] == "wallet_error"
    key = await h.api_key(await h.register())
    session = await h.create_session(key)
    start = (await h.client.post(f"/api/checkout/{session['id']}/pay", json={})).json()
    request_id = start["redirect_url"].rsplit("/", 1)[-1]
    details = (await h.client.get(f"/wallet/api/requests/{request_id}")).json()
    assert details["request"]["merchant_name"] == "Магазин «Пиксель»"
    assert [a["id"] for a in details["accounts"]] == ["alice", "bob", "carol", "dave", "eve"]
    rejected = await h.client.post(f"/wallet/api/requests/{request_id}/reject")
    assert rejected.json()["request"]["status"] == "rejected"
    twice = await h.client.post(f"/wallet/api/requests/{request_id}/approve", json={"account_id": "alice"})
    assert twice.status_code == 409


async def test_demo_checkout_button() -> None:
    harness = await create_harness({"DEMO_ACCOUNT": "true"})
    try:
        response = await harness.client.post("/api/demo/checkout")
        assert response.status_code == 200
        session_id = response.json()["redirect_url"].rsplit("/", 1)[-1]
        page = (await harness.client.get(f"/api/checkout/{session_id}")).json()
        assert page["business_name"] == "Демо-магазин «Пиксель»" and page["amount"] == "25000000"
        config = (await harness.client.get("/api/config")).json()
        assert config["demo"] == {"email": "demo@cryptopay.dev", "password": "demo-password"}
    finally:
        await harness.client.aclose()


async def test_demo_checkout_disabled(h: Harness) -> None:
    response = await h.client.post("/api/demo/checkout")
    assert response.status_code == 404


# ───────────────────────────── служебное и безопасность ─────────────────────────────


async def test_health_and_openapi(h: Harness) -> None:
    health = (await h.client.get("/healthz")).json()
    assert health["status"] == "ok" and health["wallet"]["type"] == "mock"
    spec = (await h.client.get("/api/openapi.json")).json()
    assert "/v1/checkout/sessions" in spec["paths"] and "/wallet/api/explorer" not in spec["paths"]
    docs = await h.client.get("/docs")
    assert docs.status_code == 200 and "swagger-ui" in docs.text


async def test_security_headers(h: Harness) -> None:
    response = await h.client.get("/")
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "server" not in response.headers


async def test_body_size_limit(h: Harness) -> None:
    response = await h.client.post(
        "/api/auth/login", content=b"x" * (33 * 1024), headers={"content-type": "application/json"}
    )
    assert response.status_code == 413 and response.json()["error"]["code"] == "request_too_large"


async def test_rate_limit_on_login() -> None:
    harness = await create_harness({"RATE_LIMIT_ENABLED": "true", "TRUST_PROXY": "1"})
    try:
        body = {"email": "a@b.co", "password": "whatever1"}
        # За одним прокси (Render): прокси дописывает реальный IP клиента последним звеном.
        via_proxy = {"x-forwarded-for": "1.1.1.1, 9.9.9.9"}
        for _ in range(20):
            assert (await harness.client.post("/api/auth/login", json=body, headers=via_proxy)).status_code == 401
        blocked = await harness.client.post("/api/auth/login", json=body, headers=via_proxy)
        assert blocked.status_code == 429 and blocked.json()["error"]["type"] == "rate_limit_error"
        assert int(blocked.headers["retry-after"]) > 0
        # клиент подделывает начало цепочки — не помогает: доверяем только звену, которое дописал прокси
        spoofed = await harness.client.post(
            "/api/auth/login", json=body, headers={"x-forwarded-for": "2.2.2.2, 9.9.9.9"}
        )
        assert spoofed.status_code == 429
        other_client = await harness.client.post("/api/auth/login", json=body, headers={"x-forwarded-for": "8.8.8.8"})
        assert other_client.status_code == 401
    finally:
        await harness.client.aclose()
