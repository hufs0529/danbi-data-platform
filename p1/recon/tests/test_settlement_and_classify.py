import datetime as dt
from decimal import Decimal

from p1.generator.settlement import SettlementFaults, Txn, build_files
from p1.recon import classify as C
from p1.recon import settlement as S
from p1.recon.source_check import window

KST = dt.timezone(dt.timedelta(hours=9))
D = dt.date(2026, 10, 29)


def _txns(n=30):
    out = []
    for i in range(n):
        at = dt.datetime.combine(D, dt.time(9 + i % 12, i % 60), KST).astimezone(dt.UTC)
        kind = "CANCEL" if i % 7 == 0 else "CAPTURE"
        out.append(Txn(f"pg_{i:04d}", f"O{i:06d}", kind, ["CARD", "EASY_PAY", "TRANSFER"][i % 3], "신한",
                       Decimal(10_000 + 100 * i), at))  # fmt: skip
    return out


# --- 생성기 결함 → 적재기 판정 (같은 저장소의 두 컴포넌트를 서로 검증) ------------------------
def test_clean_file_accepted_with_trailer_and_utc():
    (name, body), *_ = build_files(D, _txns())
    v = S.inspect(name, body, set())
    assert v.status == "ACCEPTED" and v.header_version == 1 and v.seq == 1 and len(v.lines) == 30
    assert v.lines[0].txn_at.tzinfo is None and v.lines[0].txn_at.hour == 0  # 09:00 KST → 00:00 UTC
    cancel = next(ln for ln in v.lines if ln.kind == "CANCEL")
    assert cancel.amount < 0 and cancel.vat is None  # v1 은 부가세 분리 불가


def test_duplicate_by_content_hash():
    files = build_files(D, _txns(), SettlementFaults(duplicate=True))
    first = S.inspect(files[0][0], files[0][1], set())
    second = S.inspect(files[1][0], files[1][1], {first.sha256})
    assert second.status == "DUPLICATE"


def test_correction_has_higher_seq_and_becomes_effective():
    files = build_files(D, _txns(), SettlementFaults(amount_skew_ratio=0.3, correction=True))
    v1, v2 = (S.inspect(n, b, set()) for n, b in files)
    assert (v1.seq, v2.seq) == (1, 2) and v1.sha256 != v2.sha256
    assert S.effective_seq({1: v1.sha256, 2: v2.sha256}) == 2


def test_broken_encoding_quarantined():
    (name, body), *_ = build_files(D, _txns(), SettlementFaults(broken_encoding=True))
    v = S.inspect(name, body, set())
    assert v.status == "QUARANTINED" and v.reason.split(":")[0] in ("encoding", "header")


def test_header_v2_accepted_and_normalized():
    (name, body), *_ = build_files(D, _txns(), SettlementFaults(header_version=2))
    v = S.inspect(name, body, set())
    assert v.status == "ACCEPTED" and v.header_version == 2
    assert all(ln.amount - ln.fee - ln.vat == ln.net for ln in v.lines)


def test_truncated_file_quarantined_by_trailer():
    (name, body), *_ = build_files(D, _txns())
    lines = body.split(b"\r\n")
    cut = b"\r\n".join(lines[:5] + lines[-2:])  # 중간이 잘렸지만 TRAILER 는 남은 파일
    v = S.inspect(name, cut, set())
    assert v.status == "QUARANTINED" and v.reason.startswith("trailer")


def test_bad_name_quarantined():
    assert S.inspect("settle.csv", b"x", set()).reason.startswith("name")


# --- 4분류 -------------------------------------------------------------------------------
def _i(key, kind="CAPTURE", amount=10000):
    return {"payment_key": key, "kind": kind, "amount": Decimal(amount)}


def _p(key, kind="CAPTURE", amount=10000, method="CARD", fee=None):
    signed = Decimal(amount) * (-1 if kind == "CANCEL" else 1)
    return {"payment_key": key, "kind": kind, "amount": signed, "method": method,
            "fee": C.expected_fee_incl_vat(signed, method) if fee is None else Decimal(fee), "vat": None}  # fmt: skip


def test_four_way_classification_and_causes():
    internal = [
        _i("a"),
        _i("b", amount=10000),
        _i("c"),
        _i("d"),
        _i("e", "CANCEL", 3000),
        _i("e", "CANCEL", 2000),
    ]
    pg = [_p("a"), _p("b", amount=9000), _p("e", "CANCEL", 5000), _p("x"), _p("y"), _p("z")]
    res = {(r.payment_key, r.kind): r for r in C.classify(
        D, internal, pg, pg_next_day_keys={("c", "CAPTURE")}, internal_prev_day_keys={("x", "CAPTURE")},
        known_payment_keys={"y"})}  # fmt: skip
    assert res[("a", "CAPTURE")].classification == "MATCHED"
    assert (res[("b", "CAPTURE")].classification, res[("b", "CAPTURE")].diff) == (
        "AMOUNT_MISMATCH",
        Decimal(-1000),
    )
    assert res[("c", "CAPTURE")].cause_code == "TIMING_NEXT_DAY" and res[("c", "CAPTURE")].explained
    assert res[("d", "CAPTURE")].cause_code == "PG_MISSING" and not res[("d", "CAPTURE")].explained
    assert res[("e", "CANCEL")].classification == "MATCHED"  # 같은 날 부분취소 두 번 = PG 한 줄
    assert res[("x", "CAPTURE")].cause_code == "TIMING_PREV_DAY"
    assert res[("y", "CAPTURE")].cause_code == "INTERNAL_STATUS_LAG"
    assert res[("z", "CAPTURE")].cause_code == "UNKNOWN_TXN"
    s = C.summary(list(res.values()))
    assert s["unexplained_count"] == 4 and s["pending"] == 0


def test_pending_until_next_day_file_arrives():
    [r] = C.classify(
        D, [_i("c")], [], pg_next_day_keys=None, internal_prev_day_keys=set(), known_payment_keys=set()
    )
    assert r.cause_code == "PG_PENDING"
    assert C.summary([r])["unexplained_count"] == 0 and C.summary([r])["pending"] == 1


def test_fee_mismatch_flag_independent_of_amount():
    [r] = C.classify(D, [_i("a")], [_p("a", fee=500)], set(), set(), set())
    assert r.classification == "MATCHED" and r.fee_mismatch


def test_report_lists_only_exceptions():
    res = C.classify(D, [_i("a"), _i("d")], [_p("a")], set(), set(), set())
    md = C.render_report(D, res)
    assert "| d | CAPTURE |" in md and "| a | CAPTURE |" not in md and "설명되지 않은 차이: 1건" in md


def test_source_window_has_lag_and_aligns_to_hour():
    w0, w1 = window(dt.datetime(2026, 10, 30, 10, 17))
    assert (w0, w1) == (dt.datetime(2026, 10, 30, 8, 0), dt.datetime(2026, 10, 30, 9, 0))
