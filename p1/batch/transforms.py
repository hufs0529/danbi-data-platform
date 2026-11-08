"""Bronze → Silver 변환(순수 DataFrame 함수). Iceberg MERGE 문은 merge_sql/scd2 에서 만든다.

규칙
- 최신 판정은 도착 순서가 아니라 change_seq(소스 커밋 순서). 같은 키의 여러 변경 중 마지막 하나만 MERGE 한다.
- MERGE 는 `s._src_seq > t._src_seq` 일 때만 덮어쓴다 → 같은 구간을 두 번 처리해도(겹침 창, 재시도, 재처리) 결과가 같다.
- 삭제(op=d)는 행을 지우지 않고 is_deleted 로 남긴다. 개인정보 파기는 별도 경로(P1-13)다.
"""

from __future__ import annotations

from pyspark.sql import Column as SCol
from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as F
from pyspark.sql.types import MapType, StringType

from contracts.registry import Contract

BOT_RULE_VERSION = "v1"
BOT_UA = r"(?i)(bot|crawler|spider|curl|python-requests|headless)"


def _field(m: SCol, name: str) -> SCol:
    return m.getItem(name)


def cast_expr(raw: SCol, typ: str, transform: str | None) -> SCol:
    """Bronze JSON 문자열 값 → 계약 타입."""
    op = (transform or "").split(":")[0]
    if op == "ts_iso":
        return F.to_timestamp(raw)
    if op == "ts_kst_local_ms":
        # 레거시 DATETIME 은 KST 벽시계 값을 UTC 인 것처럼 담는다 → 9시간 빼서 진짜 UTC 로
        return F.timestamp_millis(raw.cast("bigint")) - F.expr("INTERVAL 9 HOURS")
    if op == "date_days":
        return F.date_add(F.lit("1970-01-01").cast("date"), raw.cast("int"))
    if typ == "boolean":
        return F.when(raw.isin("true", "1"), F.lit(True)).when(raw.isin("false", "0"), F.lit(False))
    return raw.cast(typ)


def derive_expr(expr: str) -> SCol:
    if expr.startswith("kst_date("):
        col = expr[len("kst_date(") : -1]
        return F.to_date(F.from_utc_timestamp(F.col(col), "Asia/Seoul"))
    if expr == "is_bot(user_agent)":
        return F.col("user_agent").rlike(BOT_UA)
    if expr == "bot_rule_version()":
        return F.lit(BOT_RULE_VERSION)
    if expr.startswith("seconds_between("):
        a, b = (x.strip() for x in expr[len("seconds_between(") : -1].split(","))
        return (F.unix_timestamp(F.col(b)) - F.unix_timestamp(F.col(a))).cast("bigint")
    raise ValueError(f"알 수 없는 derive: {expr}")


def cdc_to_rows(bronze: DataFrame, c: Contract, extra: tuple[str, ...] = ()) -> DataFrame:
    """bronze.cdc_events(한 테이블) → 계약 컬럼 + 메타(op, _src_seq, _src_ts, txn_id).

    extra: 계약 밖(드리프트) 필드. 문자열 그대로 같은 이름의 컬럼으로 싣는다(분류 전까지 분석가에게 안 보임).
    """
    m = F.from_json(F.when(F.col("op") == "d", F.col("before_json")).otherwise(F.col("after_json")),
                    MapType(StringType(), StringType()))  # fmt: skip
    df = bronze.select(F.col("op"), F.col("change_seq").alias("_src_seq"), F.col("source_ts").alias("_src_ts"),
                       F.col("txn_id"), m.alias("_m"))  # fmt: skip
    cols = [F.col("op"), F.col("_src_seq"), F.col("_src_ts"), F.col("txn_id")]
    for col in c.columns:
        if col.source is not None:
            cols.append(
                cast_expr(_field(F.col("_m"), col.bronze_field), col.type, col.transform).alias(col.name)
            )
    cols += [_field(F.col("_m"), f).alias(f) for f in extra]
    df = df.select(*cols)
    for col in c.columns:
        if col.derive:
            df = df.withColumn(col.name, derive_expr(col.derive).cast(col.type))
    return df


def unknown_fields(bronze: DataFrame, c: Contract) -> list[str]:
    """계약 밖 필드(S-C1 스키마 드리프트). 수집 시점 파생 필드(__)는 제외."""
    known = {col.source for col in c.columns if col.source} | {
        col.bronze_field for col in c.columns if col.source
    }
    keys = (
        bronze.select(
            F.explode(F.map_keys(F.from_json("after_json", MapType(StringType(), StringType())))).alias("k")
        )
        .distinct()
        .collect()
    )
    return sorted(r.k for r in keys if r.k not in known and "__" not in r.k)


def latest_per_key(df: DataFrame, keys: list[str]) -> DataFrame:
    w = Window.partitionBy(*keys).orderBy(F.col("_src_seq").desc())
    return df.withColumn("_rn", F.row_number().over(w)).where("_rn = 1").drop("_rn")


def merge_sql(
    c: Contract, source_view: str, run_id: str, catalog: str = "glue", extra: tuple[str, ...] = ()
) -> str:
    keys = list(c.grain)
    data_cols = [col.name for col in c.columns] + list(extra)
    on = " AND ".join(f"t.{k} = s.{k}" for k in keys)
    set_cols = ", ".join(f"t.{n} = s.{n}" for n in data_cols if n not in keys)
    meta_set = (f"t._src_seq = s._src_seq, t._src_ts = s._src_ts, t._ingested_at = current_timestamp(), "
                f"t._run_id = '{run_id}'")  # fmt: skip
    ins_cols = data_cols + ["is_deleted", "deleted_at", "_src_seq", "_src_ts", "_ingested_at", "_run_id"]
    ins_vals = [f"s.{n}" for n in data_cols] + [
        "s.op = 'd'", "CASE WHEN s.op = 'd' THEN s._src_ts END", "s._src_seq", "s._src_ts", "current_timestamp()",
        f"'{run_id}'",
    ]  # fmt: skip
    return f"""
MERGE INTO {catalog}.{c.dataset} t
USING {source_view} s
ON {on}
WHEN MATCHED AND s._src_seq > t._src_seq AND s.op = 'd' THEN
  UPDATE SET t.is_deleted = true, t.deleted_at = s._src_ts, {meta_set}
WHEN MATCHED AND s._src_seq > t._src_seq THEN
  UPDATE SET {set_cols}, t.is_deleted = false, t.deleted_at = NULL, {meta_set}
WHEN NOT MATCHED THEN
  INSERT ({", ".join(ins_cols)}) VALUES ({", ".join(ins_vals)})
""".strip()


# ---------------------------------------------------------------------------
# SCD Type 2
# ---------------------------------------------------------------------------
def scd2_versions(changes: DataFrame, current: DataFrame, c: Contract) -> DataFrame:
    """변경 증분 + 현재 행 → 새 버전 목록(valid_from·valid_to 포함).

    changes: cdc_to_rows 결과(op, _src_seq, _src_ts, txn_id, 계약 컬럼)
    current: silver 의 is_current 행(키, 추적 컬럼, valid_from, _src_seq)
    - 같은 트랜잭션 안의 여러 변경은 마지막 것만 남긴다(유효 구간 0인 버전 방지)
    - 추적 컬럼이 직전 버전과 같으면 버전을 만들지 않는다(다른 컬럼만 바뀐 변경)
    - 이미 반영된 변경(_src_seq ≤ 현재 행)은 버린다 → 재실행 멱등
    """
    keys = list(c.grain[:-1]) if c.grain[-1] == "valid_from" else list(c.grain)
    tracked = list(c.scd2_tracked)
    cols = keys + tracked
    w_txn = Window.partitionBy(*keys, "txn_id").orderBy(F.col("_src_seq").desc())
    ch = (changes.withColumn("_rn", F.row_number().over(w_txn)).where("_rn = 1").drop("_rn")
          .select(*cols, "op", "_src_seq", F.col("_src_ts").alias("valid_from"), F.lit(False).alias("_existing")))  # fmt: skip
    cur = current.select(
        *cols, F.lit("c").alias("op"), "_src_seq", "valid_from", F.lit(True).alias("_existing")
    )
    cur_seq = current.select(*keys, F.col("_src_seq").alias("_cur_seq"))
    ch = ch.join(cur_seq, keys, "left").where(
        F.col("_cur_seq").isNull() | (F.col("_src_seq") > F.col("_cur_seq"))
    )
    ch = ch.drop("_cur_seq")
    allv = cur.unionByName(ch)
    w = Window.partitionBy(*keys).orderBy("_src_seq")
    prev = [F.lag(t).over(w) for t in tracked]
    same_as_prev = F.lit(True)
    for t, p in zip(tracked, prev, strict=True):
        same_as_prev = same_as_prev & F.coalesce(F.col(t) == p, F.col(t).isNull() & p.isNull())
    first = F.lag("_src_seq").over(w).isNull()
    allv = allv.withColumn("_changed", first | ~same_as_prev | (F.col("op") == "d"))
    allv = allv.where(F.col("_changed") | F.col("_existing")).drop("_changed")
    allv = allv.withColumn("valid_to", F.lead("valid_from").over(w))
    allv = allv.withColumn("is_current", F.col("valid_to").isNull() & (F.col("op") != "d"))
    # 삭제 이벤트는 직전 버전을 닫는 데만 쓰고 자신은 버전이 아니다
    return allv.where(F.col("op") != "d")


def scd2_merge_sql(c: Contract, source_view: str, run_id: str, catalog: str = "glue") -> str:
    keys = list(c.grain[:-1])
    on = " AND ".join(f"t.{k} = s.{k}" for k in keys) + " AND t.valid_from = s.valid_from"
    cols = keys + list(c.scd2_tracked)
    return f"""
MERGE INTO {catalog}.{c.dataset} t
USING {source_view} s
ON {on}
WHEN MATCHED AND (t.valid_to IS DISTINCT FROM s.valid_to OR t.is_current != s.is_current) THEN
  UPDATE SET t.valid_to = s.valid_to, t.is_current = s.is_current, t._ingested_at = current_timestamp(), t._run_id = '{run_id}'
WHEN NOT MATCHED THEN
  INSERT ({", ".join(cols)}, valid_from, valid_to, is_current, _src_seq, _ingested_at, _run_id)
  VALUES ({", ".join("s." + n for n in cols)}, s.valid_from, s.valid_to, s.is_current, s._src_seq, current_timestamp(), '{run_id}')
""".strip()


def append_dedup_sql(c: Contract, source_view: str, run_id: str, catalog: str = "glue") -> str:
    on = " AND ".join(f"t.{k} = s.{k}" for k in c.grain)
    cols = [col.name for col in c.columns]
    return f"""
MERGE INTO {catalog}.{c.dataset} t
USING {source_view} s
ON {on}
WHEN NOT MATCHED THEN
  INSERT ({", ".join(cols)}, _ingested_at, _run_id) VALUES ({", ".join("s." + n for n in cols)}, current_timestamp(), '{run_id}')
""".strip()
