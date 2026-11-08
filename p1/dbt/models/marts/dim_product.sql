select
    p.product_id,
    p.name,
    p.category,
    p.brand,
    p.status,
    pr.list_price,
    pr.sale_price
from {{ ref('stg_products') }} p
left join {{ ref('stg_product_prices_scd2') }} pr on pr.product_id = p.product_id and pr.is_current
