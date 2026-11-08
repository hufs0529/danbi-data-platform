select * from {{ source('silver', 'products') }}
where not coalesce(is_deleted, false)
