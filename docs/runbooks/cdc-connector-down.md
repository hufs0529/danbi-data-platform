# 커넥터 중단 (cdc_connector_down)

**증상** `Danbi/Pipeline ConnectorRunning < 1` 5분. **영향** Bronze 이후 전 계층이 멈춘다(속보치·Silver·Gold 지연). 데이터는 소스 binlog/WAL 에 남아 있어 **보존 기간(binlog 72h, WAL 4GB) 안이면 유실이 없다.**

## 확인 순서
```bash
aws kafkaconnect list-connectors --query 'connectors[].{n:connectorName,s:connectorState}'
aws logs tail /danbi/dev/msk-connect --since 30m | grep -E "ERROR|Exception" | tail -20
```
| 로그 키워드 | 원인 | 조치 |
| --- | --- | --- |
| `Access denied`, `password authentication failed` | 계정 암호 회전 | `make db-migrate ENGINE=…` 후 커넥터 재생성 |
| `Schema being registered is incompatible` | 비호환 DDL | [schema-change](schema-change.md) |
| `binlog ... no longer available`, `replication slot ... invalidated` | 보존 초과 | [binlog-purged](binlog-purged.md) |
| `TimeoutException`, `UNKNOWN_TOPIC` | MSK·토픽 문제 | `make topics ACTION=plan` |
| `SecretsManager ... timed out` | NAT/엔드포인트 | [network-debugging](network-debugging.md) |

## 조치
커넥터 재생성(오프셋은 `connect.offsets.cdc` 에 있어 이어받는다):
```bash
make plan STACK=p1-20-streaming   # -var connectors_enabled=false → apply → true → apply
```
## 사후
30분 이상 중단이면 [cdc-completeness](cdc-completeness.md) 로 유실 0 확인 후 포스트모템.
