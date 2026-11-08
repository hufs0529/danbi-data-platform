{{ config(partitioned_by=['month(date_kst)']) }}
-- 전환율 = purchase 세션 수 / 방문 세션 수 (봇 제외, 규칙 버전 기록)
with s as (
    select
        {{ kst_date('event_time') }} as date_kst,
        platform,
        session_id,
        max(bot_rule_version) as bot_rule_version,
        max(case when event_type = 'add_to_cart' then 1 else 0 end) as has_cart,
        max(case when event_type = 'checkout_start' then 1 else 0 end) as has_checkout,
        max(case when event_type = 'purchase' then 1 else 0 end) as has_purchase
    from {{ ref('stg_events_clean') }}
    group by 1, 2, 3
)
select
    date_kst,
    platform,
    count(*) as sessions,
    sum(has_cart) as cart_sessions,
    sum(has_checkout) as checkout_sessions,
    sum(has_purchase) as purchase_sessions,
    cast(sum(has_purchase) as double) / nullif(count(*), 0) as conversion_rate,
    max(bot_rule_version) as bot_rule_version,
    '{{ var("metric_version") }}' as metric_version
from s
group by 1, 2
