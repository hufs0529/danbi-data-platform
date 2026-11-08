import csv
import datetime as dt
import io
from decimal import Decimal

import pytest

from p1.generator.settlement import (
    HEADER_V2,
    SettlementFaults,
    Txn,
    build_files,
    fee_parts,
    render,
    settle_date,
    window_utc,
)

KST = dt.timezone(dt.timedelta(hours=9))
D = dt.date(2026, 10, 29)


def _txn(i: int, hh: int, mm: int, kind: str = "CAPTURE", day: dt.date = D) -> Txn:
    at = dt.datetime.combine(day, dt.time(hh, mm), KST).astimezone(dt.UTC)
    return Txn(f"pg_{i:04d}", f"O{i:06d}", kind, "CARD", "신한", Decimal(10_000 + i * 100), at)


def test_cutoff_2330_kst_moves_to_next_day():
    assert settle_date(_txn(1, 23, 29).occurred_at) == D
    assert settle_date(_txn(2, 23, 30).occurred_at) == D + dt.timedelta(days=1)
    lo, hi = window_utc(D)
    assert lo.astimezone(KST) == dt.datetime(2026, 10, 28, 23, 30, tzinfo=KST)
    assert hi - lo == dt.timedelta(days=1)


def test_fee_truncates_and_cancel_is_negative():
    assert fee_parts(Decimal(10_050), "CARD") == (Decimal(201), Decimal(20))
    assert fee_parts(Decimal(-10_050), "CARD") == (Decimal(-201), Decimal(-20))


def test_render_is_cp949_with_trailer():
    body = render(D, [_txn(1, 10, 0), _txn(2, 11, 0, "CANCEL")])
    with pytest.raises(UnicodeDecodeError):
        body.decode("utf-8")
    rows = list(csv.reader(io.StringIO(body.decode("cp949"))))
    assert rows[0][2] == "거래ID"
    assert rows[-1][:2] == ["TRAILER", "2"]
    assert Decimal(rows[-1][2]) == Decimal(10_100) - Decimal(10_200)
    assert rows[2][4] == "취소" and rows[2][7].startswith("-")


def test_header_v2_splits_vat():
    rows = list(csv.reader(io.StringIO(render(D, [_txn(1, 10, 0)], header_version=2).decode("cp949"))))
    assert rows[0] == HEADER_V2
    amount, fee, vat, net = (Decimal(x) for x in rows[1][7:11])
    assert net == amount - fee - vat


def test_faults_produce_expected_file_sequence():
    internal = [_txn(i, 9 + i % 12, 0) for i in range(50)]
    files = build_files(D, internal, SettlementFaults(amount_skew_ratio=0.2, duplicate=True, correction=True))
    names = [n for n, _ in files]
    assert names == ["danbipay_settle_20261029_01.csv"] * 2 + ["danbipay_settle_20261029_02.csv"]
    assert files[0][1] == files[1][1]  # 중복 전송은 바이트까지 같다

    def amounts(body):
        return {r[2]: Decimal(r[7]) for r in csv.reader(io.StringIO(body.decode("cp949"))) if r[0].isdigit()}

    original = {t.payment_key: t.amount for t in internal}
    assert amounts(files[0][1]) != original
    assert amounts(files[2][1]) == original  # 정정본은 금액을 바로잡는다


def test_broken_encoding_is_not_cp949_readable_as_expected_header():
    (_, body), *_ = build_files(D, [_txn(1, 10, 0)], SettlementFaults(broken_encoding=True))
    try:
        header = body.decode("cp949").splitlines()[0]
    except UnicodeDecodeError:
        header = ""
    assert "거래ID" not in header


def test_extra_and_missing():
    internal = [_txn(i, 10, i % 60) for i in range(100)]
    (_, body), *_ = build_files(D, internal, SettlementFaults(missing_ratio=0.1, extra_count=3))
    keys = {r[2] for r in csv.reader(io.StringIO(body.decode("cp949"))) if r[0].isdigit()}
    assert len({k for k in keys if k.startswith("pg_extra")}) == 3
    assert len(keys - {k for k in keys if k.startswith("pg_extra")}) < 100
