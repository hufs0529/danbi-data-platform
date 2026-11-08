# 작은 파일 폭증 (F-07)

**증상** `SmallFileRatio > 0.1`, Athena 쿼리 p95 상승. **원인** 체크포인트 15초 커밋, 5~15분 MERGE(merge-on-read 삭제 파일), 유지보수 잡 실패.

## 확인
```sql
SELECT count(*) AS files, count_if(file_size_in_bytes < 32*1024*1024) AS small,
       sum(file_size_in_bytes)/1024/1024 AS mb FROM "silver"."orders$files";
SELECT count(*) FROM "silver"."orders$snapshots";
```
## 조치
1. `make spark-job JOB=maintenance ARGS="--tables silver.orders"` (컴팩션·스냅샷 만료·고아 파일).
2. 매일 유지보수 DAG(`p1_iceberg_maintenance`) 실패 이력 확인.
3. 구조적이면: 체크포인트 간격(지연과 맞바꿈), MERGE 주기, 파티션 입도 재검토 → ADR.
## 검증
쿼리 p95 회복, 비율 ≤ 10%.
