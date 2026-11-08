# 파기 워크플로 실패·지연 (S-G4)

**증상** `erasure-failed`, `erasure-slo-breach`(72시간). **법정 기한** 5일 — 내부 SLO 와 사이 48시간이 여유다.

## 확인
```bash
aws dynamodb get-item --table-name danbi-dev-erasure-ledger --key '{"request_id":{"S":"erase-<token>"}}'
aws stepfunctions describe-execution --execution-arn <arn> --query '{s:status,e:error,c:cause}'
```
## 조치
- 모든 단계가 멱등 → 원인 해결 후 **같은 이름이 아닌** 새 실행(이름에 `-retry1`)을 같은 입력으로 시작한다(실행 이름은 90일 재사용 불가).
- `Verify` 실패(남은 행): 결과의 테이블 목록 확인 → 파기 대상 목록(`erasure_lambda.py`)에 빠진 테이블이 있으면 코드 수정·재실행. 새 테이블에 member_token 이 생기면 반드시 목록에 추가(계약 리뷰 체크리스트).
- `ExpireSnapshots` 실패: EMR 로그 → [silver-freshness](silver-freshness.md) 의 EMR 항목.
## 증빙
대장 `VERIFIED` + 검증 결과, P2 전파 이벤트 수신 확인(P2 대장).
