select * from {{ source('silver', 'payments') }}
where not coalesce(is_deleted, false)
