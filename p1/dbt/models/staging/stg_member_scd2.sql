select * from {{ source('silver', 'dim_member_scd2') }}
