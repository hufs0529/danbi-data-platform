# 소스 DB 장애 조치 (F-02)

**증상** RDS 이벤트 `Multi-AZ failover` 또는 재부팅 후 커넥터 재연결 오류. **영향** 몇 분 지연. RDS Multi-AZ 는 블록 수준 동기 복제라 binlog 파일·위치가 그대로이고, GTID 가 켜져 있어 커넥터가 같은 지점에서 이어받는다.

## 확인
```bash
aws rds describe-events --source-type db-instance --duration 120
aws kafkaconnect list-connectors --query 'connectors[].connectorState'
```
## 조치
1. 커넥터가 스스로 재연결(`errors.max.retries=-1`). 10분 안에 RUNNING 이 아니면 재생성([cdc-connector-down](cdc-connector-down.md)).
2. 리플리카 승격(다른 binlog)이었다면 GTID 기반 이어받기 확인: 커넥터 로그 `GTID set` 줄.
3. 유실·중복 검증: 장애 시각 ±1시간 [cdc-completeness](cdc-completeness.md). 중복은 Silver MERGE(`_src_seq`)가 흡수한다.
## 훈련(dev)
`aws rds reboot-db-instance --db-instance-identifier danbi-dev-mysql` (단일 AZ 는 재부팅으로 대체) → 위 검증 리포트 첨부.
