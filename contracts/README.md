# 데이터 계약 (contracts)

데이터셋마다 하나의 YAML. **이 파일이 단일 원본이다.** 여기서 다음이 만들어진다.

| 생성물 | 쓰는 곳 | 코드 |
| --- | --- | --- |
| Silver Iceberg DDL | `p1/lakehouse/migrate.py` (Athena) | `contracts/registry.py::athena_ddl` |
| 수집 시점 PII 정책(토큰화·암호화·마스킹·일반화) | Flink 수집 잡(P1-06) | `registry.py::pii_policy` |
| Silver 변환(형 변환·시간대·파생 컬럼) | Spark MERGE(P1-07) | `registry.py::silver_columns` |
| Lake Formation 태그·열 필터 | `p1-60-governance`(P1-12) | `registry.py::lf_columns` |
| 품질 검사·SLA | dbt 테스트, 관측(P1-14) | `quality`, `sla` |

## 필드

```yaml
dataset: silver.orders            # <계층>.<테이블>
owner: team-commerce-data
description: ...
grain: [order_id]                 # 1행의 의미 = 키
source: {db: commerce, table: orders}
update: merge                     # merge(현재 상태) | scd2(이력) | append(이벤트)
partition: ["month(ordered_at)"]
retention: {class: ecommerce_transaction_5y}
sla: {freshness_minutes: 15}
columns:
  - name: member_token
    type: string
    source: member_id             # Bronze JSON 의 필드
    transform: token:member       # 수집 시점(Flink)에서 적용 → Bronze 부터 평문 없음
    classification: personal_token
```

### transform

| 값 | 적용 시점 | 의미 |
| --- | --- | --- |
| `token:<kind>` | 수집(Flink) | HMAC 결정적 토큰. 같은 kind 끼리 조인 가능 |
| `encrypt:member` | 수집(Flink) | 회원별 데이터 키로 암호화(탈퇴 시 키 폐기 = crypto-shredding) |
| `mask:text` | 수집(Flink) | 자유 텍스트의 PII 탐지·마스킹 |
| `generalize:<fn>` | 수집(Flink) | 원문에서 파생값만 남김(`birth_year`, `zip3`, `sido`, `sigungu`). Bronze 필드명은 `<source>__<fn>` |
| `drop` | 수집(Flink) | Bronze 에 들이지 않는다 |
| `ts_iso`, `ts_kst_local_ms`, `date_days` | Silver(Spark) | Debezium 시간 표현 → UTC timestamp / date |
| (없음) | Silver | `type` 으로 형 변환 |

`derive:` 는 Silver 에서 다른 컬럼으로 계산한다(`kst_date(ordered_at)`).

### classification

`public` · `internal` · `personal_token`(토큰, 단독으로 식별 불가) · `personal_encrypted`(복호화 권한 필요) · `personal_generalized` · `restricted`. 고유식별정보·카드·계좌 번호는 레이크에 들이지 않으므로 등급이 없다.
