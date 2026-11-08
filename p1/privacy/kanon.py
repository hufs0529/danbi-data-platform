"""재식별 위험 간이 점검(k-익명성). 준식별자 조합으로 묶었을 때 k 명 미만인 그룹을 찾는다.

dim_member 의 (출생연대, 시군구, 등급) 조합이 2~3명뿐이면, 다른 정보(구매 상품·시각)와 결합해 특정인을 알아낼 수 있다.
결과는 월간 점검 리포트에 싣고, 위험 그룹이 많으면 일반화 수준을 올린다(시군구 → 시도, 출생연대 → 20년 단위).
"""

from __future__ import annotations

QUASI_IDENTIFIERS = ("birth_decade", "region_sigungu", "grade")


def kanon_sql(table: str = "gold.dim_member", k: int = 5, qi: tuple[str, ...] = QUASI_IDENTIFIERS) -> str:
    cols = ", ".join(qi)
    return (f"SELECT {cols}, count(*) AS n FROM {table} WHERE status = 'ACTIVE' "
            f"GROUP BY {cols} HAVING count(*) < {int(k)} ORDER BY n")  # fmt: skip


def summarize(groups: list[tuple], total: int, k: int = 5) -> dict:
    at_risk = sum(int(g[-1]) for g in groups)
    return {"k": k, "risky_groups": len(groups), "members_in_risky_groups": at_risk,
            "ratio": round(at_risk / total, 4) if total else 0.0,
            "recommendation": "일반화 상향(시군구→시도)" if total and at_risk / total > 0.01 else "유지"}  # fmt: skip
