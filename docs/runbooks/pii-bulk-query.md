# 대량 개인정보 조회·비상 접근 (F-10)

**증상** 접속기록 점검 `BULK_PERSONAL_QUERY`·`NIGHT_PERSONAL_QUERY`·`REPEATED_DENIED`, `breakglass-used` 알림.

## 절차
1. 접속기록에서 해당 사용자·쿼리 확인(`s3://<audit>/access-logs/dt=…/access.jsonl`, `query_id`, `subject_tokens`).
2. 사용자·팀장에게 **목적 확인**(업무 필요성). 기록: 점검 리포트 '조치 기록' 표.
3. 정당하지 않으면: 권한 회수(LF·접근 요청 YAML 삭제 PR), 보안팀 보고, 다운로드 여부 확인(Athena 결과 버킷 접근 로그).
4. 비상 접근은 사용 후 24시간 안에 사후 검토(사유·조회 범위·종료 확인).
## 증빙
점검 리포트(월간) + 조치 기록 + 체인 검증 결과(`access_log verify`).
