-- 커넥터가 publication 을 자동 생성하면(FOR ALL TABLES) 슈퍼유저 권한이 필요하고 범위가 넓어진다.
-- 마스터 계정이 필요한 테이블만 담아 만들고, 커넥터는 publication.autocreate.mode=disabled 로 쓴다.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'dbz_publication') THEN
    CREATE PUBLICATION dbz_publication FOR TABLE
      payments, payment_status_history, refunds, outbox_events, debezium_signal, debezium_heartbeat;
  END IF;
END $$;
