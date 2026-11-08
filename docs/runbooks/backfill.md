# 재처리·정정 (S-C2: 3일치 Gold 가 틀렸다)

1. **영향 범위**: MWAA `p1_backfill` 의 `impact` 태스크(Marquez 계보 하류 목록) → 공지 대상.
2. **원인 수정**: 모델·변환 코드 PR(테스트에 재현 케이스 추가).
3. **실행**: `p1_backfill` params `tables`, `start`, `end`, `publish=false` → Silver 재처리(멱등) → gold_audit 재빌드·테스트.
4. **확인**: gold_audit 결과를 소유 팀과 검토(숫자 차이 표).
5. **게시**: 같은 DAG 를 `publish=true` 로 → INSERT OVERWRITE(원자적), 대시보드 중단 0.
6. **공지**: 템플릿 — "{기간} {지표}가 {원인}으로 {차이}만큼 잘못 표시되었고 {시각}에 정정되었습니다. 정정 전후 값: …"
7. 두 번 실행해도 결과가 같은지 확인(멱등), 포스트모템.
