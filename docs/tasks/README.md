# 태스크 색인

태스크 1개 = `main`의 커밋 1개다. 태스크의 커밋은 `git log --grep="^Task: P1-04"`로 찾는다.
각 문서에 목표·산출물·적용·검증·완료 기준이 있다.

## 공통

| ID | 제목 | 스택·경로 |
| --- | --- | --- |
| [C01](C01.md) | 저장소 골격과 작업 규칙 | 루트 |
| [C02](C02.md) | Terraform 부트스트랩(state, OIDC, 예산) | `infra/bootstrap` |
| [C03](C03.md) | VPC·서브넷·라우트 테이블·NACL·엔드포인트 | `infra/modules/network`, `stacks/10-network` |
| [C04](C04.md) | 보안 기본선(KMS, CloudTrail, 감사 버킷, 역할) | `stacks/20-security` |
| [C05](C05.md) | CI/CD(GitHub Actions + OIDC) | `.github/workflows` |
| [C06](C06.md) | 플랫폼(ECS, ECR, 운영 EC2)과 공통 라이브러리 | `stacks/30-platform`, `libs/danbi_common` |

## P1 — 실시간 CDC 레이크하우스

| ID | 제목 | 스택·경로 |
| --- | --- | --- |
| [P1-01](P1-01.md) | 소스 DB(RDS MySQL·PostgreSQL)와 스키마 | `stacks/p1-10-sources`, `p1/sources` |
| [P1-02](P1-02.md) | 상태 기계 데이터 생성기 | `p1/generator` |
| [P1-03](P1-03.md) | MSK와 스키마 레지스트리 | `stacks/p1-20-streaming`, `p1/streaming/schema_registry` |
| [P1-04](P1-04.md) | Debezium on MSK Connect | `stacks/p1-20-streaming`, `p1/cdc` |
| [P1-05](P1-05.md) | 레이크하우스 저장소·카탈로그·Athena | `stacks/p1-30-lakehouse`, `p1/lakehouse` |
| [P1-06](P1-06.md) | Flink 수집 잡(토큰화 → Bronze) | `stacks/p1-40-processing`, `p1/streaming/ingest` |
| [P1-07](P1-07.md) | Silver MERGE·SCD2(EMR Serverless) | `stacks/p1-40-processing`, `p1/batch` |
| [P1-08](P1-08.md) | Gold 모델(dbt-athena)과 WAP 게시 | `p1/dbt` |
| [P1-09](P1-09.md) | PG 정산 파일 수집과 대사 | `p1/recon`, `stacks/p1-30-lakehouse` |
| [P1-10](P1-10.md) | 오케스트레이션(MWAA + DAG) | `stacks/p1-50-orchestration`, `p1/orchestration` |
| [P1-11](P1-11.md) | 실시간 매출·이상결제 탐지 | `stacks/p1-40-processing`, `p1/streaming/realtime` |
| [P1-12](P1-12.md) | 거버넌스(LF-Tag, 데이터 필터, 계보, 계약) | `stacks/p1-60-governance`, `contracts` |
| [P1-13](P1-13.md) | 개인정보 파기와 보존(crypto-shredding) | `stacks/p1-60-governance`, `p1/privacy` |
| [P1-14](P1-14.md) | 관측·SLO·알림 | `stacks/p1-70-observability`, `p1/observability` |
| [P1-15](P1-15.md) | 장애 훈련 도구와 런북 | `p1/drills`, `docs/runbooks` |

## P2 — 권한 인지형 Knowledge Lake + RAG

| ID | 제목 | 스택·경로 |
| --- | --- | --- |
| [P2-01](P2-01.md) | 사내 앱(BookStack·Gitea), 파일 버킷, Cognito | `stacks/p2-10-apps` |
| [P2-02](P2-02.md) | 가상 문서 말뭉치와 시더 | `p2/corpus` |
| [P2-03](P2-03.md) | Knowledge Lake 저장소와 문서 레지스트리 | `stacks/p2-20-knowledge`, `p2/lake` |
| [P2-04](P2-04.md) | 변경 수집(웹훅·S3·Cognito 이벤트 → SQS) | `stacks/p2-30-pipeline`, `p2/ingestion` |
| [P2-05](P2-05.md) | 정기 전수 대조 | `p2/ingestion/reconciler` |
| [P2-06](P2-06.md) | 파싱·분류·PII 마스킹·인젝션 탐지 | `p2/worker` |
| [P2-07](P2-07.md) | 청크·증분 임베딩 | `p2/worker` |
| [P2-08](P2-08.md) | OpenSearch 도메인·인덱스·색인 워커 | `stacks/p2-20-knowledge`, `stacks/p2-30-pipeline`, `p2/search` |
| [P2-09](P2-09.md) | 권한 트리밍 질의 API | `stacks/p2-40-serving`, `p2/api` |
| [P2-10](P2-10.md) | 삭제 전파 워크플로 | `stacks/p2-50-workflows`, `p2/workflows/deletion` |
| [P2-11](P2-11.md) | 블루/그린 재색인 워크플로 | `stacks/p2-50-workflows`, `p2/workflows/reindex` |
| [P2-12](P2-12.md) | 평가 하네스와 CI 게이트 | `p2/eval`, `.github/workflows/eval.yml` |
