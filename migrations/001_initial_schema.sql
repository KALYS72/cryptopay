-- CryptoPay: платёжная система в стиле Stripe с имитацией криптокошелька.
-- Суммы — NUMERIC(78,0): целые числа минимальных единиц любой длины (1 ETH = 10^18 wei).
-- Целостность обеспечивает сама база: UNIQUE, CHECK, внешние ключи.

CREATE TABLE merchants (
    id                  TEXT PRIMARY KEY,
    email               TEXT        NOT NULL UNIQUE,
    business_name       TEXT        NOT NULL,
    password_hash       TEXT        NOT NULL,
    api_key_hash        TEXT        UNIQUE,
    api_key_last4       TEXT,
    api_key_created_at  TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL
);

CREATE TABLE payment_intents (
    id                      TEXT PRIMARY KEY,
    merchant_id             TEXT          NOT NULL REFERENCES merchants (id) ON DELETE CASCADE,
    amount                  NUMERIC(78,0) NOT NULL CHECK (amount > 0),
    currency                TEXT          NOT NULL,
    description             TEXT,
    metadata                JSONB         NOT NULL DEFAULT '{}',
    status                  TEXT          NOT NULL CHECK (status IN
                                ('requires_payment_method', 'requires_action', 'processing', 'succeeded', 'canceled')),
    checkout_session_id     TEXT,
    customer_email          TEXT,
    wallet_request_id       TEXT,
    wallet_tx_id            TEXT,
    payer_address           TEXT,
    confirmations           INTEGER       NOT NULL DEFAULT 0 CHECK (confirmations >= 0),
    required_confirmations  INTEGER       NOT NULL CHECK (required_confirmations > 0),
    amount_received         NUMERIC(78,0) NOT NULL DEFAULT 0 CHECK (amount_received >= 0),
    amount_refunded         NUMERIC(78,0) NOT NULL DEFAULT 0 CHECK (amount_refunded >= 0),
    last_error_code         TEXT,
    last_error_message      TEXT,
    cancellation_reason     TEXT,
    created_at              TIMESTAMPTZ   NOT NULL,
    succeeded_at            TIMESTAMPTZ,
    canceled_at             TIMESTAMPTZ,
    version                 INTEGER       NOT NULL DEFAULT 0,
    -- нельзя вернуть больше, чем получено
    CHECK (amount_refunded <= amount_received),
    -- одна транзакция в сети не может оплатить два платежа
    CONSTRAINT payment_intents_wallet_tx_unique UNIQUE (wallet_tx_id)
);
CREATE INDEX payment_intents_merchant_created_idx ON payment_intents (merchant_id, created_at DESC, id DESC);
-- частичный индекс: фоновая сверка смотрит только незавершённые попытки оплаты
CREATE INDEX payment_intents_waiting_idx ON payment_intents (created_at)
    WHERE status IN ('requires_action', 'processing');

CREATE TABLE checkout_sessions (
    id                 TEXT PRIMARY KEY,
    merchant_id        TEXT          NOT NULL REFERENCES merchants (id) ON DELETE CASCADE,
    payment_intent_id  TEXT          NOT NULL UNIQUE REFERENCES payment_intents (id) ON DELETE CASCADE,
    amount             NUMERIC(78,0) NOT NULL CHECK (amount > 0),
    currency           TEXT          NOT NULL,
    description        TEXT,
    success_url        TEXT,
    cancel_url         TEXT,
    status             TEXT          NOT NULL CHECK (status IN ('open', 'complete', 'expired')),
    payment_status     TEXT          NOT NULL CHECK (payment_status IN ('unpaid', 'paid')),
    created_at         TIMESTAMPTZ   NOT NULL,
    expires_at         TIMESTAMPTZ   NOT NULL,
    completed_at       TIMESTAMPTZ
);
CREATE INDEX checkout_sessions_merchant_created_idx ON checkout_sessions (merchant_id, created_at DESC, id DESC);
CREATE INDEX checkout_sessions_open_expiry_idx ON checkout_sessions (expires_at) WHERE status = 'open';

CREATE TABLE refunds (
    id                   TEXT PRIMARY KEY,
    merchant_id          TEXT          NOT NULL REFERENCES merchants (id) ON DELETE CASCADE,
    payment_intent_id    TEXT          NOT NULL REFERENCES payment_intents (id) ON DELETE CASCADE,
    amount               NUMERIC(78,0) NOT NULL CHECK (amount > 0),
    currency             TEXT          NOT NULL,
    reason               TEXT CHECK (reason IN ('requested_by_customer', 'duplicate', 'fraudulent')),
    status               TEXT          NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed')),
    destination_address  TEXT          NOT NULL,
    wallet_tx_id         TEXT UNIQUE,
    failure_reason       TEXT,
    created_at           TIMESTAMPTZ   NOT NULL,
    completed_at         TIMESTAMPTZ
);
CREATE INDEX refunds_merchant_created_idx ON refunds (merchant_id, created_at DESC, id DESC);
CREATE INDEX refunds_pending_idx ON refunds (created_at) WHERE status = 'pending';

-- Журнал проводок: только INSERT. Баланс = SUM(net).
CREATE TABLE balance_transactions (
    id           TEXT PRIMARY KEY,
    merchant_id  TEXT          NOT NULL REFERENCES merchants (id) ON DELETE CASCADE,
    type         TEXT          NOT NULL CHECK (type IN ('payment', 'refund', 'refund_failure')),
    source_id    TEXT          NOT NULL,
    amount       NUMERIC(78,0) NOT NULL,
    fee          NUMERIC(78,0) NOT NULL CHECK (fee >= 0),
    net          NUMERIC(78,0) NOT NULL,
    currency     TEXT          NOT NULL,
    description  TEXT          NOT NULL,
    created_at   TIMESTAMPTZ   NOT NULL,
    CHECK (net = amount - fee)
);
CREATE INDEX balance_transactions_merchant_created_idx
    ON balance_transactions (merchant_id, created_at DESC, id DESC);
-- одна проводка типа «оплата» на один платёж: повторная сверка не начислит деньги дважды
CREATE UNIQUE INDEX balance_transactions_one_payment_idx ON balance_transactions (source_id) WHERE type = 'payment';

CREATE TABLE events (
    id           TEXT PRIMARY KEY,
    merchant_id  TEXT        NOT NULL REFERENCES merchants (id) ON DELETE CASCADE,
    type         TEXT        NOT NULL,
    subject_id   TEXT        NOT NULL,
    data         JSONB       NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX events_merchant_created_idx ON events (merchant_id, created_at DESC, id DESC);
CREATE INDEX events_subject_idx ON events (subject_id);

CREATE TABLE webhook_endpoints (
    id              TEXT PRIMARY KEY,
    merchant_id     TEXT        NOT NULL REFERENCES merchants (id) ON DELETE CASCADE,
    url             TEXT        NOT NULL,
    secret          TEXT        NOT NULL,
    enabled_events  TEXT[]      NOT NULL,
    description     TEXT,
    status          TEXT        NOT NULL CHECK (status IN ('enabled', 'disabled')),
    created_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX webhook_endpoints_merchant_idx ON webhook_endpoints (merchant_id);

-- Transactional Outbox: доставки вебхуков пишутся в той же транзакции, что и событие.
CREATE TABLE webhook_deliveries (
    id                TEXT PRIMARY KEY,
    merchant_id       TEXT        NOT NULL REFERENCES merchants (id) ON DELETE CASCADE,
    event_id          TEXT        NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    endpoint_id       TEXT        NOT NULL,
    event_type        TEXT        NOT NULL,
    url               TEXT        NOT NULL,
    status            TEXT        NOT NULL CHECK (status IN ('pending', 'delivered', 'failed')),
    attempts          INTEGER     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at   TIMESTAMPTZ NOT NULL,
    last_error        TEXT,
    last_status_code  INTEGER,
    delivered_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL
);
CREATE INDEX webhook_deliveries_due_idx ON webhook_deliveries (next_attempt_at) WHERE status = 'pending';
CREATE INDEX webhook_deliveries_event_idx ON webhook_deliveries (event_id);

-- Ответы на запросы с Idempotency-Key (хранятся 24 часа).
CREATE TABLE idempotency_keys (
    merchant_id      TEXT        NOT NULL REFERENCES merchants (id) ON DELETE CASCADE,
    key              TEXT        NOT NULL,
    request_hash     TEXT        NOT NULL,
    status           TEXT        NOT NULL CHECK (status IN ('processing', 'completed')),
    response_status  INTEGER,
    response_body    TEXT,
    created_at       TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (merchant_id, key)
);
CREATE INDEX idempotency_keys_created_idx ON idempotency_keys (created_at);
