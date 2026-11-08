# Gold 게시 실패·지연 (F-08)

**증상** `gold_daily_publish`(06:30 KST 미게시), `p1_gold_daily` 실패. **영향** 07:00 재무·경영 대시보드가 어제 값을 보여준다(**틀린 값을 보여주지는 않는다** — WAP).

## 확인 순서
1. 어느 단계? `silver_fresh`(Silver 지연 → [silver-freshness](silver-freshness.md)) / `dbt_build_audit` / `publish_gold`.
2. dbt 실패면 `s3://<artifacts>/dbt/runs/<run_id>/run_results.json` 에서 실패 테스트.
   | 실패 테스트 | 의미 | 조치 |
   | --- | --- | --- |
   | `null_ratio_below` | 결제 금액 null 급증(F-08) | 원천 확인(outbox payload), 결제팀 연락. 게시하지 않는다 |
   | `assert_net_sales_matches_silver` | Silver↔Gold 불일치 | 모델 로직·중복 확인 |
   | `unique`·`not_null` | 원천 중복·누락 | Silver MERGE·CDC 확인 |
3. 소비자 공지: "오늘 07:00 지표는 전일 기준, 원인 조사 중".

## 이미 게시된 값이 틀렸을 때
`make spark-job JOB=publish_gold ARGS="--models <m> --rollback"` → 직전 스냅샷 복귀(`ops.publish_log`), 이후 [backfill](backfill.md).
