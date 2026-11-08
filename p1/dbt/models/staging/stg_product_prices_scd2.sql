select * from {{ source('silver', 'product_prices_scd2') }}
