-- 대사 2단계(Silver vs Gold): Gold 순매출을 Silver 원천에서 독립적으로 다시 계산해 비교한다
with recomputed as (
    select date(with_timezone(occurred_at, 'UTC') AT TIME ZONE 'Asia/Seoul') as date_kst,
           sum(case when to_status = 'CAPTURED' then amount
                    when to_status in ('PARTIAL_CANCELED', 'CANCELED') then -amount else 0 end) as net
    from {{ source('silver', 'payment_status_history') }}
    where not coalesce(is_deleted, false)
    group by 1
)
select r.date_kst, r.net as silver_net, g.net_sales as gold_net
from recomputed r
left join {{ ref('mart_daily_sales') }} g on g.date_kst = r.date_kst
where coalesce(g.net_sales, 0) <> r.net
