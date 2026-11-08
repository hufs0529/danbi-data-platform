-- 단비마켓 주문 서비스 (MySQL 8.0, DB: commerce)
-- 일부러 넣은 현실 문제
--   * members.joined_at 은 레거시라 KST 로컬 시각(DATETIME), 나머지는 UTC(TIMESTAMP)
--   * cs_tickets.content 는 자유 텍스트라 PII 가 섞일 수 있다 → 수집 시 탐지·마스킹 대상
--   * order_items 는 대용량(증분 스냅샷 S-C3 대상)
--   * 하드 삭제가 일어나는 테이블: member_addresses, coupon_usages(취소 시)

CREATE TABLE IF NOT EXISTS members (
  member_id        BIGINT       NOT NULL AUTO_INCREMENT,
  email            VARCHAR(255) NOT NULL,
  name             VARCHAR(50)  NOT NULL,
  phone            VARCHAR(20)  NOT NULL,
  birth_date       DATE         NULL,
  grade            VARCHAR(16)  NOT NULL DEFAULT 'BASIC',   -- BASIC, SILVER, GOLD, VIP (SCD2 대상)
  marketing_agreed TINYINT(1)   NOT NULL DEFAULT 0,
  status           VARCHAR(16)  NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE, DORMANT, WITHDRAWN
  joined_at        DATETIME(3)  NOT NULL,                   -- KST 로컬 시각 (레거시)
  withdrawn_at     DATETIME(3)  NULL,
  updated_at       TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (member_id),
  UNIQUE KEY uk_members_email (email)
);

CREATE TABLE IF NOT EXISTS member_addresses (
  address_id      BIGINT       NOT NULL AUTO_INCREMENT,
  member_id       BIGINT       NOT NULL,
  label           VARCHAR(20)  NOT NULL,
  recipient_name  VARCHAR(50)  NOT NULL,
  recipient_phone VARCHAR(20)  NOT NULL,
  zipcode         CHAR(5)      NOT NULL,
  address1        VARCHAR(200) NOT NULL,
  address2        VARCHAR(200) NULL,
  is_default      TINYINT(1)   NOT NULL DEFAULT 0,
  created_at      TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at      TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (address_id),
  KEY ix_addr_member (member_id)
);

CREATE TABLE IF NOT EXISTS products (
  product_id BIGINT       NOT NULL AUTO_INCREMENT,
  name       VARCHAR(200) NOT NULL,
  category   VARCHAR(50)  NOT NULL,
  brand      VARCHAR(100) NULL,
  status     VARCHAR(16)  NOT NULL DEFAULT 'ON_SALE',
  created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (product_id)
);

-- 현재 가격만 덮어쓴다. 이력은 CDC 로 레이크에서 SCD2 로 복원한다("주문 시점 가격" 질문)
CREATE TABLE IF NOT EXISTS product_prices (
  product_id BIGINT        NOT NULL,
  list_price DECIMAL(12,0) NOT NULL,
  sale_price DECIMAL(12,0) NOT NULL,
  currency   CHAR(3)       NOT NULL DEFAULT 'KRW',
  updated_at TIMESTAMP(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (product_id)
);

CREATE TABLE IF NOT EXISTS coupons (
  coupon_id       BIGINT        NOT NULL AUTO_INCREMENT,
  code            VARCHAR(32)   NOT NULL,
  discount_type   VARCHAR(10)   NOT NULL,      -- AMOUNT, RATE
  discount_value  DECIMAL(12,2) NOT NULL,
  max_discount    DECIMAL(12,0) NULL,
  total_quantity  INT           NOT NULL,
  issued_quantity INT           NOT NULL DEFAULT 0,
  valid_from      TIMESTAMP(3)  NOT NULL,
  valid_to        TIMESTAMP(3)  NOT NULL,
  updated_at      TIMESTAMP(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (coupon_id),
  UNIQUE KEY uk_coupon_code (code)
);

CREATE TABLE IF NOT EXISTS orders (
  order_id        BIGINT        NOT NULL AUTO_INCREMENT,
  order_no        VARCHAR(32)   NOT NULL,
  member_id       BIGINT        NOT NULL,
  address_id      BIGINT        NULL,
  status          VARCHAR(20)   NOT NULL,      -- CREATED, PAID, SHIPPED, DELIVERED, CANCELED, PARTIAL_CANCELED
  total_amount    DECIMAL(12,0) NOT NULL,      -- 할인 전 상품 금액 합
  discount_amount DECIMAL(12,0) NOT NULL DEFAULT 0,
  coupon_id       BIGINT        NULL,
  currency        CHAR(3)       NOT NULL DEFAULT 'KRW',
  ordered_at      TIMESTAMP(3)  NOT NULL,      -- UTC
  updated_at      TIMESTAMP(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (order_id),
  UNIQUE KEY uk_order_no (order_no),
  KEY ix_orders_member (member_id),
  KEY ix_orders_ordered_at (ordered_at)
);

CREATE TABLE IF NOT EXISTS order_items (
  order_item_id   BIGINT        NOT NULL AUTO_INCREMENT,
  order_id        BIGINT        NOT NULL,
  product_id      BIGINT        NOT NULL,
  quantity        INT           NOT NULL,
  unit_price      DECIMAL(12,0) NOT NULL,      -- 주문 시점 판매가
  discount_amount DECIMAL(12,0) NOT NULL DEFAULT 0,
  status          VARCHAR(16)   NOT NULL DEFAULT 'ORDERED',  -- ORDERED, CANCELED
  created_at      TIMESTAMP(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at      TIMESTAMP(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (order_item_id),
  KEY ix_items_order (order_id)
);

CREATE TABLE IF NOT EXISTS coupon_usages (
  usage_id  BIGINT       NOT NULL AUTO_INCREMENT,
  coupon_id BIGINT       NOT NULL,
  member_id BIGINT       NOT NULL,
  order_id  BIGINT       NOT NULL,
  used_at   TIMESTAMP(3) NOT NULL,
  PRIMARY KEY (usage_id),
  KEY ix_usage_coupon (coupon_id)
);

CREATE TABLE IF NOT EXISTS cs_tickets (
  ticket_id    BIGINT       NOT NULL AUTO_INCREMENT,
  member_id    BIGINT       NOT NULL,
  order_id     BIGINT       NULL,
  category     VARCHAR(30)  NOT NULL,
  status       VARCHAR(16)  NOT NULL DEFAULT 'OPEN',
  content      TEXT         NOT NULL,          -- 자유 텍스트: 고객이 전화번호·계좌를 적기도 한다
  satisfaction TINYINT      NULL,              -- 1~5 (S-C4 신규 데이터셋)
  created_at   TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at   TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (ticket_id),
  KEY ix_ticket_member (member_id)
);

-- Debezium 증분 스냅샷 신호 테이블 (S-C3: 2천만 행 재스냅샷을 락 없이)
CREATE TABLE IF NOT EXISTS debezium_signal (
  id   VARCHAR(64) NOT NULL,
  type VARCHAR(32) NOT NULL,
  data TEXT        NULL,
  PRIMARY KEY (id)
);
