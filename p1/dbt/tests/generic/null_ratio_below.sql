{# 컬럼 null 비율이 임계값을 넘으면 실패(F-08: 결제 금액 null 급증 → 게시 차단) #}
{% test null_ratio_below(model, column_name, threshold) %}
with s as (
    select count(*) as n, count_if({{ column_name }} is null) as nulls from {{ model }}
)
select n, nulls, cast(nulls as double) / nullif(n, 0) as ratio
from s
where cast(nulls as double) / nullif(n, 0) > {{ threshold }}
{% endtest %}
