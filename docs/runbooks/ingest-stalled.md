# Bronze 수집 정체

**증상** `bronze_ingest_freshness` > 10분, `ingest_consumer_lag` > 60초, Flink 체크포인트 실패. **영향** 모든 하류 지연. 체크포인트가 실패하면 Iceberg 커밋이 멈춘다(데이터는 Kafka 에 남음, 토픽 보존 48h 안에서 유실 없음).

## 확인 순서
```bash
aws kinesisanalyticsv2 describe-application --application-name danbi-dev-bronze-ingest --query 'ApplicationDetail.ApplicationStatus'
aws logs tail /danbi/dev/flink --since 30m --log-stream-names bronze-ingest | grep -E "Exception|FAILED" | tail
```
| 원인 | 신호 | 조치 |
| --- | --- | --- |
| 처리량 부족 | 지연이 계속 증가, CPU 높음 | 병렬도 상향(`ingest_parallelism`), 파티션 수 확인 |
| 체크포인트 타임아웃 | `Checkpoint expired` | 간격 늘림, S3 지연·작은 파일 확인 |
| 키 저장소·KMS 스로틀링 | `ThrottlingException` | 회원 키 캐시 TTL 확인, KMS 할당량 |
| Glue 커밋 충돌 | `CommitFailedException` | 같은 테이블에 다른 작성자(유지보수 잡) 동시 실행 여부 |
| 스키마 레지스트리 불가 | `schemas/ids` 연결 오류 | ECS 서비스 상태, SG |

## 조치
재시작은 스냅샷에서(상태·오프셋 유지): 콘솔 또는 `flink_running=false → true`. 스냅샷 없이 재시작하지 않는다.
