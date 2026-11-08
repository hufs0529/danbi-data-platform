# 소스 스키마 변경 (S-C1)

## 호환 변경(컬럼 추가)
자동: Debezium 이 새 스키마 등록(BACKWARD) → Bronze JSON 에 필드 → Silver 잡이 드리프트로 기록하고 문자열 컬럼 추가(30분 안 노출) → `ops.schema_drift` PENDING. 할 일: 계약에 컬럼·분류 추가 PR(CI: governance·compat) → `migrate apply` → 드리프트 RESOLVED. 분류 전까지 분석가 열 필터에는 없다.

## 비호환 변경(타입·이름 변경, 삭제)
1. 1차 방어: 운영팀 PR 에서 계약 CI 실패(소스 DDL 전수 매핑).
2. 2차 방어: 레지스트리가 새 스키마를 거부 → 커넥터 태스크 실패([cdc-connector-down](cdc-connector-down.md) 알림).
3. 절차: 새 컬럼으로 병행(v2) → 소비자 영향 목록(Marquez 계보) → 30일 병행 → 옛 컬럼 deprecated → 다음 메이저에서 삭제. 레지스트리 호환성 규칙을 끄지 않는다.
