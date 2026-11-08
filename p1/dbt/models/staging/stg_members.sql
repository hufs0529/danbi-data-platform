select * from {{ source('silver', 'members') }}
where not coalesce(is_deleted, false)
