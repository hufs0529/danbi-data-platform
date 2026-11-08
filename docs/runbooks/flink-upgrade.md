# 상태 있는 Flink 잡 업그레이드 (F-09)

**목표** 상태 유실 0, 중복 알림 0.

## 절차
1. 변경이 상태 호환인지 확인: 상태 연산자의 `uid`(`fraud-detector-v1`, `sales-1m-v1` …)를 바꾸지 않았나? 상태 타입(PICKLED)을 바꾸지 않았나?
2. 새 zip 업로드(`build_flink_app.sh realtime --upload`), `realtime_app_key` 변경 → apply. Managed Flink 가 **스냅샷을 만들고** 새 코드로 그 스냅샷에서 시작한다(`snapshots_enabled=true`).
3. 확인: 업그레이드 직전 2번 결제한 도용 카드로 3번째 결제 → 알림 1건(상태 유지), 같은 카드 추가 결제 → 알림 없음(쿨다운 유지).
4. 실패 시 이전 zip 키로 되돌림(이전 스냅샷에서 시작).

## 상태 비호환 변경일 때
새 uid(`-v2`)로 배포하고 `allowNonRestoredState` 로 시작 → 탐지 이력 5분 공백을 감수, 리스크팀 사전 공지. ADR 기록.
