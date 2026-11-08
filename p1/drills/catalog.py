"""장애 훈련 카탈로그(F-01~F-10). 훈련 = 주입 → 관찰(알림·지표) → 복구 → 검증 → 포스트모템.

각 훈련은 '기대 동작'과 '수용 기준'을 측정 가능한 형태로 갖고, 검증 SQL 은 자동으로 돌릴 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Drill:
    id: str
    title: str
    inject: tuple[str, ...]
    expect: str
    accept: str
    verify_sql: tuple[str, ...] = ()
    restore: tuple[str, ...] = ()
    runbook: str = ""
    evidence: tuple[str, ...] = field(default_factory=tuple)


DRILLS = [
    Drill("F-01", "Debezium 커넥터 30분 중단",
          ("make gen-start", "make plan STACK=p1-20-streaming  # -var connectors_enabled=false 로 apply", "sleep 1800"),
          "오프셋 토픽(connect.offsets.cdc)에서 재개, 중복은 Silver MERGE 가 흡수",
          "유실 0(소스 대사 창 전부 OK), 알림 5분 내",
          ("SELECT check_table, ok FROM ops.recon_source_results WHERE checked_at > current_timestamp - INTERVAL '3' HOUR",
           "SELECT order_id, count(*) FROM silver.orders GROUP BY 1 HAVING count(*) > 1"),
          ("make plan STACK=p1-20-streaming  # connectors_enabled=true 로 apply",),
          "cdc-connector-down.md", ("알림 수신 시각", "재개 후 커넥터 로그의 오프셋", "대사 결과")),
    Drill("F-02", "MySQL primary 교체(재부팅·장애 조치)",
          ("aws rds reboot-db-instance --db-instance-identifier danbi-dev-mysql",),
          "GTID·binlog 위치로 이어받기", "유실·중복 검증 리포트 0건",
          ("SELECT check_table, ok FROM ops.recon_source_results WHERE checked_at > current_timestamp - INTERVAL '2' HOUR",),
          (), "mysql-failover.md", ("RDS 이벤트", "커넥터 재연결 로그")),
    Drill("F-03", "binlog 보존 초과(위치 유실)",
          ("CALL mysql.rds_set_configuration('binlog retention hours', 1);  -- dev 에서만",
           "커넥터 중단 2시간 + 생성기 가동", "커넥터 재생성"),
          "감지 → snapshot.mode=no_data 재개 → 갭 증분 스냅샷 → 갭 검증", "4시간 내 정상화, 데이터 갭 0",
          ("SELECT check_table, ok FROM ops.recon_source_results WHERE checked_at > current_timestamp - INTERVAL '6' HOUR",),
          ("CALL mysql.rds_set_configuration('binlog retention hours', 72);",), "binlog-purged.md",
          ("오류 로그", "신호 테이블 기록", "갭 구간 대사")),
    Drill("F-04", "역직렬화 실패 메시지(독약)",
          ("stream-admin 태스크에서 cdc.mysql.commerce.orders 에 0x01 로 시작하는 바이트 1건 쓰기",),
          "DLQ 로 격리, 나머지 계속 처리", "정상 처리량 유지, DlqMessages=1, 재처리 절차 존재",
          ("SELECT count(*) FROM bronze.cdc_events WHERE ingest_ts > current_timestamp - INTERVAL '10' MINUTE",),
          (), "dlq.md", ("DLQ 메시지 error 필드", "처리량 그래프")),
    Drill("F-05", "앱 오프라인 후 2시간 늦은 이벤트",
          ("python -m p1.generator.cli run --faults late=0.2 --duration 600",),
          "허용 지연 안은 창 재발화, 초과분은 rt.late_events → 확정치는 배치가 반영", "확정치에 100% 반영",
          ("SELECT count(*) FROM rt.late_events WHERE seen_at > current_timestamp - INTERVAL '3' HOUR",
           "SELECT count(*) FROM silver.events_clean WHERE delay_seconds > 7200"),
          (), "realtime-lag.md", ("late_events 건수", "다음 날 Gold vs 속보치 차이")),
    Drill("F-06", "PG 파일 중복·정정·CP949 깨짐·헤더 변경",
          ("python -m p1.generator.cli settle --date <D> --faults duplicate,correction,skew=0.01 --upload",
           "python -m p1.generator.cli settle --date <D+1> --faults broken_encoding --upload",
           "python -m p1.generator.cli settle --date <D+2> --faults header_v2 --upload"),
          "해시로 중복 차단, 정정본 차수 유효본, 인코딩·헤더 판정, 격리", "잘못된 파일이 Silver 에 없음",
          ("SELECT file_name, status, reason FROM bronze.pg_settlement_files ORDER BY received_at DESC LIMIT 10",
           "SELECT DISTINCT file_seq FROM silver.pg_settlement WHERE settle_date = DATE '<D>'"),
          (), "pg-settlement.md", ("파일 대장", "격리 위치", "대사 리포트")),
    Drill("F-07", "작은 파일 폭증",
          ("체크포인트 간격 2초로 1시간 운영(-var checkpoint_interval_ms=2000)", "유지보수 DAG 일시 중지"),
          "SmallFileRatio 알림 → 컴팩션·스냅샷 만료", "쿼리 p95 회복, 비율 ≤ 10%",
          ('SELECT count(*), count_if(file_size_in_bytes < 33554432) FROM "bronze"."cdc_events$files"',),
          ("checkpoint_interval_ms=15000 로 복구", "make spark-job JOB=maintenance"), "small-files.md",
          ("컴팩션 전후 파일 수", "쿼리 시간")),
    Drill("F-08", "결제 금액 null 급증",
          ("python -m p1.generator.cli run --faults null_amount=0.3 --duration 1800",),
          "dbt null_ratio_below 실패 → 게시 차단, 이전 스냅샷 유지", "잘못된 Gold 게시 0, 담당자 알림",
          ("SELECT model, published_at FROM ops.publish_log ORDER BY published_at DESC LIMIT 5",),
          (), "gold-publish.md", ("run_results 실패 테스트", "gold 스냅샷 변화 없음")),
    Drill("F-09", "상태 있는 Flink 잡 업그레이드",
          ("도용 카드로 결제 2건", "realtime 앱 새 버전 배포(스냅샷 자동)", "같은 카드 3번째 결제"),
          "스냅샷에서 상태 복원 → 3번째에서 알림 1건", "상태 유실 0, 중복 알림 0",
          (), (), "flink-upgrade.md", ("스냅샷 이름", "리스크 SNS 알림 1건")),
    Drill("F-10", "분석가 대량 PII 조회 시도",
          ("analyst 역할로 SELECT member_token FROM gold.fct_order_items (권한 거부 ×5)",
           "engineer 역할로 심야에 silver.members 5만 행 조회"),
          "LF 거부 + 접속기록 + 점검 알림", "차단되고 점검 리포트에 표시",
          (), ('aws lambda invoke --function-name danbi-dev-access-log --payload \'{"mode":"inspect"}\' out.json',),
          "pii-bulk-query.md", ("접속기록 줄", "점검 리포트 발견 항목")),
]  # fmt: skip


def get(drill_id: str) -> Drill:
    for d in DRILLS:
        if d.id == drill_id:
            return d
    raise KeyError(drill_id)
