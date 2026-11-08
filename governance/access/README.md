# 기간제 접근 요청 (S-G2)

요청 하나 = YAML 하나 = PR 하나. 승인(리뷰 2인: 데이터 소유 팀 + 보안)을 받아 병합하면 다음 배포에서 권한이 생기고,
`expires_at` 이 지나면 매일 도는 `access-expiry` 워크플로가 회수한다(최대 24시간 지연, "지체 없이" 회수의 운영 정의).
부여·변경·말소 이력은 Git 이력과 CloudTrail(Lake Formation GrantPermissions/RevokePermissions)에 남는다.

```yaml
id: 2026-10-30-kim-cohort
principal_arn: arn:aws:iam::<acct>:role/danbi-dev-analyst
requester: kim@danbi.example
reason: VIP 이탈 코호트 분석(회원 토큰으로 주문·클릭 조인 필요)
approved_by: [lee@danbi.example, sec@danbi.example]
ticket: DATA-1234
scope:
  layer: [gold]
  classification: [personal_token]
expires_at: "2026-11-29T00:00:00Z"   # 최대 30일
```
