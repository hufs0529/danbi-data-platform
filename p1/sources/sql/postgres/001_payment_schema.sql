-- 단비페이 결제 서비스 (PostgreSQL 16, DB: payment)
-- 일부러 넣은 현실 문제
--   * 부분취소: canceled_amount 가 여러 번 늘어난다
--   * 상태 역전: 승인 이벤트가 매입 이벤트보다 늦게 도착할 수 있다(생성기가 주입)
--   * 도메인 이벤트는 행 변경이 아니라 outbox_events 로 발행(FR-02)

CREATE TABLE IF NOT EXISTS payments (
  payment_id      BIGSERIAL     PRIMARY KEY,
  payment_key     VARCHAR(64)   NOT NULL UNIQUE,    -- PG 거래 ID (정산 파일과 매칭 키)
  order_no        VARCHAR(32)   NOT NULL,
  member_id       BIGINT        NOT NULL,
  method          VARCHAR(16)   NOT NULL,           -- CARD, TRANSFER, EASY_PAY
  card_token      VARCHAR(64)   NULL,               -- PG 가 준 토큰. 카드번호는 받지 않는다
  card_issuer     VARCHAR(16)   NULL,
  status          VARCHAR(20)   NOT NULL,           -- READY, APPROVED, CAPTURED, PARTIAL_CANCELED, CANCELED, FAILED
  amount          NUMERIC(12,0) NOT NULL,
  canceled_amount NUMERIC(12,0) NOT NULL DEFAULT 0,
  currency        CHAR(3)       NOT NULL DEFAULT 'KRW',
  approved_at     TIMESTAMPTZ   NULL,
  captured_at     TIMESTAMPTZ   NULL,
  updated_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
  version         INT           NOT NULL DEFAULT 0  -- 낙관적 락. 같은 결제의 변경 순서 판정에도 쓴다
);
CREATE INDEX IF NOT EXISTS ix_payments_order_no ON payments (order_no);

CREATE TABLE IF NOT EXISTS payment_status_history (
  id          BIGSERIAL     PRIMARY KEY,
  payment_id  BIGINT        NOT NULL REFERENCES payments (payment_id),
  from_status VARCHAR(20)   NULL,
  to_status   VARCHAR(20)   NOT NULL,
  amount      NUMERIC(12,0) NOT NULL,
  occurred_at TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS refunds (
  refund_id    BIGSERIAL     PRIMARY KEY,
  payment_id   BIGINT        NOT NULL REFERENCES payments (payment_id),
  amount       NUMERIC(12,0) NOT NULL,
  reason       VARCHAR(50)   NOT NULL,
  status       VARCHAR(16)   NOT NULL,              -- REQUESTED, COMPLETED, REJECTED
  requested_at TIMESTAMPTZ   NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ   NULL
);

-- Transactional Outbox: 결제 상태 변경과 같은 트랜잭션에서 INSERT 한다.
-- 컬럼 이름은 Debezium Outbox Event Router 기본값을 따른다.
CREATE TABLE IF NOT EXISTS outbox_events (
  id            UUID         PRIMARY KEY,
  aggregatetype VARCHAR(64)  NOT NULL,              -- payment → 토픽 events.payment
  aggregateid   VARCHAR(64)  NOT NULL,              -- payment_key → 메시지 키(같은 결제는 같은 파티션)
  type          VARCHAR(64)  NOT NULL,              -- PaymentApproved, PaymentCaptured, PaymentCanceled ...
  payload       JSONB        NOT NULL,
  occurred_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS debezium_signal (
  id   VARCHAR(64) PRIMARY KEY,
  type VARCHAR(32) NOT NULL,
  data TEXT        NULL
);

-- 결제 DB 가 한가해도 복제 슬롯이 전진하도록 커넥터가 주기적으로 쓰는 테이블.
-- RDS 는 다른 DB 의 WAL 때문에 슬롯이 WAL 을 붙잡아 디스크가 차는 일이 잦다.
CREATE TABLE IF NOT EXISTS debezium_heartbeat (
  id INT PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL
);
INSERT INTO debezium_heartbeat (id, ts) VALUES (1, now()) ON CONFLICT (id) DO NOTHING;
