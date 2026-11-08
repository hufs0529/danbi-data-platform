# 내부 ALB 삭제(시간당 과금), 수집 워커 0대. 큐·Lambda·EventBridge 는 남는다(사용량 과금이라 거의 0)
# 끈 동안의 웹훅은 버려지고 S3·Cognito 이벤트는 큐에 쌓인다 → 재개 후 대조 잡(P2-05)으로 위키·Git 공백을 메운다
hooks_enabled         = false
ingest_desired        = 0
reconcile_enabled     = false # 끈 동안 하트비트 경보가 울리지 않게 경보도 함께 사라진다
worker_desired        = 0
hwp_converter_desired = 0
