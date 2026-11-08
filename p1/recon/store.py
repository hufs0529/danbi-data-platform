"""레이크 입출력 (pyiceberg + Glue 카탈로그). 대사 잡은 작아서 Spark 없이 컨테이너 하나로 돈다.

쓰기는 모두 '정산일 단위 덮어쓰기'라 같은 날짜를 다시 돌려도 결과가 같다(S-B2 재실행 동일).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pyarrow as pa


def catalog(region: str = "ap-northeast-2"):
    from pyiceberg.catalog import load_catalog

    return load_catalog("glue", **{"type": "glue", "glue.region": region, "s3.region": region})


def read(cat, table: str, row_filter: str = "true", columns: tuple[str, ...] = ("*",)) -> list[dict]:
    return cat.load_table(table).scan(row_filter=row_filter, selected_fields=columns).to_arrow().to_pylist()


def overwrite(cat, table: str, rows: list[dict], filter_expr: str) -> int:
    t = cat.load_table(table)
    schema = t.schema().as_arrow()
    data = pa.Table.from_pylist([_coerce(r, schema) for r in rows], schema=schema)
    t.overwrite(data, overwrite_filter=filter_expr)  # 필터 범위를 지우고 새 행을 쓰는 단일 커밋
    return len(rows)


def append(cat, table: str, rows: list[dict]) -> None:
    t = cat.load_table(table)
    schema = t.schema().as_arrow()
    t.append(pa.Table.from_pylist([_coerce(r, schema) for r in rows], schema=schema))


def _coerce(row: dict, schema: pa.Schema) -> dict:
    out = {}
    for f in schema:
        v = row.get(f.name)
        if v is not None and pa.types.is_decimal(f.type) and not isinstance(v, Decimal):
            v = Decimal(str(v))
        if v is not None and pa.types.is_timestamp(f.type) and isinstance(v, dt.datetime) and v.tzinfo:
            v = v.astimezone(dt.UTC).replace(tzinfo=None)
        out[f.name] = v
    return out
