-- 봇 제외 규칙은 Silver 에서 버전(bot_rule_version)과 함께 판정한다. 전환율은 이 버전에 묶인다
select * from {{ source('silver', 'events_clean') }}
where not is_bot
