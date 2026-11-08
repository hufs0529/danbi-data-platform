# 런북: 연결이 안 될 때 (SG·NACL·라우팅)

## 증상

- ECS 태스크·Lambda·MSK Connect가 RDS, MSK, OpenSearch, 외부 API에 연결하지 못한다(타임아웃).
- 타임아웃은 대개 네트워크(라우팅·NACL·SG), 즉시 거부(connection refused)는 대상 프로세스 문제다.

## 확인 순서

1. **라우팅**: 출발 서브넷의 라우트 테이블에 목적지 경로가 있는가
   - 앱 → 인터넷: `rt-app-*` 에 `0.0.0.0/0 → nat-*`. `make pause` 후에는 NAT가 없다(의도된 동작)
   - 데이터 계층은 인터넷 경로가 **없다**(설계). 데이터 계층에서 바깥으로 나가야 한다면 설계를 다시 본다
2. **보안 그룹**: 대상 SG가 출발지의 *클라이언트 SG*를 허용하는가 (`docs` 의 보안 그룹 매트릭스)
3. **NACL**: 상태가 없으므로 *양방향*을 본다
   - 앱 → 데이터 요청: data NACL 인바운드에 서비스 포트(3306/5432/9098/443) + data NACL 아웃바운드에 1024–65535 → 앱 계층
   - 앱 → 인터넷 응답: app NACL 인바운드 200번(1024–65535 from 0.0.0.0/0)
4. **흐름 로그로 확인**: `infra/stacks/10-network/sql/vpc_flow_logs.sql` 의 쿼리로 `action = 'REJECT'` 를 본다
   - REJECT 가 같은 5-튜플로 *들어오는 방향*에서만 보이면 SG, *응답 방향*에서 보이면 NACL 응답 규칙을 의심한다
5. **Reachability Analyzer**: 콘솔에서 출발 ENI → 대상 ENI 경로 분석. 어느 구성 요소에서 막히는지 알려 준다

## 자주 겪는 원인

| 원인 | 신호 | 조치 |
| --- | --- | --- |
| NAT 없음(`pause` 상태) | 앱 계층에서 외부 HTTPS 타임아웃 | `make resume` 또는 인터페이스 엔드포인트 켜기 |
| 클라이언트 SG 미부착 | 같은 서브넷인데 특정 워크로드만 실패 | 워크로드에 `sg-*-clients` 부착 |
| 데이터 NACL 포트 누락 | 새 서비스(예: Redis 6379) 추가 후 실패 | `data_service_ports` 에 포트 추가 → 규칙 번호 자동 부여 |
| 임시 포트 범위가 좁음 | 일부 연결만 간헐적 실패 | 1024–65535 확인(리눅스 기본값만 열면 NAT·ELB 응답이 막힌다) |
| 인터페이스 엔드포인트 DNS | NAT를 껐는데 SDK가 퍼블릭 엔드포인트로 감 | VPC `enableDnsHostnames`·엔드포인트 `private_dns_enabled` 확인 |
