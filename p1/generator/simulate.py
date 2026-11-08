"""단비마켓 상태 기계 시뮬레이터.

랜덤 행이 아니라 업무 흐름을 따른다:
    가입 → 탐색(클릭) → 장바구니 → 주문 → 결제 승인 → 매입 → (부분)취소·환불 → 배송
그 사이에 등급 변경(SCD2), 가격 변경(SCD2), 배송지 삭제, 탈퇴, CS 문의가 섞인다.

tick() 한 번이 가상 시간 1초다. 결과는 Action 목록이고 DB 에 쓰는 일은 하지 않는다.
"""

from __future__ import annotations

import datetime as dt
import random
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any

from .actions import Action, Click, Ddl, Op, Write
from .faults import Faults
from .ids import IdGen

KST = dt.timezone(dt.timedelta(hours=9))

SURNAMES = "김이박최정강조윤장임한오서신권황안송류홍"
GIVEN = ["단비", "서준", "하은", "도윤", "지우", "민준", "서연", "예준", "하윤", "주원", "지호", "수아"]
CITIES = [("서울특별시", "강남구", "테헤란로"), ("서울특별시", "마포구", "월드컵북로"),
          ("경기도", "성남시 분당구", "판교역로"), ("부산광역시", "해운대구", "센텀중앙로"),
          ("인천광역시", "연수구", "송도과학로"), ("대전광역시", "유성구", "대학로")]  # fmt: skip
CATEGORIES = ["패션", "뷰티", "식품", "가전", "생활", "도서", "스포츠"]
ISSUERS = ["신한", "국민", "삼성", "현대", "롯데", "하나", "우리", "BC"]
GRADES = ["BASIC", "SILVER", "GOLD", "VIP"]
CS_CATEGORIES = ["배송", "환불", "교환", "결제", "회원정보"]


@dataclass
class MemberState:
    member_id: int
    grade: str
    address_ids: list[int] = field(default_factory=list)
    withdrawn: bool = False


@dataclass
class PaymentState:
    payment_id: int
    payment_key: str
    order_id: int
    order_no: str
    member_id: int
    amount: Decimal
    method: str
    status: str
    item_amounts: list[
        tuple[int, Decimal]
    ]  # (order_item_id, 할인 전 금액). 부분취소 환불은 남은 결제액을 넘지 않는다
    canceled: Decimal = Decimal(0)
    version: int = 0
    capture_due: dt.datetime | None = None
    late_approval_due: dt.datetime | None = None


@dataclass
class World:
    members: dict[int, MemberState] = field(default_factory=dict)
    products: dict[int, Decimal] = field(default_factory=dict)  # product_id → 현재 판매가
    coupons: dict[int, tuple[str, Decimal]] = field(default_factory=dict)  # id → (type, value)
    payments: dict[int, PaymentState] = field(default_factory=dict)  # 진행 중(매입 전·취소 가능)


class Simulator:
    def __init__(
        self,
        seed: int = 42,
        start: dt.datetime | None = None,
        faults: Faults | None = None,
        orders_per_minute: float = 30.0,
        clicks_per_second: float = 20.0,
        worker: int = 0,
    ) -> None:
        self.rng = random.Random(seed)
        self.now = start or dt.datetime.now(dt.UTC).replace(microsecond=0)
        self.faults = faults or Faults()
        self.orders_per_minute = orders_per_minute
        self.clicks_per_second = clicks_per_second
        self.ids = IdGen(worker)
        self.world = World()
        self.ticks = 0
        self._uuid_rng = random.Random(seed + 1)

    # ------------------------------------------------------------------ 공통
    def _uuid(self) -> str:
        return str(uuid.UUID(int=self._uuid_rng.getrandbits(128), version=4))

    def _id(self) -> int:
        return self.ids.next(self.now)

    def _chance(self, p: float) -> bool:
        return self.rng.random() < p

    def _poisson(self, lam: float) -> int:
        # 작은 λ 에서 충분한 근사 (Knuth)
        n, p, limit = 0, 1.0, pow(2.718281828459045, -lam)
        while True:
            p *= self.rng.random()
            if p <= limit:
                return n
            n += 1

    def _phone(self) -> str:
        return f"010-{self.rng.randint(1000, 9999)}-{self.rng.randint(1000, 9999)}"

    def _name(self) -> str:
        return self.rng.choice(SURNAMES) + self.rng.choice(GIVEN)

    # ------------------------------------------------------------------ 초기 데이터
    def seed_catalog(self, n_products: int = 200, n_members: int = 500, n_coupons: int = 5) -> list[Action]:
        actions: list[Action] = []
        ops = []
        for _ in range(n_products):
            pid = self._id()
            price = Decimal(self.rng.randrange(5_000, 300_000, 100))
            self.world.products[pid] = price
            ops.append(Op("insert", "products", {"product_id": pid, "name": f"상품{pid % 100000}",
                          "category": self.rng.choice(CATEGORIES), "brand": f"브랜드{self.rng.randint(1, 30)}",
                          "status": "ON_SALE"}))  # fmt: skip
            ops.append(Op("insert", "product_prices", {"product_id": pid, "list_price": price,
                          "sale_price": price, "currency": "KRW"}))  # fmt: skip
        for _ in range(n_coupons):
            cid = self._id()
            kind = self.rng.choice(["AMOUNT", "RATE"])
            value = Decimal(self.rng.choice([2000, 3000, 5000])) if kind == "AMOUNT" else Decimal(10)
            self.world.coupons[cid] = (kind, value)
            ops.append(Op("insert", "coupons", {"coupon_id": cid, "code": f"DANBI{cid % 1000000}",
                          "discount_type": kind, "discount_value": value, "max_discount": Decimal(10000),
                          "total_quantity": 100000, "issued_quantity": 0, "valid_from": self.now,
                          "valid_to": self.now + dt.timedelta(days=30)}))  # fmt: skip
        actions.append(Write("commerce", tuple(ops), self.now))
        for _ in range(n_members):
            actions.extend(self._signup())
        return actions

    # ------------------------------------------------------------------ 업무 흐름
    def _signup(self) -> list[Action]:
        mid = self._id()
        aid = self._id()
        city, gu, road = self.rng.choice(CITIES)
        name = self._name()
        self.world.members[mid] = MemberState(mid, "BASIC", [aid])
        joined_kst = self.now.astimezone(KST).replace(tzinfo=None)  # 레거시 컬럼: KST 로컬 시각
        return [
            Write(
                "commerce",
                (
                    Op("insert", "members", {"member_id": mid, "email": f"user{mid}@example.com", "name": name,
                       "phone": self._phone(), "birth_date": dt.date(self.rng.randint(1960, 2006),
                       self.rng.randint(1, 12), self.rng.randint(1, 28)), "grade": "BASIC",
                       "marketing_agreed": int(self._chance(0.6)), "status": "ACTIVE",
                       "joined_at": joined_kst}),
                    Op("insert", "member_addresses", {"address_id": aid, "member_id": mid, "label": "집",
                       "recipient_name": name, "recipient_phone": self._phone(),
                       "zipcode": f"{self.rng.randint(1000, 63999):05d}",
                       "address1": f"{city} {gu} {road} {self.rng.randint(1, 500)}",
                       "address2": f"{self.rng.randint(101, 120)}동 {self.rng.randint(101, 2504)}호",
                       "is_default": 1}),
                ),
                self.now,
            )
        ]  # fmt: skip

    def _active_member(self) -> MemberState | None:
        active = [m for m in self.world.members.values() if not m.withdrawn and m.address_ids]
        return self.rng.choice(active) if active else None

    def _place_order(self) -> list[Action]:
        member = self._active_member()
        if member is None or not self.world.products:
            return []
        order_id = self._id()
        order_no = f"O{self.now.astimezone(KST):%Y%m%d}{order_id % 10_000_000:07d}"
        items, ops = [], []
        for pid in self.rng.sample(
            list(self.world.products), k=min(len(self.world.products), self.rng.randint(1, 4))
        ):
            qty = self.rng.choice([1, 1, 1, 2, 3])
            price = self.world.products[pid]
            iid = self._id()
            items.append((iid, price * qty))
            ops.append(Op("insert", "order_items", {"order_item_id": iid, "order_id": order_id, "product_id": pid,
                          "quantity": qty, "unit_price": price, "discount_amount": Decimal(0),
                          "status": "ORDERED"}))  # fmt: skip
        total = sum((a for _, a in items), Decimal(0))
        discount, coupon_id = Decimal(0), None
        if self.world.coupons and self._chance(0.3):
            coupon_id = self.rng.choice(list(self.world.coupons))
            kind, value = self.world.coupons[coupon_id]
            raw = value if kind == "AMOUNT" else (total * value / 100).quantize(Decimal(1), ROUND_DOWN)
            discount = min(raw, Decimal(10000), total)
            ops.append(Op("insert", "coupon_usages", {"usage_id": self._id(), "coupon_id": coupon_id,
                          "member_id": member.member_id, "order_id": order_id, "used_at": self.now}))  # fmt: skip
        ops.insert(0, Op("insert", "orders", {"order_id": order_id, "order_no": order_no,
                         "member_id": member.member_id, "address_id": member.address_ids[0], "status": "CREATED",
                         "total_amount": total, "discount_amount": discount, "coupon_id": coupon_id,
                         "currency": "KRW", "ordered_at": self.now}))  # fmt: skip
        actions: list[Action] = [Write("commerce", tuple(ops), self.now)]

        # 결제: 대부분 승인, 일부 실패
        pay_amount = total - discount
        method = self.rng.choices(["CARD", "EASY_PAY", "TRANSFER"], [0.7, 0.2, 0.1])[0]
        pid = self._id()
        pkey = f"pg_{self._uuid().replace('-', '')[:24]}"
        failed = self._chance(0.03)
        ps = PaymentState(pid, pkey, order_id, order_no, member.member_id, pay_amount, method,
                          "FAILED" if failed else "APPROVED", list(items))  # fmt: skip
        card_token = (
            f"ctk_{member.member_id % 7919:04d}{self.rng.randint(0, 2)}" if method == "CARD" else None
        )
        pay_ops = [Op("insert", "payments", {"payment_id": pid, "payment_key": pkey, "order_no": order_no,
                      "member_id": member.member_id, "method": method, "card_token": card_token,
                      "card_issuer": self.rng.choice(ISSUERS) if method == "CARD" else None,
                      "status": "READY", "amount": pay_amount, "canceled_amount": Decimal(0), "currency": "KRW",
                      "updated_at": self.now, "version": 0})]  # fmt: skip
        if failed:
            pay_ops += self._transition(ps, "READY", "FAILED", pay_amount, {"reason": "LIMIT_EXCEEDED"})
            actions.append(Write("payment", tuple(pay_ops), self.now))
            return actions

        ps.capture_due = self.now + dt.timedelta(minutes=self.rng.randint(1, 30))
        if self._chance(self.faults.out_of_order_ratio):
            # 상태 역전: 승인 웹훅이 늦게 온다. 지금은 READY 그대로, 매입이 먼저 기록된다
            ps.status = "READY"
            ps.late_approval_due = ps.capture_due + dt.timedelta(seconds=self.rng.randint(5, 120))
            actions.append(Write("payment", tuple(pay_ops), self.now))
        else:
            pay_ops += self._transition(
                ps, "READY", "APPROVED", pay_amount, {"approved_at": self.now.isoformat()}
            )
            pay_ops.append(Op("update", "payments", {"status": "APPROVED", "approved_at": self.now,
                              "updated_at": self.now, "version": ps.version}, {"payment_id": pid}))  # fmt: skip
            actions.append(Write("payment", tuple(pay_ops), self.now))
            actions.append(Write("commerce", (Op("update", "orders", {"status": "PAID"},
                                              {"order_id": order_id}),), self.now))  # fmt: skip
        self.world.payments[pid] = ps
        actions.extend(self._clicks_for_purchase(member.member_id, order_no))
        return actions

    def _transition(
        self, ps: PaymentState, frm: str | None, to: str, amount: Decimal, extra: dict
    ) -> list[Op]:
        """상태 이력 + outbox 이벤트. 같은 트랜잭션에 들어간다."""
        ps.version += 1
        event_type = {"APPROVED": "PaymentApproved", "CAPTURED": "PaymentCaptured", "FAILED": "PaymentFailed",
                      "PARTIAL_CANCELED": "PaymentPartiallyCanceled", "CANCELED": "PaymentCanceled"}[to]  # fmt: skip
        payload_amount: Any = str(amount)
        if self._chance(self.faults.null_amount_ratio):
            payload_amount = None
        payload = {"payment_key": ps.payment_key, "order_no": ps.order_no, "member_id": ps.member_id,
                   "method": ps.method, "from_status": frm, "to_status": to, "amount": payload_amount,
                   "total_amount": str(ps.amount), "canceled_amount": str(ps.canceled), "version": ps.version,
                   "occurred_at": self.now.isoformat(), **extra}  # fmt: skip
        ops = [
            Op("insert", "payment_status_history", {"payment_id": ps.payment_id, "from_status": frm,
               "to_status": to, "amount": amount, "occurred_at": self.now}),
            Op("insert", "outbox_events", {"id": self._uuid(), "aggregatetype": "payment",
               "aggregateid": ps.payment_key, "type": event_type, "payload": payload, "occurred_at": self.now}),
        ]  # fmt: skip
        if self._chance(self.faults.dup_ratio):
            # 서비스 재시도로 같은 이벤트가 다른 outbox id 로 두 번 들어간다 → 소비자는 (payment_key, version) 로 중복 제거
            ops.append(Op("insert", "outbox_events", {**ops[1].values, "id": self._uuid()}))
        return ops

    def _progress_payments(self) -> list[Action]:
        actions: list[Action] = []
        for ps in list(self.world.payments.values()):
            if ps.capture_due and self.now >= ps.capture_due and ps.status in ("APPROVED", "READY"):
                frm = ps.status
                ps.status = "CAPTURED"
                ops = self._transition(ps, frm, "CAPTURED", ps.amount, {"captured_at": self.now.isoformat()})
                ops.append(Op("update", "payments", {"status": "CAPTURED", "captured_at": self.now,
                              "updated_at": self.now, "version": ps.version}, {"payment_id": ps.payment_id}))  # fmt: skip
                actions.append(Write("payment", tuple(ops), self.now))
                ps.capture_due = None
            elif ps.late_approval_due and self.now >= ps.late_approval_due:
                # 늦게 온 승인: 상태는 되돌리지 않고(CAPTURED 유지) 승인 시각만 채운다
                approved_at = ps.late_approval_due - dt.timedelta(minutes=10)
                ops = self._transition(ps, "READY", "APPROVED", ps.amount, {"approved_at": approved_at.isoformat(),
                                       "late_arrival": True})  # fmt: skip
                ops.append(Op("update", "payments", {"approved_at": approved_at, "updated_at": self.now,
                              "version": ps.version}, {"payment_id": ps.payment_id}))  # fmt: skip
                actions.append(Write("payment", tuple(ops), self.now))
                actions.append(Write("commerce", (Op("update", "orders", {"status": "PAID"},
                                                  {"order_id": ps.order_id}),), self.now))  # fmt: skip
                ps.late_approval_due = None
            elif ps.status == "CAPTURED" and self._chance(0.0005):
                actions.extend(self._cancel(ps))
        return actions

    def _cancel(self, ps: PaymentState) -> list[Action]:
        remaining = [(iid, amt) for iid, amt in ps.item_amounts]
        if len(remaining) > 1 and self._chance(0.6):
            iid, amt = remaining.pop(self.rng.randrange(len(remaining)))
            ps.item_amounts = remaining
            to, order_status = "PARTIAL_CANCELED", "PARTIAL_CANCELED"
            item_ops = (Op("update", "order_items", {"status": "CANCELED"}, {"order_item_id": iid}),)
        else:
            amt = ps.amount - ps.canceled
            to, order_status = "CANCELED", "CANCELED"
            item_ops = tuple(Op("update", "order_items", {"status": "CANCELED"}, {"order_item_id": i})
                             for i, _ in ps.item_amounts)  # fmt: skip
            del self.world.payments[ps.payment_id]
        amt = max(Decimal(0), min(amt, ps.amount - ps.canceled))
        frm = ps.status
        ps.canceled += amt
        ps.status = to
        refund_id = self._id()
        ops = self._transition(ps, frm, to, amt, {"refund_id": refund_id})
        ops += [
            Op("update", "payments", {"status": to, "canceled_amount": ps.canceled, "updated_at": self.now,
               "version": ps.version}, {"payment_id": ps.payment_id}),
            Op("insert", "refunds", {"refund_id": refund_id, "payment_id": ps.payment_id, "amount": amt,
               "reason": self.rng.choice(["단순변심", "상품불량", "배송지연"]), "status": "COMPLETED",
               "requested_at": self.now, "completed_at": self.now}),
        ]  # fmt: skip
        return [
            Write("payment", tuple(ops), self.now),
            Write("commerce", (Op("update", "orders", {"status": order_status}, {"order_id": ps.order_id}),
                               *item_ops), self.now),
        ]  # fmt: skip

    def _member_changes(self) -> list[Action]:
        out: list[Action] = []
        m = self._active_member()
        if m is None:
            return out
        r = self.rng.random()
        if r < 0.3:  # 등급 변경(SCD2)
            new = self.rng.choice([g for g in GRADES if g != m.grade])
            m.grade = new
            out.append(Write("commerce", (Op("update", "members", {"grade": new}, {"member_id": m.member_id}),),
                             self.now))  # fmt: skip
        elif r < 0.45:  # 배송지 추가
            aid = self._id()
            city, gu, road = self.rng.choice(CITIES)
            m.address_ids.append(aid)
            out.append(Write("commerce", (Op("insert", "member_addresses", {"address_id": aid,
                       "member_id": m.member_id, "label": "회사", "recipient_name": self._name(),
                       "recipient_phone": self._phone(), "zipcode": f"{self.rng.randint(1000, 63999):05d}",
                       "address1": f"{city} {gu} {road} {self.rng.randint(1, 500)}", "address2": None,
                       "is_default": 0}),), self.now))  # fmt: skip
        elif r < 0.55 and len(m.address_ids) > 1 and self._chance(max(self.faults.hard_delete_ratio, 0.2)):
            aid = m.address_ids.pop()
            out.append(
                Write("commerce", (Op("delete", "member_addresses", key={"address_id": aid}),), self.now)
            )
        elif r < 0.58:  # 탈퇴
            m.withdrawn = True
            out.append(Write("commerce", (Op("update", "members", {"status": "WITHDRAWN",
                       "withdrawn_at": self.now.astimezone(KST).replace(tzinfo=None)},
                       {"member_id": m.member_id}),), self.now))  # fmt: skip
        else:  # CS 문의
            text = f"{self.rng.choice(CS_CATEGORIES)} 문의드립니다. 빠른 확인 부탁드려요."
            if self._chance(self.faults.pii_in_text_ratio):
                text += f" 연락처 {self._phone()} 로 전화 주세요."
            out.append(Write("commerce", (Op("insert", "cs_tickets", {"ticket_id": self._id(),
                       "member_id": m.member_id, "order_id": None, "category": self.rng.choice(CS_CATEGORIES),
                       "status": "OPEN", "content": text, "satisfaction": None}),), self.now))  # fmt: skip
        return out

    def _price_change(self) -> list[Action]:
        pid = self.rng.choice(list(self.world.products))
        old = self.world.products[pid]
        new = (old * Decimal(self.rng.choice(["0.9", "0.8", "1.1", "0.95"]))).quantize(
            Decimal(100), ROUND_DOWN
        )
        self.world.products[pid] = new
        return [Write("commerce", (Op("update", "product_prices", {"sale_price": new}, {"product_id": pid}),),
                      self.now)]  # fmt: skip

    # ------------------------------------------------------------------ 클릭
    def _click(self, event_type: str, session: str, member_id: int | None, **kw: Any) -> Click:
        bot = kw.pop("bot", False)
        event_time = self.now - dt.timedelta(milliseconds=self.rng.randint(0, 900))
        deliver = self.now
        if self._chance(self.faults.late_ratio):
            deliver = self.now + dt.timedelta(minutes=self.rng.randint(5, self.faults.late_max_minutes))
        event = {"event_id": self._uuid(), "event_type": event_type, "session_id": session,
                 "member_id": member_id, "device_id": f"dev-{session[:8]}",
                 "platform": self.rng.choice(["web", "ios", "android"]),
                 "page": kw.get("page"), "product_id": kw.get("product_id"), "search_query": kw.get("search_query"),
                 "order_no": kw.get("order_no"),
                 "user_agent": "Mozilla/5.0 (compatible; DanbiCrawler/1.0)" if bot else "Mozilla/5.0 DanbiApp/5.2",
                 "event_time": event_time, "sent_time": deliver}  # fmt: skip
        return Click(event, deliver)

    def _browse_session(self) -> list[Action]:
        session = self._uuid()
        bot = self._chance(self.faults.bot_ratio)
        member = None if self._chance(0.4) else self._active_member()
        mid = member.member_id if member else None
        out: list[Action] = [self._click("page_view", session, mid, page="/", bot=bot)]
        for _ in range(self.rng.randint(1, 12 if not bot else 60)):
            pid = self.rng.choice(list(self.world.products)) if self.world.products else None
            if self._chance(0.2):
                q = self.rng.choice(CATEGORIES)
                if self._chance(self.faults.pii_in_text_ratio * 0.1):
                    q = self._phone()  # 검색창에 전화번호를 넣는 사용자
                out.append(self._click("search", session, mid, page="/search", search_query=q, bot=bot))
            out.append(self._click("page_view", session, mid, page=f"/p/{pid}", product_id=pid, bot=bot))
            if self._chance(0.1):
                out.append(self._click("add_to_cart", session, mid, product_id=pid, bot=bot))
        dups = [Click(dict(c.event), c.deliver_at) for c in out if isinstance(c, Click)
                and self._chance(self.faults.dup_ratio)]  # fmt: skip
        return out + dups

    def _clicks_for_purchase(self, member_id: int, order_no: str) -> list[Action]:
        session = self._uuid()
        return [
            self._click("page_view", session, member_id, page="/cart"),
            self._click("checkout_start", session, member_id, page="/checkout"),
            self._click("purchase", session, member_id, page="/complete", order_no=order_no),
        ]

    # ------------------------------------------------------------------ 진행
    def tick(self) -> list[Action]:
        """가상 1초."""
        self.ticks += 1
        actions: list[Action] = []
        if self.faults.schema_change_at == self.ticks:
            actions.append(Ddl("commerce", "ALTER TABLE orders ADD COLUMN gift_message VARCHAR(200) NULL",
                               "S-C1 호환 변경: nullable 컬럼 추가"))  # fmt: skip
        if self.faults.breaking_change_at == self.ticks:
            actions.append(Ddl("commerce", "ALTER TABLE orders MODIFY COLUMN discount_amount VARCHAR(20)",
                               "S-C1 비호환 변경: 금액 컬럼 타입을 문자열로"))  # fmt: skip
        for _ in range(self._poisson(self.orders_per_minute / 60)):
            actions.extend(self._place_order())
        for _ in range(self._poisson(self.clicks_per_second / 15)):  # 세션당 평균 15 클릭
            actions.extend(self._browse_session())
        if self._chance(0.05):
            actions.extend(self._signup())
        if self._chance(0.08):
            actions.extend(self._member_changes())
        if self.world.products and self._chance(0.01):
            actions.extend(self._price_change())
        actions.extend(self._progress_payments())
        self.now += dt.timedelta(seconds=1)
        return actions

    def run(self, seconds: int) -> list[Action]:
        out: list[Action] = []
        for _ in range(seconds):
            out.extend(self.tick())
        return out
