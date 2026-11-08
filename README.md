# danbi-data-platform

가상의 커머스·간편결제 회사 **단비마켓(단비페이)** 의 데이터 플랫폼을 AWS 서울 리전(ap-northeast-2)에 구축하는 포트폴리오 저장소다.

- **P1 — 실시간 CDC 기반 거버넌스 레이크하우스**: RDS(MySQL·PostgreSQL) → Debezium(MSK Connect) → MSK → Managed Flink → S3 Iceberg(Bronze/Silver/Gold) → Athena + Lake Formation
- **P2 — 권한 인지형 Knowledge Lake + RAG**: 위키·Git·파일 서버 → 변경 수집(SQS) → 파싱·PII 마스킹 → Iceberg → OpenSearch(Nori + 벡터) → 권한 트리밍 질의 API

저장소는 **태스크 1개 = 커밋 1개** 규칙으로 쌓였다. 태스크 목록과 각 태스크의 적용·검증 절차는 [`docs/tasks/README.md`](docs/tasks/README.md)에 있다.

## 디렉터리

```
infra/
  bootstrap/        # tfstate 버킷, GitHub OIDC 배포 역할, 예산 알림 (로컬 state로 1회 적용)
  modules/          # 재사용 모듈 (network, secure_bucket, ecs_service ...)
  stacks/           # 적용 단위. 이름 순서 = 적용 순서
  envs/             # 환경별 변수 (dev, paused)
libs/danbi_common/  # P1·P2 공용 파이썬 라이브러리 (PII 토큰화·탐지, 설정, 로깅)
p1/                 # P1 애플리케이션 코드
p2/                 # P2 애플리케이션 코드
contracts/          # 데이터 계약 (YAML)
docs/               # ADR, 런북, 태스크, 규칙
```

## 빠른 시작

전제: AWS 계정, 관리자 권한(부트스트랩 1회), Terraform 1.10 이상(또는 OpenTofu 1.10 이상), Python 3.12 이상, Docker.

```bash
# 1) 부트스트랩: state 버킷과 배포 역할 (로컬 state)
cd infra/bootstrap && terraform init && terraform apply -var="github_repo=<owner>/danbi-data-platform" -var="alert_email=<you@example.com>"

# 2) backend.hcl 작성 (bootstrap 출력값 사용)
cp infra/backend.hcl.example infra/backend.hcl

# 3) 스택 적용 (이름 순서대로)
make init  STACK=10-network
make plan  STACK=10-network
make apply STACK=10-network

# 4) 쓰지 않을 때 비싼 구성 끄기 / 다시 켜기
make pause
make resume
```

적용 순서: `10-network` → `20-security` → `30-platform` → `p1-10-sources` → `p1-20-streaming` → `p1-30-lakehouse` → `p1-40-processing` → `p1-50-orchestration` → `p1-60-governance` → `p1-70-observability` → `p2-10-apps` → `p2-20-knowledge` → `p2-30-pipeline` → `p2-40-serving` → `p2-50-workflows`. 내릴 때는 역순이다.

## 로컬 검증

```bash
pip install -e ".[dev]"
export PYTHONPATH=.:libs   # p1·p2·libs 를 패키지로 import (pytest 는 pyproject 설정으로 자동)
make lint      # ruff
make test      # pytest (AWS 없이 도는 단위 테스트)
make tf-fmt    # terraform fmt -check
make tf-validate
```

## 비용 주의

MSK, MWAA, OpenSearch, NAT 게이트웨이, Managed Flink(실행 중일 때)는 켜져 있는 시간만큼 과금된다. 실습이 끝나면 `make pause`를 실행하고, 예산 알림(bootstrap)이 설정되어 있는지 확인한다.
