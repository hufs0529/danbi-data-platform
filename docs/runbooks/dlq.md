# DLQ 유입 (F-04)

**증상** `DlqMessages > 0`. **영향** 해당 메시지만 Bronze 에 없다(나머지는 정상 처리). DLQ 는 원문(개인정보 가능)이 있어 보존 72시간 — **그 안에 처리**한다.

## 확인
DLQ 메시지는 `{source_topic, source_partition, source_offset, error, raw_value}`. 오류 유형별 집계:
```bash
# stream-admin 태스크(VPC 안)에서 kafka-console-consumer 대신 파이썬 도구로 오류 문자열만 집계
make topics ACTION=plan  # 토픽 상태 확인
```
| 오류 | 원인 | 조치 |
| --- | --- | --- |
| `매직 바이트` | 레지스트리 없이 쓴 프로듀서 | 생산자 수정, 재처리 불필요(쓰레기) |
| `Avro 해석 실패(schema_id=…)` | 스키마 미등록·삭제 | 스키마 복구 후 재처리 |
| `알 수 없는 소스 DB` | 새 DB 가 CDC 범위에 들어옴 | 계약·정책 추가 후 재처리 |

## 재처리
원인 수정 후 DLQ 의 `raw_value` 를 원래 토픽 같은 키로 다시 쓰고, Bronze 에서 `(kafka_topic, kafka_partition, kafka_offset)` 중복을 확인한다. 장기 분석이 필요하면 72시간 안에 S3 격리 버킷(landing/quarantine, 접근 제한)으로 옮긴다.
