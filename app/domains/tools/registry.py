"""Capability Registry（D3，CONTRACTS §2.8/§4）。

职责：
1. Manifest 兼容性校验：注册（启动同步 capability_manifests）与调用前均校验
   manifest_version/read_only/compatibility.min_platform；不兼容 → 拒绝注册或调用；
2. 连接器实例解析：**依据授权资产逐资产解析**（asset_observability_bindings），
   修复 D2 按 connector_type 聚合导致同类多实例相互覆盖的问题：
   - 命中绑定 → 使用该实例配置；
   - 未绑定且该类型仅一个 enabled 实例 → 使用默认实例（D2 兼容）；
   - 未绑定且存在多个 enabled 实例 → 拒绝（防止串用其他资产的凭据/数据）。

仅支持仓库内代码审查过的连接器（BUILTIN_MANIFESTS），不实现脚本上传执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AssetObservabilityBinding, ConnectorInstance, ConnectorType

SUPPORTED_MANIFEST_VERSIONS = {"1"}
# 平台当前版本（pyproject 0.1.0；compatibility.min_platform 语义兼容判定）
PLATFORM_VERSION = "0.1.0"


class ManifestRejected(ValueError):
    """Manifest 不兼容/不合法 → 拒绝注册或调用。"""


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    """按 CONTRACTS §4 校验 manifest；返回错误列表（空 = 合法）。"""
    errors: list[str] = []
    if manifest.get("manifest_version") not in SUPPORTED_MANIFEST_VERSIONS:
        errors.append(
            f"unsupported manifest_version {manifest.get('manifest_version')!r} "
            f"(supported: {sorted(SUPPORTED_MANIFEST_VERSIONS)})"
        )
    if not manifest.get("connector_id"):
        errors.append("connector_id is required")
    if not manifest.get("capabilities"):
        errors.append("at least one capability is required")
    seen: set[str] = set()
    for cap in manifest.get("capabilities") or []:
        key = cap.get("capability_key")
        if not key:
            errors.append("capability_key is required")
            continue
        if key in seen:
            errors.append(f"duplicate capability_key {key!r}")
        seen.add(key)
        if cap.get("read_only") is not True:
            # PRD §5：所有 capability read_only=true；写能力不在 Manifest 模型内
            errors.append(f"capability {key!r} must be read_only=true")
    compat = manifest.get("compatibility") or {}
    min_platform = str(compat.get("min_platform") or "0.0.0")
    if _parse_version(min_platform) > _parse_version(PLATFORM_VERSION):
        errors.append(
            f"manifest requires min_platform >= {min_platform}, platform is {PLATFORM_VERSION}"
        )
    scopes = manifest.get("scopes") or {}
    envs = scopes.get("environments")
    if envs is not None and not isinstance(envs, list):
        errors.append("scopes.environments must be a list")
    return errors


def _parse_version(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in version.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


@dataclass(frozen=True)
class ResolvedInstance:
    """一次工具调用的实例解析结果（配置 + 实例标识，进 Evidence 审计）。

    source 标记解析路径（D4 安全修复）：
    - "binding"：按授权资产的观测绑定解析（配置含绑定级 config，服务标签可信）；
    - "default"：单一 enabled 实例默认（实例级 service 默认值仅在该实例
      未被其他资产的 service 绑定共用时允许使用，防止错误映射到其他服务）；
    - "fallback"：D2 兼容路径（测试直连 connector_configs）。
    """

    instance_id: str
    connector_code: str
    config: dict[str, Any] = field(default_factory=dict)
    source: str = "fallback"


class ConnectorResolutionError(RuntimeError):
    """实例解析失败（未配置/未绑定多实例歧义）→ Evidence unavailable。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResolutionMap:
    """run 启动时一次性加载的实例解析表（授权资产范围内）。"""

    enabled_by_type: dict[str, list[ResolvedInstance]] = field(default_factory=dict)
    bindings: dict[tuple[str, str], ResolvedInstance] = field(default_factory=dict)
    # D4：显式声明了 service 的绑定所用实例集合——共享实例的 service 默认值
    # 不得被未绑定资产借用（防止查询映射到其他服务）
    service_bound_instances: frozenset[str] = field(default_factory=frozenset)

    def resolve(self, connector_code: str, asset_id: str) -> ResolvedInstance:
        """按 (资产, 连接器类型) 解析实例；歧义/缺失抛 ConnectorResolutionError。"""
        bound = self.bindings.get((asset_id, connector_code))
        if bound is not None:
            return bound
        candidates = self.enabled_by_type.get(connector_code, [])
        if not candidates:
            raise ConnectorResolutionError(
                "CONNECTOR_NOT_CONFIGURED",
                f"no enabled instance for connector '{connector_code}'",
            )
        if len(candidates) > 1:
            raise ConnectorResolutionError(
                "INSTANCE_AMBIGUOUS",
                f"connector '{connector_code}' has {len(candidates)} enabled instances; "
                f"asset '{asset_id}' has no binding — refusing to guess credentials",
            )
        return candidates[0]


async def load_resolution_map(
    session_factory: async_sessionmaker[AsyncSession],
) -> ResolutionMap:
    """加载 enabled 实例与资产绑定（Worker 每次 run 前刷新）。"""
    m = ResolutionMap()
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(ConnectorInstance, ConnectorType)
                .join(ConnectorType, ConnectorInstance.connector_type_id == ConnectorType.id)
                .where(ConnectorInstance.status == "enabled")
            )
        ).all()
        binding_rows = (
            await session.execute(
                select(AssetObservabilityBinding, ConnectorType)
                .join(
                    ConnectorInstance, AssetObservabilityBinding.instance_id == ConnectorInstance.id
                )
                .join(ConnectorType, ConnectorInstance.connector_type_id == ConnectorType.id)
                .where(ConnectorInstance.status == "enabled")
            )
        ).all()
    for instance, ctype in rows:
        cfg = dict(instance.config or {})
        m.enabled_by_type.setdefault(ctype.code, []).append(
            ResolvedInstance(
                instance_id=instance.id, connector_code=ctype.code, config=cfg, source="default"
            )
        )

    def _find(code: str, instance_id: str) -> ResolvedInstance | None:
        return next(
            (i for i in m.enabled_by_type.get(code, []) if i.instance_id == instance_id), None
        )

    service_bound = frozenset(
        bound.instance_id
        for binding, ctype in binding_rows
        if (binding.config or {}).get("service")
        for bound in (_find(ctype.code, binding.instance_id),)
        if bound is not None
    )
    for binding, ctype in binding_rows:
        bound = _find(ctype.code, binding.instance_id)
        if bound is None:
            continue
        merged = dict(bound.config)
        merged.update(dict(binding.config or {}))
        m.bindings[(binding.asset_id, ctype.code)] = ResolvedInstance(
            instance_id=bound.instance_id,
            connector_code=ctype.code,
            config=merged,
            source="binding",
        )
    return ResolutionMap(
        enabled_by_type=m.enabled_by_type,
        bindings=m.bindings,
        service_bound_instances=service_bound,
    )
