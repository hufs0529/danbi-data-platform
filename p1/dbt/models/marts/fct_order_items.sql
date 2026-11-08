{{ config(partitioned_by=['month(ordered_date_kst)']) }}
-- 그레인: 주문 아이템 1행
-- 주문 할인은 아이템 금액 비율로 배분하고(원 미만 절사), 절사로 남는 차액은 마지막 아이템에 몰아준다.
-- 그래야 아이템 합계 = 주문 할인이 정확히 맞는다(singular test assert_discount_allocation_exact).
with items as (
    select * from {{ ref('stg_order_items') }}
),
orders as (
    select * from {{ ref('stg_orders') }}
),
gross as (
    select
        i.*,
        cast(i.unit_price * i.quantity as decimal(18, 0)) as gross_amount,
        sum(cast(i.unit_price * i.quantity as decimal(18, 0))) over (partition by i.order_id) as order_gross,
        row_number() over (partition by i.order_id order by i.order_item_id desc) as rn_desc
    from items i
),
alloc as (
    select
        g.*,
        o.order_no, o.member_token, o.ordered_at, o.ordered_date_kst, o.discount_amount as order_discount,
        cast(floor(o.discount_amount * g.gross_amount / nullif(g.order_gross, 0)) as decimal(18, 0)) as base_discount
    from gross g
    join orders o on o.order_id = g.order_id
),
alloc_fixed as (
    select
        a.*,
        a.base_discount + case when a.rn_desc = 1
            then a.order_discount - sum(a.base_discount) over (partition by a.order_id) else 0 end as allocated_discount
    from alloc a
)
select
    f.order_item_id,
    f.order_id,
    f.order_no,
    f.member_token,
    f.product_id,
    p.category,
    f.quantity,
    f.unit_price,
    f.gross_amount,
    cast(f.allocated_discount as decimal(18, 0)) as discount_amount,
    cast(f.gross_amount - f.allocated_discount as decimal(18, 0)) as net_amount,
    f.status as item_status,
    f.ordered_at,
    f.ordered_date_kst,
    g.grade as member_grade_at_order,           -- SCD2 시점 조인(S-B4)
    pr.sale_price as catalog_price_at_order,
    f.unit_price = pr.sale_price as price_matches_catalog
from alloc_fixed f
left join {{ ref('stg_products') }} p on p.product_id = f.product_id
left join {{ ref('stg_member_scd2') }} g
    on g.member_token = f.member_token
   and g.valid_from <= f.ordered_at and (g.valid_to is null or f.ordered_at < g.valid_to)
left join {{ ref('stg_product_prices_scd2') }} pr
    on pr.product_id = f.product_id
   and pr.valid_from <= f.ordered_at and (pr.valid_to is null or f.ordered_at < pr.valid_to)
