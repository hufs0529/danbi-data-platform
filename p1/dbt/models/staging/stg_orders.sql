select * from {{ source('silver', 'orders') }}
where not coalesce(is_deleted, false)
