"""일반화: 원문 대신 분석에 필요한 만큼만 남긴다(가명처리의 한 방법).

birth_date → 출생연도, zipcode → 앞 3자리(시군구 수준), 주소 → 시도·시군구.
결과가 너무 구체적이면(작은 동네 + 출생연도 + 성별 조합) 재식별 위험이 생긴다. k-익명성 점검은 P1-13.
"""

from __future__ import annotations

import datetime as dt
import re

SIDO_ALIASES = {
    "서울": "서울특별시", "부산": "부산광역시", "대구": "대구광역시", "인천": "인천광역시", "광주": "광주광역시",
    "대전": "대전광역시", "울산": "울산광역시", "세종": "세종특별자치시", "경기": "경기도", "강원": "강원특별자치도",
    "충북": "충청북도", "충남": "충청남도", "전북": "전북특별자치도", "전남": "전라남도", "경북": "경상북도",
    "경남": "경상남도", "제주": "제주특별자치도",
}  # fmt: skip
_FULL = set(SIDO_ALIASES.values()) | {"강원도", "전라북도"}
_EPOCH = dt.date(1970, 1, 1)


def birth_year(value) -> int | None:
    """Debezium DATE(에포크 이후 일수) 또는 'YYYY-MM-DD' 를 받는다."""
    if value is None:
        return None
    if isinstance(value, int):
        return (_EPOCH + dt.timedelta(days=value)).year
    if isinstance(value, dt.date):
        return value.year
    m = re.match(r"^(\d{4})", str(value))
    return int(m.group(1)) if m else None


def zip3(value) -> str | None:
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    return digits[:3] if len(digits) == 5 else None


def _tokens(address: str) -> list[str]:
    return address.split()


def sido(value) -> str | None:
    if not value:
        return None
    first = _tokens(str(value))[0]
    if first in _FULL:
        return {"강원도": "강원특별자치도", "전라북도": "전북특별자치도"}.get(first, first)
    for short, full in SIDO_ALIASES.items():
        if first.startswith(short):
            return full
    return None


def sigungu(value) -> str | None:
    """'경기도 성남시 분당구 ...' → '성남시 분당구', '서울특별시 강남구 ...' → '강남구'."""
    if not value:
        return None
    toks = _tokens(str(value))
    if len(toks) < 2 or sido(value) is None:
        return None
    second = toks[1]
    if second.endswith("시") and len(toks) > 2 and toks[2].endswith("구"):
        return f"{second} {toks[2]}"
    return second if re.search(r"(시|군|구)$", second) else None


GENERALIZERS = {"birth_year": birth_year, "zip3": zip3, "sido": sido, "sigungu": sigungu}
