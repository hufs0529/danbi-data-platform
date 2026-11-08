-- 취소 누적이 매입을 넘는 결제는 원천 데이터 오류 또는 중복 처리 신호다
select payment_key, captured_amount, canceled_amount
from {{ ref('fct_payments') }}
where captured_amount > 0 and canceled_amount > captured_amount
