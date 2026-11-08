"""pyiceberg 로컬 카탈로그(SQLite + 파일)로 '정산일 단위 덮어쓰기'가 멱등인지 확인한다."""

import datetime as dt
from decimal import Decimal

import pytest

pytest.importorskip("sqlalchemy")

from pyiceberg.catalog.sql import SqlCatalog  # noqa: E402
from pyiceberg.schema import Schema  # noqa: E402
from pyiceberg.types import DateType, DecimalType, NestedField, StringType  # noqa: E402

from p1.recon import store  # noqa: E402


@pytest.fixture
def cat(tmp_path):
    c = SqlCatalog("t", uri=f"sqlite:///{tmp_path}/c.db", warehouse=f"file://{tmp_path}/wh")
    c.create_namespace("silver")
    c.create_table("silver.pg_settlement", Schema(
        NestedField(1, "settle_date", DateType()), NestedField(2, "payment_key", StringType()),
        NestedField(3, "amount", DecimalType(18, 0))))  # fmt: skip
    return c


def _rows(d, n, amount=100):
    return [{"settle_date": d, "payment_key": f"k{i}", "amount": amount} for i in range(n)]


def test_overwrite_by_date_is_idempotent_and_scoped(cat):
    d1, d2 = dt.date(2026, 10, 28), dt.date(2026, 10, 29)
    store.overwrite(cat, "silver.pg_settlement", _rows(d1, 3), "settle_date == '2026-10-28'")
    store.overwrite(cat, "silver.pg_settlement", _rows(d2, 5), "settle_date == '2026-10-29'")
    store.overwrite(cat, "silver.pg_settlement", _rows(d2, 5), "settle_date == '2026-10-29'")  # 재실행
    rows = store.read(cat, "silver.pg_settlement")
    assert len(rows) == 8
    store.overwrite(
        cat, "silver.pg_settlement", _rows(d2, 4, amount=200), "settle_date == '2026-10-29'"
    )  # 정정본
    by_date = {}
    for r in store.read(cat, "silver.pg_settlement"):
        by_date.setdefault(r["settle_date"], []).append(r["amount"])
    assert len(by_date[d1]) == 3 and by_date[d2] == [Decimal(200)] * 4
