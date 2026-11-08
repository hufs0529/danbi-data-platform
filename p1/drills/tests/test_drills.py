import re
from pathlib import Path

from p1.drills.catalog import DRILLS
from p1.drills.run import postmortem, show
from p1.observability.monitor import load_config

ROOT = Path(__file__).resolve().parents[3]
RUNBOOKS = ROOT / "docs" / "runbooks"


def test_all_ten_drills_with_runbooks_and_acceptance():
    assert [d.id for d in DRILLS] == [f"F-{i:02d}" for i in range(1, 11)]
    for d in DRILLS:
        assert (RUNBOOKS / d.runbook).exists(), d.runbook
        assert d.accept and d.expect and d.inject


def test_every_runbook_referenced_anywhere_exists():
    """알람·DAG·코드가 가리키는 런북이 모두 있어야 한다(깨진 링크 = 새벽 3시의 막다른 길)."""
    pat = re.compile(
        r"runbooks?/([a-z0-9-]+\.md)|\"runbook\": \"([a-z0-9-]+\.md)\"|runbook: ([a-z0-9-]+\.md)"
    )
    refs = set()
    for p in list((ROOT / "p1").rglob("*.py")) + list((ROOT / "p1").rglob("*.yaml")) + list((ROOT / "infra").rglob("*.tf")) \
            + list((ROOT / "infra").rglob("*.json")):  # fmt: skip
        for m in pat.finditer(p.read_text(encoding="utf-8")):
            refs.add(next(g for g in m.groups() if g))
    cfg = load_config()
    refs |= {s["runbook"] for s in cfg["slos"]} | {a["runbook"] for a in cfg["alerts"]}
    missing = sorted(r for r in refs if not (RUNBOOKS / r).exists())
    assert missing == []


def test_runbooks_follow_structure():
    for p in RUNBOOKS.glob("*.md"):
        if p.name in ("README.md", "network-debugging.md"):
            continue
        text = p.read_text(encoding="utf-8")
        assert text.startswith("# "), p.name
        assert any(k in text for k in ("확인", "절차", "조치")), p.name


def test_show_and_postmortem_render():
    d = DRILLS[0]
    assert "## 수용 기준" in show(d) and "connect.offsets.cdc" in show(d)
    pm = postmortem(d)
    assert "F-01" in pm and "{{" not in pm
