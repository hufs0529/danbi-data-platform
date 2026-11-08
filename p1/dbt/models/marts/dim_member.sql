-- 현재 회원 차원. 분석용 속성만 담는다(암호문 컬럼은 제외 → 분석가에게 복호화 대상 자체가 없다)
with addr as (
    select member_token, region_sido, region_sigungu,
           row_number() over (partition by member_token order by is_default desc, updated_at desc) as rn
    from {{ ref('stg_member_addresses') }}
)
select
    m.member_token,
    m.grade,
    m.status,
    m.marketing_agreed,
    cast(floor(m.birth_year / 10) * 10 as integer) as birth_decade,
    {{ kst_date('m.joined_at') }} as joined_date_kst,
    a.region_sido,
    a.region_sigungu
from {{ ref('stg_members') }} m
left join addr a on a.member_token = m.member_token and a.rn = 1
