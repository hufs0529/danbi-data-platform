{{ config(materialized='table', table_type='iceberg', tags=[]) }}
-- 시맨틱 레이어용 날짜 축(2024-01-01 ~ 2027-12-31)
select cast(d as date) as date_day
from unnest(sequence(date '2024-01-01', date '2027-12-31', interval '1' day)) as t(d)
