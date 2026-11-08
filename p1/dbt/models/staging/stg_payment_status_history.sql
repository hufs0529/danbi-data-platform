select * from {{ source('silver', 'payment_status_history') }}
where not coalesce(is_deleted, false)
