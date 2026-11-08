import os
import time

import pytest

# PySpark 는 collect() 결과 timestamp 를 파이썬 프로세스의 로컬 시간대로 바꾼다. 테스트는 UTC 로 고정
os.environ["TZ"] = "UTC"
time.tzset()


@pytest.fixture(scope="session")
def spark():
    pyspark = pytest.importorskip("pyspark")
    from pyspark.sql import SparkSession

    s = (
        SparkSession.builder.master("local[1]")
        .appName("tests")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.sql.shuffle.partitions", "1")
        .getOrCreate()
    )
    assert pyspark
    yield s
    s.stop()
