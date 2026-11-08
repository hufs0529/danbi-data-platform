select * from {{ source('silver', 'member_addresses') }}
where not coalesce(is_deleted, false)
