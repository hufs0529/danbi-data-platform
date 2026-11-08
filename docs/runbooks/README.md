# 런북

모든 알림은 여기 있는 문서 하나로 연결된다(알람 설명·DAG 실패 메시지·SNS 본문의 링크). 형식은 같다.

**증상 → 영향 → 확인 순서(명령어) → 조치(경우별) → 에스컬레이션 → 사후(포스트모템 필요 여부)**

| 런북 | 알림·상황 | 관련 훈련 |
| --- | --- | --- |
| [cdc-connector-down](cdc-connector-down.md) | 커넥터 RUNNING 아님 | F-01 |
| [binlog-purged](binlog-purged.md) | 커넥터가 binlog/WAL 위치를 잃음 | F-03 |
| [mysql-failover](mysql-failover.md) | 소스 DB 장애 조치 | F-02 |
| [ingest-stalled](ingest-stalled.md) | Bronze 신선도·컨슈머 지연·체크포인트 실패 | — |
| [dlq](dlq.md) | DLQ 유입 | F-04 |
| [silver-freshness](silver-freshness.md) | Silver 30분 초과 | — |
| [realtime-lag](realtime-lag.md) | 실시간 매출 지연, 늦은 이벤트 | F-05 |
| [pg-settlement](pg-settlement.md) | 정산 파일 격리·미도착, 대사 미설명 차이 | F-06, S-B2 |
| [small-files](small-files.md) | 작은 파일 비율 > 10% | F-07 |
| [gold-publish](gold-publish.md) | Gold 게시 실패·지연(WAP 차단) | F-08 |
| [flink-upgrade](flink-upgrade.md) | 상태 있는 잡 업그레이드 | F-09 |
| [pii-bulk-query](pii-bulk-query.md) | 대량 개인정보 조회·비상 접근 | F-10 |
| [cdc-completeness](cdc-completeness.md) | 소스 vs 레이크 불일치 | F-01, F-03 |
| [schema-change](schema-change.md) | 소스 스키마 변경(호환·비호환) | S-C1 |
| [backfill](backfill.md) | 재처리·정정 | S-C2 |
| [erasure](erasure.md) | 파기 실패·72시간 초과 | S-G4 |
| [network-debugging](network-debugging.md) | 연결 실패(SG·NACL·라우팅) | — |
