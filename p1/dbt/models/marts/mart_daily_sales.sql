{{ config(partitioned_by=['month(date_kst)']) }}
-- 그레인: KST 일자 1행. 취소는 '발생일' 기준(기본값). 원주문일 기준은 mart_daily_sales_by_order_date
-- GMV = 결제 승인 금액 합(취소 전, 쿠폰 할인 후 결제액). 순매출 = 매입 - 취소(환불)
with approved as (
    select approved_date_kst as date_kst,
           sum(authorized_amount) as gmv,
           count(*) as payments,
           count(distinct member_token) as buyers
    from {{ ref('fct_payments') }}
    where approved_at is not null
    group by 1
),
movements as (
    select {{ kst_date('occurred_at') }} as date_kst,
           sum(case when to_status = 'CAPTURED' then amount else 0 end) as captured,
           sum(case when to_status in ('PARTIAL_CANCELED', 'CANCELED') then amount else 0 end) as canceled
    from {{ ref('stg_payment_status_history') }}
    group by 1
)
select
    coalesce(a.date_kst, m.date_kst) as date_kst,
    cast(coalesce(a.gmv, 0) as decimal(18, 0)) as gmv,
    cast(coalesce(m.captured, 0) as decimal(18, 0)) as captured_amount,
    cast(coalesce(m.canceled, 0) as decimal(18, 0)) as canceled_amount,
    cast(coalesce(m.captured, 0) - coalesce(m.canceled, 0) as decimal(18, 0)) as net_sales,
    coalesce(a.payments, 0) as payments,
    coalesce(a.buyers, 0) as buyers,
    cast(coalesce(a.gmv, 0) / nullif(a.payments, 0) as decimal(18, 0)) as aov,
    'event_date' as cancel_basis,
    '{{ var("metric_version") }}' as metric_version
from approved a
full outer join movements m on m.date_kst = a.date_kst
