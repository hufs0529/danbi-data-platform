-- RDS MySQL 은 binlog 보관 기간 기본값이 NULL(가능한 빨리 삭제)이다.
-- 커넥터가 멈춘 동안 binlog 가 지워지면 재스냅샷밖에 방법이 없다(F-03).
-- 72시간 = 주말 동안 커넥터가 멈춰도 월요일 아침에 이어받을 수 있는 최소치. 디스크 사용량과 맞바꾼다.
CALL mysql.rds_set_configuration('binlog retention hours', 72);
