"""토픽 as code: topics.yaml 과 클러스터의 차이를 계산하고(plan) 적용한다(apply).

    python -m p1.streaming.topics plan
    python -m p1.streaming.topics apply

VPC 안(msk-clients SG 를 단 ECS 태스크)에서 실행한다. 부트스트랩은 SSM 에서 읽는다.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

TOPICS_FILE = Path(__file__).with_name("topics.yaml")
MAX_PII_RETENTION_MS = 72 * 3600 * 1000  # 파기 SLO (S-G4)


@dataclass(frozen=True)
class TopicSpec:
    name: str
    partitions: int
    replication_factor: int
    config: dict[str, str]
    pii: bool = False


@dataclass
class Plan:
    create: list[TopicSpec] = field(default_factory=list)
    add_partitions: dict[str, tuple[int, int]] = field(default_factory=dict)  # name → (현재, 목표)
    alter_config: dict[str, dict[str, tuple[str | None, str]]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    unmanaged: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.create or self.add_partitions or self.alter_config)


def load_specs(path: Path = TOPICS_FILE) -> list[TopicSpec]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    d = doc.get("defaults", {})
    specs = []
    for t in doc["topics"]:
        cfg = {**d.get("config", {}), **t.get("config", {})}
        specs.append(TopicSpec(t["name"], int(t["partitions"]), int(t.get("replication_factor", d.get("replication_factor", 2))),
                               {k: str(v) for k, v in cfg.items()}, bool(t.get("pii", False))))  # fmt: skip
    return specs


def lint(specs: list[TopicSpec]) -> list[str]:
    errs = []
    names = [s.name for s in specs]
    for dup in {n for n in names if names.count(n) > 1}:
        errs.append(f"{dup}: 중복 정의")
    for s in specs:
        retention = int(s.config.get("retention.ms", "-1"))
        if s.pii and (retention < 0 or retention > MAX_PII_RETENTION_MS):
            errs.append(f"{s.name}: PII 토픽 보존 {retention}ms > 파기 SLO 72h")
        if s.partitions < 1:
            errs.append(f"{s.name}: 파티션 1 이상")
    return errs


def diff(specs: list[TopicSpec], current: dict[str, tuple[int, dict[str, str]]]) -> Plan:
    """current: 토픽 → (파티션 수, 우리가 관리하는 키의 현재 설정값)"""
    plan = Plan(errors=lint(specs))
    wanted = {s.name for s in specs}
    plan.unmanaged = sorted(n for n in current if n not in wanted and not n.startswith(("_", "__")))
    for s in specs:
        if s.name not in current:
            plan.create.append(s)
            continue
        parts, cfg = current[s.name]
        if s.partitions < parts:
            plan.errors.append(
                f"{s.name}: 파티션 감소 {parts}→{s.partitions} 는 허용하지 않는다(키 순서 보장 붕괴)"
            )
        elif s.partitions > parts:
            plan.add_partitions[s.name] = (parts, s.partitions)
        changes = {k: (cfg.get(k), v) for k, v in s.config.items() if cfg.get(k) != v}
        if changes:
            plan.alter_config[s.name] = changes
    return plan


def render(plan: Plan) -> str:
    lines = []
    for s in plan.create:
        lines.append(f"+ create {s.name} partitions={s.partitions} rf={s.replication_factor}")
    for n, (a, b) in plan.add_partitions.items():
        lines.append(f"~ partitions {n}: {a} → {b}  (주의: 기존 키의 파티션이 바뀐다. 컨슈머 상태 확인)")
    for n, ch in plan.alter_config.items():
        for k, (a, b) in ch.items():
            lines.append(f"~ config {n} {k}: {a} → {b}")
    for n in plan.unmanaged:
        lines.append(f"? unmanaged {n} (topics.yaml 에 없다)")
    for e in plan.errors:
        lines.append(f"! {e}")
    return "\n".join(lines) or "no changes"


# ---------------------------------------------------------------------------
def _admin():
    import os

    from confluent_kafka.admin import AdminClient

    from danbi_common.aws import get_parameter
    from danbi_common.kafka import client_config

    bootstrap = os.environ.get("MSK_BOOTSTRAP") or get_parameter(
        os.environ.get("MSK_BOOTSTRAP_PARAM", "/danbi/dev/p1/streaming/bootstrap_brokers_iam")
    )
    return AdminClient(client_config(bootstrap))


def current_state(admin, keys: set[str]) -> dict[str, tuple[int, dict[str, str]]]:
    from confluent_kafka.admin import ConfigResource

    md = admin.list_topics(timeout=20)
    out: dict[str, tuple[int, dict[str, str]]] = {}
    resources = [ConfigResource(ConfigResource.Type.TOPIC, n) for n in md.topics]
    configs = admin.describe_configs(resources) if resources else {}
    for res, fut in configs.items():
        entries = fut.result()
        out[res.name] = (
            len(md.topics[res.name].partitions),
            {k: entries[k].value for k in keys if k in entries},
        )
    return out


def apply(admin, plan: Plan) -> None:
    from confluent_kafka.admin import AlterConfigOpType, ConfigEntry, ConfigResource, NewPartitions, NewTopic

    if plan.create:
        futs = admin.create_topics([NewTopic(s.name, s.partitions, s.replication_factor, config=s.config)
                                    for s in plan.create])  # fmt: skip
        for n, f in futs.items():
            f.result()
            print(f"created {n}")
    if plan.add_partitions:
        futs = admin.create_partitions([NewPartitions(n, b) for n, (_, b) in plan.add_partitions.items()])
        for n, f in futs.items():
            f.result()
            print(f"partitions {n}")
    for n, ch in plan.alter_config.items():
        res = ConfigResource(ConfigResource.Type.TOPIC, n, incremental_configs=[
            ConfigEntry(k, v, incremental_operation=AlterConfigOpType.SET) for k, (_, v) in ch.items()])  # fmt: skip
        admin.incremental_alter_configs([res])[res].result()
        print(f"config {n}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["lint", "plan", "apply"])
    args = p.parse_args(argv)
    specs = load_specs()
    if args.action == "lint":
        errs = lint(specs)
        print("\n".join(errs) or "ok")
        return 1 if errs else 0
    admin = _admin()
    keys = {k for s in specs for k in s.config}
    plan = diff(specs, current_state(admin, keys))
    print(render(plan))
    if plan.errors:
        return 1
    if args.action == "apply" and not plan.empty:
        apply(admin, plan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
