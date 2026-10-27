"""Capability Manifest 红线单元测试（CONTRACTS §4：只读；Docker 强制只读代理）。"""

from __future__ import annotations

from app.domains.tools.manifests import (
    ALL_CAPABILITY_KEYS,
    BUILTIN_MANIFESTS,
    manifest_payload,
)


def test_all_capabilities_read_only() -> None:
    for manifest in BUILTIN_MANIFESTS.values():
        assert manifest.read_only is True
        for cap in manifest.capabilities:
            assert cap.source_type is not None
            assert cap.collector


def test_docker_connector_declares_proxy_dependency() -> None:
    """ADR-11：Docker 能力必须挂在 docker-ro 连接器上（经只读代理）。"""
    docker_caps = [c for c in ALL_CAPABILITY_KEYS.values() if c.connector_code == "docker-ro"]
    assert {c.key for c in docker_caps} == {"get_container_status", "read_container_logs"}


def test_manifest_payload_shape() -> None:
    payload = manifest_payload(BUILTIN_MANIFESTS["redis-ro"])
    assert payload["manifest_version"] == "1"
    assert payload["read_only"] is True
    assert payload["capabilities"][0]["capability_key"] == "check_redis_health"
    assert payload["rate_limit"]["max_calls_per_run"] > 0
    assert payload["timeout_ms"] > 0


def test_capability_keys_unique() -> None:
    keys = [c.key for c in ALL_CAPABILITY_KEYS.values()]
    assert len(keys) == len(set(keys))
    assert set(keys) == {
        "get_service_health",
        "get_container_status",
        "read_container_logs",
        "check_redis_health",
    }
