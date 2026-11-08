{# Silver 의 timestamp 는 UTC(시간대 없음). 한국 영업일·정산은 KST 기준이다 #}
{% macro kst_date(col) -%}
  date(with_timezone({{ col }}, 'UTC') AT TIME ZONE 'Asia/Seoul')
{%- endmacro %}

{# PG 매입 마감 23:30 KST: 이후 거래는 다음 날 정산 파일에 들어간다(p1/generator/settlement.py 와 같은 규칙) #}
{% macro settle_date(col) -%}
  date((with_timezone({{ col }}, 'UTC') AT TIME ZONE 'Asia/Seoul') + INTERVAL '30' MINUTE)
{%- endmacro %}
