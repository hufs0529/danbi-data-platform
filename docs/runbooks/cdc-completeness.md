# 소스 vs 레이크 불일치 (대사 1단계)

**증상** `SourceLakeMismatch > 0`(매시 :40, 1시간 창, 30분 지연 여유).

## 확인
```sql
SELECT * FROM ops.recon_source_results WHERE NOT ok ORDER BY checked_at DESC LIMIT 20;
```
| 패턴 | 원인 후보 | 조치 |
| --- | --- | --- |
| 레이크 < 소스, 최근 창만 | 지연(여유 30분 부족) | 다음 실행에서 해소되는지 확인 |
| 레이크 < 소스, 특정 구간 | 커넥터 중단·binlog 유실 | [binlog-purged](binlog-purged.md) 증분 스냅샷 |
| 레이크 > 소스 | 소스 하드 삭제(레이크는 is_deleted 제외 집계) | 삭제 이벤트 수신 확인 |
| 금액만 다름 | 같은 행의 늦은 갱신 순서 | `_src_seq` 확인, MERGE 재실행 |
