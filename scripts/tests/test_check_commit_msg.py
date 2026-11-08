from scripts.check_commit_msg import check


def test_valid_message():
    msg = "feat(p1-cdc): Debezium 커넥터 배포 [P1-04]\n\n- 왜: ...\n\nTask: P1-04\n"
    assert check(msg) == []


def test_missing_trailer():
    msg = "feat(p1-cdc): Debezium 커넥터 배포 [P1-04]\n\n본문\n"
    assert any("Task:" in e for e in check(msg))


def test_mismatched_ids():
    msg = "infra(network): VPC [C03]\n\nTask: C04\n"
    assert any("다르다" in e for e in check(msg))


def test_bad_subject():
    assert check("VPC 추가\n\nTask: C03") != []
