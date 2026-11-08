# 포스트모템: F-01 Debezium 커넥터 30분 중단 (훈련, 예시)

> 실제 배포 후 이 형식으로 채운다. 아래 수치는 **훈련 설계상 목표값**이며 측정값이 아니다.

- 날짜: 2026-10-30 · 심각도: SEV3(훈련) · 런북: docs/runbooks/cdc-connector-down.md

## 요약
1. 14:00 커넥터 2개를 삭제해 30분 동안 CDC 를 멈췄다.
2. `cdc_connector_down` 알림은 5분 안에 와야 하고, 재생성 후 `connect.offsets.cdc` 에서 이어받아야 한다.
3. 소스 대사(1시간 창 3개)가 모두 OK 이고 Silver 중복이 0 이면 통과.

## 타임라인
| 시각 | 사건 | 근거 |
| --- | --- | --- |
| 14:00 | `connectors_enabled=false` apply | Terraform 실행 로그 |
| ≤14:05 | 알림(목표) | SNS 메일 |
| 14:30 | `connectors_enabled=true` apply | |
| 14:3x | 커넥터 RUNNING, 오프셋 재개 로그 | /danbi/dev/msk-connect |
| 15:40 | 소스 대사 OK | ops.recon_source_results |

## 확인할 질문
- 재생성 후 초기 스냅샷이 다시 돌지는 않았나(오프셋 토픽 이름 고정이 동작했나)?
- 중단 동안 binlog 보존(72h)·WAL 상한(4GB) 중 어느 쪽이 먼저 위험해지나? 결제 DB 하트비트가 슬롯을 전진시켰나?
- 알림 메일만 보고 런북의 첫 명령까지 몇 분 걸렸나?
