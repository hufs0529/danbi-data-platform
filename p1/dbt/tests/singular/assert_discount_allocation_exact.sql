-- 아이템에 배분한 할인 합 = 주문 할인 (원 단위까지)
select f.order_id, sum(f.discount_amount) as allocated, max(o.discount_amount) as order_discount
from {{ ref('fct_order_items') }} f
join {{ ref('stg_orders') }} o on o.order_id = f.order_id
group by f.order_id
having sum(f.discount_amount) <> max(o.discount_amount)
