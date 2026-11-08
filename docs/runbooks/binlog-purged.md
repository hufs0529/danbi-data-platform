# binlog·WAL 위치 유실 (F-03)

**증상** 커넥터 실패 로그: `The connector is trying to read binlog starting at ... but this is no longer available on the server` (MySQL) / `replication slot "debezium_payment" ... invalidated` (PG, `max_slot_wal_keep_size` 초과). **영향** 중단 구간의 변경을 스트림으로는 다시 받을 수 없다. 목표: 4시간 안에 정상화, 데이터 갭 0.

## 절차
1. 갭 구간 확정: 마지막 정상 Bronze `max(source_ts)` ~ 지금.
   ```sql
   SELECT table_name, max(source_ts) FROM bronze.cdc_events WHERE source_db = 'commerce' GROUP BY 1;
   ```
2. 오프셋을 버리고 스트리밍만 재개: 커넥터를 `snapshot.mode=no_data`(스냅샷 없이 현재 위치부터)로 1회 재생성. PG 는 슬롯을 지우고 다시 만든다.
3. 갭은 증분 스냅샷으로 메운다(락 없음, 스트리밍과 병행):
   ```bash
   python -m p1.cdc.signal --engine mysql snapshot commerce.orders commerce.order_items commerce.members \
     --where "updated_at >= '<갭 시작 UTC>'"
   ```
   `updated_at` 이 없는 테이블은 전체 증분 스냅샷. 하드 삭제는 스냅샷으로 복원되지 않는다 → 소스 대사로 차이를 찾아 Silver 에 수동 표시(삭제 기록 남김).
4. 커넥터 설정을 `snapshot.mode=initial` 로 되돌린다.
5. 검증: [cdc-completeness](cdc-completeness.md) 갭 구간 창 전부 OK.

## 재발 방지 후보
binlog 보존 시간 증가(디스크 비용), 커넥터 중단 알림 기준 단축, PG 하트비트 확인.
