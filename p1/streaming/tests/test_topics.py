import pytest

from p1.streaming.topics import TopicSpec, diff, lint, load_specs, render


def test_repository_topics_pass_lint():
    specs = load_specs()
    assert lint(specs) == []
    names = {s.name for s in specs}
    assert {"clicks.raw", "events.payment", "connect.offsets.cdc", "schemahistory.mysql.commerce"} <= names


def test_pii_topic_retention_must_fit_erasure_slo():
    s = TopicSpec("x", 1, 2, {"retention.ms": str(7 * 24 * 3600 * 1000)}, pii=True)
    assert any("파기 SLO" in e for e in lint([s]))
    infinite = TopicSpec("y", 1, 2, {"retention.ms": "-1"}, pii=True)
    assert lint([infinite])


def test_diff_create_grow_and_config():
    specs = [
        TopicSpec("a", 3, 2, {"retention.ms": "100"}),
        TopicSpec("b", 6, 2, {"retention.ms": "100"}),
        TopicSpec("c", 1, 2, {"retention.ms": "100"}),
    ]
    current = {"b": (3, {"retention.ms": "100"}), "c": (1, {"retention.ms": "999"}), "legacy": (1, {})}
    plan = diff(specs, current)
    assert [s.name for s in plan.create] == ["a"]
    assert plan.add_partitions == {"b": (3, 6)}
    assert plan.alter_config == {"c": {"retention.ms": ("999", "100")}}
    assert plan.unmanaged == ["legacy"]
    assert "+ create a" in render(plan)


def test_partition_decrease_is_refused():
    plan = diff([TopicSpec("a", 3, 2, {})], {"a": (6, {})})
    assert plan.errors and "감소" in plan.errors[0]


@pytest.mark.parametrize("internal", ["__consumer_offsets", "_schemas", "__amazon_msk_connect_status_x"])
def test_internal_topics_not_reported(internal):
    assert diff([], {internal: (1, {})}).unmanaged == []
