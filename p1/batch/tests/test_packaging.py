import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_contracts_load_from_zip(tmp_path):
    """Spark 잡은 --py-files zip 안에서 계약을 읽는다(파일 시스템 glob 이 아니라 importlib.resources)."""
    z = tmp_path / "jobs.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for p in (ROOT / "contracts").rglob("*"):
            if p.is_file() and "tests" not in p.parts and "__pycache__" not in p.parts:
                zf.write(p, p.relative_to(ROOT))
    code = "from contracts.registry import load_all; print(len(load_all()))"
    out = subprocess.run(  # noqa: S603 (테스트가 만든 고정 명령)
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(z)!r}); {code}"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert int(out.stdout.strip()) >= 16


def test_spark_defaults_single_source():
    from p1.batch.jobs.common import SPARK_CONF, submit_params

    on_disk = json.loads((ROOT / "p1" / "batch" / "spark-defaults.json").read_text())
    assert on_disk == SPARK_CONF
    params = submit_params("x.py", "s3://silver/", py_files="s3://a/p1-jobs.zip")
    assert "--conf spark.sql.catalog.glue.warehouse=s3://silver/" in params and params.endswith("p1-jobs.zip")
