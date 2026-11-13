"""Сквозные сценарии в настоящем браузере: магазин → страница оплаты → кошелёк → сеть → вебхук → кабинет.

Скриншоты из этих прогонов лежат в docs/screenshots и используются в отчёте.
"""

from __future__ import annotations

import re
import time

from playwright.sync_api import Browser, Page, expect

from .conftest import BASE_URL, ConsoleWatch, Shop, call, shot

PAID_TIMEOUT = 20_000  # 3 подтверждения при блоке раз в секунду + опрос страницы


def open_demo_checkout(page: Page) -> None:
    page.goto(BASE_URL + "/")
    page.click("#try-demo")
    page.wait_for_url(re.compile(r"/checkout/cs_"))
    expect(page.locator("#pay-button")).to_contain_text("25,00 USDT")


def pay_in_wallet(page: Page, account: str, *, approve: bool = True) -> None:
    page.click("#pay-button")
    page.wait_for_url(re.compile(r"/wallet/approve/wpr_"))
    expect(page.locator("#screen-request")).to_be_visible()
    page.check(f"input[name='account'][value='{account}']")
    page.click("#approve" if approve else "#reject")
    if approve and account in ("bob", "carol"):
        # кошелёк сам показывает отказ и не уводит покупателя, пока тот не прочитает причину
        expect(page.locator("#screen-result")).to_be_visible()
        expect(page.locator("#result-title")).to_have_text("Платёж не прошёл")
        page.click("#result-back")
    page.wait_for_url(re.compile(r"/checkout/cs_"), timeout=10_000)


def test_customer_pays_with_alice_and_sees_the_stamp(page: Page) -> None:
    page.goto(BASE_URL + "/")
    shot(page, "01-landing")
    open_demo_checkout(page)
    page.fill("#email", "buyer@example.com")
    shot(page, "02-checkout")

    page.click("#pay-button")
    page.wait_for_url(re.compile(r"/wallet/approve/"))
    expect(page.locator("#accounts .account")).to_have_count(5)
    shot(page, "03-wallet-approve")
    page.click("#approve")
    page.wait_for_url(re.compile(r"/checkout/cs_"), timeout=10_000)

    expect(page.locator("#state-processing")).to_be_visible()
    shot(page, "04-checkout-processing")
    expect(page.locator("#state-paid")).to_be_visible(timeout=PAID_TIMEOUT)
    expect(page.locator("#stamp")).to_be_visible()
    expect(page.locator("#paid-amount")).to_have_text("25,00 USDT")
    page.wait_for_timeout(500)  # печать «докатилась»
    shot(page, "05-checkout-paid")


def test_insufficient_funds_then_retry_with_another_wallet(page: Page) -> None:
    open_demo_checkout(page)
    pay_in_wallet(page, "bob")
    expect(page.locator("#pay-error")).to_contain_text("Недостаточно средств")
    expect(page.locator("#pay-button")).to_contain_text("Попробовать снова")
    shot(page, "06-checkout-insufficient-funds")
    pay_in_wallet(page, "alice")
    expect(page.locator("#state-paid")).to_be_visible(timeout=PAID_TIMEOUT)


def test_customer_rejects_in_wallet(page: Page) -> None:
    open_demo_checkout(page)
    pay_in_wallet(page, "alice", approve=False)
    expect(page.locator("#pay-error")).to_contain_text("отклонил")


def test_wallet_declines(page: Page) -> None:
    open_demo_checkout(page)
    pay_in_wallet(page, "carol")
    expect(page.locator("#pay-error")).to_contain_text("Кошелёк отклонил")


def test_transaction_fails_in_the_network(page: Page) -> None:
    open_demo_checkout(page)
    pay_in_wallet(page, "eve")
    expect(page.locator("#pay-error")).to_contain_text("Выполнение транзакции прервано", timeout=PAID_TIMEOUT)


def test_shop_integration_with_webhook(page: Page, shop: Shop) -> None:
    """Магазин из examples/shop.py: сессия по API-ключу → оплата → возврат в магазин → вебхук отмечает заказ."""
    page.goto(shop.url)
    page.click("#buy")
    page.wait_for_url(re.compile(r"/checkout/cs_"))
    expect(page.locator("#merchant-name")).to_have_text("Магазин «Пиксель»")
    expect(page.locator("#back-link")).to_be_visible()
    pay_in_wallet(page, "alice")
    expect(page.locator("#state-paid")).to_be_visible(timeout=PAID_TIMEOUT)
    page.click("#success-link")
    page.wait_for_url(re.compile(r"/thanks\?order="))
    expect(page.locator("#order-status")).to_contain_text("оплачен", timeout=15_000)
    shot(page, "07-shop-order-paid")

    events = call("GET", "/v1/events?type=checkout.session.completed", token=shop.token)["data"]
    deliveries = call("GET", f"/v1/events/{events[0]['id']}/deliveries", token=shop.token)["data"]
    assert [d["status"] for d in deliveries] == ["delivered"]


def test_dashboard_refund(page: Page) -> None:
    open_demo_checkout(page)
    pay_in_wallet(page, "alice")
    expect(page.locator("#state-paid")).to_be_visible(timeout=PAID_TIMEOUT)

    page.goto(BASE_URL + "/dashboard")
    page.click("#demo-login")
    expect(page.locator("#app")).to_be_visible()
    expect(page.locator(".balance-amount").first).not_to_have_text("0,00")
    shot(page, "08-dashboard-home")

    page.click("a[data-view='payments']")
    expect(page.locator(".ledger tbody tr.is-clickable").first).to_be_visible()
    shot(page, "09-dashboard-payments")
    page.locator(".ledger tbody tr.is-clickable", has_text="Оплачен").first.click()
    drawer = page.locator("#drawer")
    expect(drawer.locator(".drawer-amount")).to_have_text("25,00 USDT")
    drawer.locator(".refund-form input").fill("5")
    drawer.locator(".refund-form button").click()
    expect(drawer.locator(".timeline")).to_contain_text("Возврат дошёл до покупателя", timeout=PAID_TIMEOUT)
    expect(drawer.locator(".ledger .status--succeeded")).to_have_text("Возвращён")
    expect(drawer.locator(".drawer-amount + .status")).to_have_text("Частично возвращён")
    shot(page, "10-dashboard-payment-refunded", full_page=False)
    page.keyboard.press("Escape")

    page.click("a[data-view='balance']")
    expect(page.locator("#view .ledger")).to_contain_text("Возврат")
    shot(page, "11-dashboard-balance")


def test_dashboard_payment_link_and_developers(page: Page, browser: Browser, console: ConsoleWatch) -> None:
    page.goto(BASE_URL + "/dashboard")
    page.click("#tab-register")
    page.fill("#business_name", "Кофейня «Байт»")
    page.fill("#email", f"coffee{int(time.time() * 1000)}@example.com")
    page.fill("#password", "correct-horse")
    page.click("#auth-submit")
    expect(page.locator("#shop-name")).to_have_text("Кофейня «Байт»")
    expect(page.locator(".steps")).to_be_visible()  # подсказка «как принять первый платёж»

    page.click("#new-payment")
    page.fill("#pf-amount", "0.0005")
    page.select_option("#pf-currency", "btc")
    page.fill("#pf-description", "Капучино")
    page.click("#payment-submit")
    expect(page.locator("#payment-created")).to_be_visible()
    link = page.locator("#created-url").inner_text()
    shot(page, "12-dashboard-payment-link", full_page=False)
    page.locator("#payment-created [data-close]").click()

    checkout = console.attach(page.context.new_page())
    checkout.goto(link)
    expect(checkout.locator("#order-amount")).to_have_text("0,0005")
    expect(checkout.locator("#order-currency")).to_have_text("BTC")
    checkout.close()

    page.click("a[data-view='developers']")
    page.locator("button", has_text="Выпустить ключ").click()
    expect(page.locator("#secret-value")).to_have_text(re.compile(r"^sk_test_[0-9a-f]{48}$"))
    secret = page.locator("#secret-value").inner_text()
    page.locator("#secret-dialog [data-close]").click()
    expect(page.locator(".key-preview")).to_have_text(f"sk_test_…{secret[-4:]}")

    page.fill("#we-url", "https://coffee.example/hooks")
    page.locator("button", has_text="Добавить адрес").click()
    expect(page.locator("#secret-value")).to_have_text(re.compile(r"^whsec_"))
    page.locator("#secret-dialog [data-close]").click()
    expect(page.locator(".endpoint")).to_contain_text("https://coffee.example/hooks")
    page.locator(".ledger tr.is-clickable").first.click()
    expect(page.locator("pre.code").last).to_contain_text('"object": "event"')
    shot(page, "13-dashboard-developers")


def test_mockchain_explorer_shows_the_payment(page: Page) -> None:
    open_demo_checkout(page)
    pay_in_wallet(page, "alice")
    expect(page.locator("#state-paid")).to_be_visible(timeout=PAID_TIMEOUT)

    page.goto(BASE_URL + "/wallet")
    expect(page.locator("#transactions tr").first).to_be_visible()
    expect(page.locator("#accounts tr")).to_have_count(6)
    shot(page, "14-mockchain-explorer")


def test_swagger_ui_works_under_strict_csp(page: Page) -> None:
    page.goto(BASE_URL + "/docs")
    expect(page.locator(".swagger-ui .info .title")).to_contain_text("CryptoPay API")
    expect(page.locator("#operations-tag-Checkout_Sessions")).to_be_visible()
    shot(page, "15-swagger-ui", full_page=False)
