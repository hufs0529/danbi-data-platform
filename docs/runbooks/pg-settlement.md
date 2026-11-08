# PG 정산 파일·대사 (F-06, S-B2)

**증상** `QuarantinedFiles > 0`, `UnexplainedCount > 0`, 08:30 대사에서 파일 미도착. **영향** 재무 09:00 보고.

## 격리 파일
```sql
SELECT file_name, status, reason, received_at FROM bronze.pg_settlement_files ORDER BY received_at DESC LIMIT 20;
```
| 사유 | 조치 |
| --- | --- |
| `encoding` | PG 사에 CP949 재전송 요청. 임의 변환 금지(글자 깨짐이 금액 열을 밀 수 있다) |
| `header` | 새 헤더 버전이면 `p1/recon/settlement.py` HEADERS 추가(PR·테스트) 후 같은 파일 재적재 |
| `trailer` | 잘린 파일 → 재전송 요청 |
| `row N` | 해당 행 확인 → PG 사 문의. 부분 적재하지 않는다 |
재적재: `make recon CMD='["load-settlement","--bucket","…","--key","…","--version-id","…"]'`

## 미설명 차이
`s3://<gold>/_reports/recon/dt=D/report.md` 의 원인 코드별: `PG_MISSING`·`AMOUNT_DIFF` → PG 사 문의·정정본 요청 / `INTERNAL_STATUS_LAG` → [cdc-completeness](cdc-completeness.md) / `UNKNOWN_TXN` → 즉시 리스크·재무 팀장 에스컬레이션(내부에 없는 거래).

## 파일 미도착
08:30 대사 리포트는 '파일 없음'으로 나간다(보류 건). 09:00 까지 PG 사 연락, 재무팀 공지.
