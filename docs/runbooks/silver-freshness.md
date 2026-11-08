# Silver 신선도 초과

**증상** `silver_orders_freshness` 30분 초과. **영향** 분 단위 소비자(운영 리포트) 지연, 07:00 Gold 게시 위험.

## 확인 순서
1. Bronze 는 신선한가? 아니면 [ingest-stalled](ingest-stalled.md).
2. MWAA `p1_silver_merge` 최근 실행: 실패·대기·지연?
   ```bash
   aws emr-serverless list-job-runs --application-id <app> --max-results 5 --query 'jobRuns[].{n:name,s:state}'
   ```
3. EMR 로그(`s3://<artifacts>/emr-logs/…/stderr.gz`)의 예외.

| 원인 | 조치 |
| --- | --- |
| 용량 상한(16 vCPU) 도달·큐 대기 | 동시 잡(유지보수·백필) 정리, 상한 일시 상향 |
| `CommitFailedException`(동시 쓰기) | 백필·파기와 겹침 → 순서 조정. MERGE 는 멱등이라 재실행 안전 |
| 드리프트 컬럼 추가 실패 | `ops.schema_drift` 확인 → [schema-change](schema-change.md) |
| 비용 이상(스캔 폭증) | 겹침 창(10분) 확인, 작은 파일 → [small-files](small-files.md) |

## 조치
수동 실행: `make spark-job JOB=silver_merge`. 같은 구간을 다시 처리해도 결과는 같다.
