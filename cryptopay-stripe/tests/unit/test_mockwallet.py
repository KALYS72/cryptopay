"""Имитатор кошелька: тестовые кошельки дают заранее известный исход, «сеть» двигается вместе с часами."""

from __future__ import annotations

import pytest

from cryptopay.mockwallet import MockWallet, MockWalletError
from cryptopay.mockwallet.chain import MockChain
from tests.support.fakes import T0, FakeClock

BLOCK = 3.0
AMOUNT = 25_000_000


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def wallet(clock: FakeClock) -> MockWallet:
    return MockWallet(now=clock.now, block_time_seconds=BLOCK)


def request(wallet: MockWallet, amount: int = AMOUNT, currency: str = "usdt") -> str:
    created = wallet.create_payment_request(
        reference="pi_1",
        merchant_name="Магазин",
        description=None,
        amount=amount,
        currency=currency,
        to_address=wallet.treasury.address,
        return_url="http://localhost/checkout/cs_1",
    )
    return created.id


def balance(wallet: MockWallet, account_id: str, currency: str = "usdt") -> int:
    return next(a for a in wallet.accounts() if a.id == account_id).balances[currency]


def test_chain_height_follows_clock() -> None:
    chain = MockChain(genesis=T0, block_time_seconds=BLOCK)
    clock = FakeClock()
    assert chain.height(clock.now()) == 0
    clock.advance_seconds(7)
    assert chain.height(clock.now()) == 2
    assert chain.confirmations(included_height=2, now=clock.now()) == 1
    assert chain.confirmations(included_height=3, now=clock.now()) == 0


def test_alice_pays_and_money_moves_when_block_arrives(wallet: MockWallet, clock: FakeClock) -> None:
    treasury_before = balance(wallet, "treasury")
    approved = wallet.approve(request(wallet), "alice")
    assert approved.status == "approved" and approved.tx_id and approved.tx_id.startswith("0x")
    assert balance(wallet, "alice") == 10_000_000_000 - AMOUNT  # списано сразу
    tx = wallet.get_transaction(approved.tx_id)
    assert tx is not None and tx.status == "pending" and tx.confirmations == 0
    assert balance(wallet, "treasury") == treasury_before  # ещё не в блоке

    clock.advance_seconds(BLOCK)
    tx = wallet.get_transaction(approved.tx_id)
    assert tx is not None and tx.status == "confirmed" and tx.confirmations == 1
    assert balance(wallet, "treasury") == treasury_before + AMOUNT
    clock.advance_seconds(BLOCK * 2)
    tx = wallet.get_transaction(approved.tx_id)
    assert tx is not None and tx.confirmations == 3


def test_bob_has_insufficient_funds(wallet: MockWallet) -> None:
    result = wallet.approve(request(wallet), "bob")
    assert result.status == "failed" and result.failure_code == "insufficient_funds"
    assert result.tx_id is None
    assert balance(wallet, "bob") == 2_000_000


def test_carol_wallet_declines(wallet: MockWallet) -> None:
    result = wallet.approve(request(wallet), "carol")
    assert result.status == "failed" and result.failure_code == "wallet_declined"


def test_dave_transaction_waits_longer_for_a_block(wallet: MockWallet, clock: FakeClock) -> None:
    approved = wallet.approve(request(wallet), "dave")
    assert approved.tx_id
    clock.advance_seconds(BLOCK * 3)
    tx = wallet.get_transaction(approved.tx_id)
    assert tx is not None and tx.status == "pending"
    clock.advance_seconds(BLOCK * 3)
    tx = wallet.get_transaction(approved.tx_id)
    assert tx is not None and tx.status == "confirmed"


def test_eve_transaction_fails_in_the_network_and_money_comes_back(wallet: MockWallet, clock: FakeClock) -> None:
    approved = wallet.approve(request(wallet), "eve")
    assert approved.status == "approved" and approved.tx_id
    assert balance(wallet, "eve") == 10_000_000_000 - AMOUNT
    clock.advance_seconds(BLOCK)
    tx = wallet.get_transaction(approved.tx_id)
    assert tx is not None and tx.status == "failed" and tx.confirmations == 0 and tx.failure_reason
    assert balance(wallet, "eve") == 10_000_000_000  # деньги вернулись отправителю


def test_reject_and_reapprove_is_impossible(wallet: MockWallet) -> None:
    request_id = request(wallet)
    assert wallet.reject(request_id).failure_code == "payment_rejected"
    with pytest.raises(MockWalletError) as caught:
        wallet.approve(request_id, "alice")
    assert caught.value.code == "request_not_pending"


def test_request_expires(wallet: MockWallet, clock: FakeClock) -> None:
    request_id = request(wallet)
    clock.advance_minutes(16)
    found = wallet.get_payment_request(request_id)
    assert found is not None and found.status == "expired"


def test_cancel_only_pending_request(wallet: MockWallet) -> None:
    pending = request(wallet)
    canceled = wallet.cancel_payment_request(pending)
    assert canceled is not None and canceled.status == "canceled"
    approved_id = request(wallet)
    wallet.approve(approved_id, "alice")
    still_approved = wallet.cancel_payment_request(approved_id)
    assert still_approved is not None and still_approved.status == "approved"


def test_unknown_account_and_treasury_cannot_pay(wallet: MockWallet) -> None:
    request_id = request(wallet)
    for account in ("mallory", "treasury"):
        with pytest.raises(MockWalletError) as caught:
            wallet.approve(request_id, account)
        assert caught.value.code == "account_not_found"


def test_refund_from_treasury_is_idempotent(wallet: MockWallet, clock: FakeClock) -> None:
    alice = next(a for a in wallet.accounts() if a.id == "alice")
    first = wallet.send_from_treasury(to_address=alice.address, amount=1_000_000, currency="usdt", reference="re_1")
    again = wallet.send_from_treasury(to_address=alice.address, amount=1_000_000, currency="usdt", reference="re_1")
    assert first.id == again.id
    clock.advance_seconds(BLOCK)
    assert balance(wallet, "alice") == 10_000_000_000 + 1_000_000


def test_refund_fails_when_treasury_is_empty(wallet: MockWallet) -> None:
    with pytest.raises(MockWalletError) as caught:
        wallet.send_from_treasury(to_address="0x1", amount=10**30, currency="usdt", reference="re_2")
    assert caught.value.code == "insufficient_funds"


def test_unsupported_currency(wallet: MockWallet) -> None:
    with pytest.raises(MockWalletError):
        request(wallet, currency="doge")


def test_reset_restores_balances(wallet: MockWallet) -> None:
    wallet.approve(request(wallet), "alice")
    wallet.reset()
    assert balance(wallet, "alice") == 10_000_000_000
    assert wallet.transactions() == []


def test_addresses_are_stable_between_restarts(clock: FakeClock) -> None:
    first = MockWallet(now=clock.now).accounts()
    second = MockWallet(now=clock.now).accounts()
    assert [a.address for a in first] == [a.address for a in second]
