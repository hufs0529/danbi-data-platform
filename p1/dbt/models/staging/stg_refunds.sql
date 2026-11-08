select * from {{ source('silver', 'refunds') }}
where not coalesce(is_deleted, false)
