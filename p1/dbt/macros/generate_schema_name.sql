{# 기본 동작(<target_schema>_<custom>) 대신 지정한 스키마를 그대로 쓴다: gold_audit #}
{% macro generate_schema_name(custom_schema_name, node) -%}
  {%- if custom_schema_name is none -%}{{ target.schema }}{%- else -%}{{ custom_schema_name | trim }}{%- endif -%}
{%- endmacro %}
