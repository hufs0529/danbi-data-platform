select * from {{ source('silver', 'order_items') }}
where not coalesce(is_deleted, false)
