# 실시간 매출 지연·늦은 이벤트 (F-05)

**증상** `rt_sales_freshness` 5분 초과, 대시보드 속보치 정체. **영향** 타임세일 중 마케팅 판단 지연. 확정치(다음 날 Gold)에는 영향 없음.

## 확인
1. 실시간 앱 상태·체크포인트(대시보드 "Flink 체크포인트").
2. 이벤트가 오고 있는가: `events.payment` 컨슈머 지연(`flink-realtime`).
3. 워터마크 정체: 한 파티션에만 이벤트가 없으면 유휴 처리(1분)가 동작해야 한다. 모든 파티션이 조용하면 창이 닫히지 않는다(정상: 결제가 없으면 매출도 0).

## 늦은 이벤트(F-05)
허용 지연 2분 안 → 창 재발화(revision+1). 그보다 늦으면 `rt.late_events`. 확정치는 Silver 에서 다시 계산하므로 100% 반영된다.
```sql
SELECT date_trunc('hour', occurred_at), count(*) FROM rt.late_events GROUP BY 1 ORDER BY 1 DESC LIMIT 24;
```
늦은 이벤트 비율이 1% 를 넘으면 허용 지연 재검토(상태 크기·지연과 맞바꿈) → ADR.
