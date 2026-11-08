# MSK 클러스터를 지운다(브로커 시간 과금 중단). 토픽 데이터와 커넥터 오프셋도 사라진다.
# 재개하면 Debezium 이 초기 스냅샷부터 다시 읽고, Silver MERGE 가 중복을 흡수한다(멱등성 설계의 실전 검증).
msk_enabled = false
