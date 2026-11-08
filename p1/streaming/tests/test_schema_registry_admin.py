import json

import httpx

from p1.streaming.schema_registry.admin import Registry, local_schemas, run


def _registry(handler):
    return Registry("http://sr", httpx.Client(base_url="http://sr", transport=httpx.MockTransport(handler)))


def test_local_schemas_are_normalized_json():
    schemas = local_schemas()
    s = schemas["clicks.raw-value"]
    assert " " not in s.split('"doc"')[0]  # 구분자 공백 제거
    assert json.loads(s)["name"] == "ClickEvent"


def test_register_only_when_compatible():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.url.path.startswith("/compatibility"):
            return httpx.Response(
                200, json={"is_compatible": False, "messages": ["READER_FIELD_MISSING_DEFAULT_VALUE"]}
            )
        return httpx.Response(200, json={"id": 1})

    assert run("register", _registry(handler)) == 1
    assert ("POST", "/subjects/clicks.raw-value/versions") not in calls


def test_first_version_registers():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.startswith("/compatibility"):
            return httpx.Response(404, json={"error_code": 40401})
        return httpx.Response(200, json={"id": 7})

    assert run("register", _registry(handler)) == 0


def test_config_sets_global_and_subject_levels():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen[req.url.path] = json.loads(req.content)["compatibility"]
        return httpx.Response(200, json={})

    run("config", _registry(handler))
    assert seen == {"/config": "BACKWARD", "/config/clicks.raw-value": "FULL_TRANSITIVE"}
