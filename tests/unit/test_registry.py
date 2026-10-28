"""D3 Registry 单元测试：manifest 兼容校验 + 实例解析语义。"""

from __future__ import annotations

import pytest

from app.domains.tools.manifests import BUILTIN_MANIFESTS, manifest_payload
from app.domains.tools.registry import (
    ConnectorResolutionError,
    ResolutionMap,
    ResolvedInstance,
    validate_manifest,
)


class TestValidateManifest:
    def test_all_builtin_manifests_valid(self) -> None:
        for code, manifest in BUILTIN_MANIFESTS.items():
            payload = manifest_payload(manifest)
            assert validate_manifest(payload) == [], f"{code} manifest invalid"

    def test_unsupported_manifest_version_rejected(self) -> None:
        payload = manifest_payload(BUILTIN_MANIFESTS["redis-ro"])
        payload["manifest_version"] = "99"
        assert any("manifest_version" in e for e in validate_manifest(payload))

    def test_write_capability_rejected(self) -> None:
        payload = manifest_payload(BUILTIN_MANIFESTS["redis-ro"])
        payload["capabilities"][0]["read_only"] = False
        assert any("read_only" in e for e in validate_manifest(payload))

    def test_duplicate_capability_keys_rejected(self) -> None:
        payload = manifest_payload(BUILTIN_MANIFESTS["redis-ro"])
        payload["capabilities"].append(dict(payload["capabilities"][0]))
        assert any("duplicate" in e for e in validate_manifest(payload))

    def test_min_platform_ahead_of_platform_rejected(self) -> None:
        payload = manifest_payload(BUILTIN_MANIFESTS["redis-ro"])
        payload["compatibility"] = {"min_platform": "9.9.9"}
        assert any("min_platform" in e for e in validate_manifest(payload))


class TestResolutionMap:
    def _inst(self, iid: str, code: str) -> ResolvedInstance:
        return ResolvedInstance(instance_id=iid, connector_code=code, config={"k": iid})

    def test_binding_hit_wins(self) -> None:
        m = ResolutionMap(
            enabled_by_type={"prom-ro": [self._inst("i1", "prom-ro"), self._inst("i2", "prom-ro")]},
            bindings={("as_a", "prom-ro"): self._inst("i2", "prom-ro")},
        )
        assert m.resolve("prom-ro", "as_a").instance_id == "i2"

    def test_single_instance_default(self) -> None:
        m = ResolutionMap(enabled_by_type={"prom-ro": [self._inst("only", "prom-ro")]})
        assert m.resolve("prom-ro", "as_x").instance_id == "only"

    def test_multiple_without_binding_rejected(self) -> None:
        """多实例未绑定 → 拒绝（防止串用其他资产的凭据/数据）。"""
        m = ResolutionMap(
            enabled_by_type={"prom-ro": [self._inst("i1", "prom-ro"), self._inst("i2", "prom-ro")]}
        )
        with pytest.raises(ConnectorResolutionError) as exc:
            m.resolve("prom-ro", "as_x")
        assert exc.value.code == "INSTANCE_AMBIGUOUS"

    def test_no_instance(self) -> None:
        m = ResolutionMap()
        with pytest.raises(ConnectorResolutionError) as exc:
            m.resolve("prom-ro", "as_x")
        assert exc.value.code == "CONNECTOR_NOT_CONFIGURED"

    def test_binding_to_disabled_instance_ignored(self) -> None:
        """绑定指向的实例不在 enabled 表 → 回退类型解析（disabled 实例不可用）。"""
        m = ResolutionMap(enabled_by_type={"prom-ro": [self._inst("enabled", "prom-ro")]})
        # bindings 里没有该键（load 阶段已过滤 disabled）→ 走默认
        assert m.resolve("prom-ro", "as_x").instance_id == "enabled"
