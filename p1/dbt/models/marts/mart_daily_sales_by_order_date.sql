{{ config(partitioned_by=['month(date_kst)']) }}
-- 취소를 원주문(승인)일로 되돌려 붙인 순매출. 프로모션 효과 분석용. 과거 일자의 숫자가 나중에 바뀐다(대시보드에 표시)
select
    approved_date_kst as date_kst,
    cast(sum(captured_amount) as decimal(18, 0)) as captured_amount,
    cast(sum(canceled_amount) as decimal(18, 0)) as canceled_amount,
    cast(sum(net_amount) as decimal(18, 0)) as net_sales,
    'order_date' as cancel_basis,
    '{{ var("metric_version") }}' as metric_version
from {{ ref('fct_payments') }}
where approved_at is not null
group by 1
