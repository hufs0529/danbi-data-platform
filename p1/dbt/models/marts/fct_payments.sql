{{ config(partitioned_by=['month(approved_date_kst)']) }}
-- 그레인: 결제 1건. 상태 이력에서 매입·취소 금액을 누적한다(부분취소 여러 번 포함)
with hist as (
    select
        payment_id,
        sum(case when to_status = 'CAPTURED' then amount else 0 end) as captured_amount,
        sum(case when to_status in ('PARTIAL_CANCELED', 'CANCELED') then amount else 0 end) as canceled_amount,
        count_if(to_status in ('PARTIAL_CANCELED', 'CANCELED')) as cancel_count
    from {{ ref('stg_payment_status_history') }}
    group by 1
)
select
    p.payment_id,
    p.payment_key,
    p.order_no,
    p.member_token,
    p.method,
    p.card_issuer,
    p.status,
    p.amount as authorized_amount,
    cast(coalesce(h.captured_amount, 0) as decimal(18, 0)) as captured_amount,
    cast(coalesce(h.canceled_amount, 0) as decimal(18, 0)) as canceled_amount,
    cast(coalesce(h.captured_amount, 0) - coalesce(h.canceled_amount, 0) as decimal(18, 0)) as net_amount,
    coalesce(h.cancel_count, 0) as cancel_count,
    p.approved_at,
    p.captured_at,
    {{ kst_date('p.approved_at') }} as approved_date_kst,
    {{ settle_date('p.captured_at') }} as expected_settle_date
from {{ ref('stg_payments') }} p
left join hist h on h.payment_id = p.payment_id
where p.status <> 'FAILED'
