# 작업 규칙

## 태스크와 ID

- 모든 작업은 태스크 단위다. ID는 `C01`(공통), `P1-04`(프로젝트 1), `P2-09`(프로젝트 2) 형식이다.
- 태스크마다 `docs/tasks/<ID>.md`가 있고, 목표·산출물·적용 방법·검증·완료 기준을 적는다.
- 태스크 하나가 끝나면 `main`에 커밋 하나가 남는다(스쿼시 머지).

## 브랜치

```
main                     # 항상 plan이 깨지지 않는 상태
task/P1-04-debezium      # 태스크 브랜치. 작업 중 커밋은 자유롭게
```

1. `git switch -c task/P1-04-debezium`
2. 작업하고 로컬 검증(`make lint test tf-fmt`)
3. PR 생성 → CI(ruff, pytest, terraform fmt/validate, checkov, 해당 스택 plan)가 통과해야 한다
4. **Squash and merge** → `main`에 태스크당 커밋 1개
5. 인프라 변경이면 `deploy` 워크플로를 수동 실행(plan 확인 → apply 승인)

## 커밋 메시지

[Conventional Commits](https://www.conventionalcommits.org/) + 태스크 ID.

```
feat(p1-cdc): MSK Connect에 Debezium MySQL·PostgreSQL 커넥터 배포 [P1-04]

- 왜: 운영 DB 부하 없이 생성·수정·삭제를 수집 (ADR-01)
- 무엇: 플러그인 빌드 스크립트, 워커 설정(오프셋 토픽 고정), 커넥터 2종
- 검증: docs/tasks/P1-04.md 의 "검증" 절

Task: P1-04
```

- type: `feat` `fix` `infra` `docs` `test` `chore` `refactor`
- scope: `common` `network` `security` `ci` `p1-*` `p2-*`
- 본문에 **왜 / 무엇 / 검증**을 적는다. `Task:` 트레일러는 필수다(`scripts/check_commit_msg.py`가 검사).

## 인프라 변경

- 스택 단위로 plan → 리뷰 → apply. 한 PR에서 여러 스택을 바꾸면 적용 순서를 PR 본문에 적는다.
- 파괴적 변경(교체, 삭제)이 plan에 나오면 PR 본문에 이유와 데이터 영향을 적는다.
- state는 S3(버전 관리 + 네이티브 잠금)에만 있다. 로컬 state 커밋 금지.

## 파이썬

- 파이썬 3.11 이상, `ruff`로 lint·format.
- AWS를 부르는 코드는 클라이언트를 주입받게 만들어 단위 테스트에서 가짜로 바꿀 수 있게 한다.
- 비밀은 코드·이미지·환경 파일에 넣지 않는다. Secrets Manager 또는 SSM SecureString에서 읽는다.

## ADR

- 결정이 생기면 `docs/adr/NNNN-제목.md`를 템플릿(`0000-template.md`)으로 추가한다.
- 결정을 바꾸면 기존 ADR을 지우지 않고 `대체됨(ADR-xxxx)`으로 표시한다.
