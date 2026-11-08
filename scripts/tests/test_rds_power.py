import boto3
import pytest
from moto import mock_aws

from scripts.rds_power import apply, find_instances


@pytest.fixture
def rds():
    with mock_aws():
        client = boto3.client("rds", region_name="ap-northeast-2")
        for ident, env in [("danbi-dev-mysql", "dev"), ("danbi-dev-postgres", "dev"), ("other-db", "prod")]:
            client.create_db_instance(
                DBInstanceIdentifier=ident,
                DBInstanceClass="db.t4g.micro",
                Engine="mysql",
                MasterUsername="dbadmin",
                MasterUserPassword="x" * 16,
                AllocatedStorage=20,
                Tags=[{"Key": "project", "Value": "danbi"}, {"Key": "env", "Value": env}],
            )
        yield client


def test_find_by_tags(rds):
    names = sorted(d["DBInstanceIdentifier"] for d in find_instances(rds, "danbi", "dev"))
    assert names == ["danbi-dev-mysql", "danbi-dev-postgres"]


def test_stop_then_idempotent(rds):
    first = dict(apply(rds, "stop", "danbi", "dev", wait=False, poll_s=0))
    assert set(first.values()) == {"stopping"}
    # moto 는 stop 직후 stopped 로 바뀐다 → 두 번째 호출은 건너뛴다
    second = dict(apply(rds, "stop", "danbi", "dev", wait=False, poll_s=0))
    assert all(v.startswith("skip") for v in second.values())
    other = rds.describe_db_instances(DBInstanceIdentifier="other-db")["DBInstances"][0]
    assert other["DBInstanceStatus"] == "available"


def test_start_after_stop(rds):
    apply(rds, "stop", "danbi", "dev", wait=False, poll_s=0)
    started = dict(apply(rds, "start", "danbi", "dev", wait=False, poll_s=0))
    assert set(started.values()) == {"starting"}
