import datetime as dt
from pathlib import Path

import yaml

from p1.governance.access import DIR, check

NOW = dt.datetime(2026, 10, 30, tzinfo=dt.UTC)


def _req(**kw):
    base = yaml.safe_load((DIR / "2026-10-30-example-cohort.yaml").read_text(encoding="utf-8"))
    base.update(kw)
    return base


def test_repository_requests_valid():
    for p in Path(DIR).glob("*.yaml"):
        assert check(yaml.safe_load(p.read_text(encoding="utf-8")), p.stem, NOW) == []


def test_too_long_and_self_approval_and_encrypted_scope():
    errs = check(_req(expires_at="2027-01-01T00:00:00Z", approved_by=["analyst@danbi.example", "x@danbi.example"],
                      scope={"layer": ["gold"], "classification": ["personal_encrypted"]}),
                 "2026-10-30-example-cohort", NOW)  # fmt: skip
    assert any("일 >" in e for e in errs)
    assert any("승인자 2명" in e for e in errs)
    assert any("비상 접근" in e for e in errs)
