"""访问控制领域：RBAC 角色判定与资产范围（asset_access_policies）解析。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.core.enums import Environment, RoleCode


@dataclass(frozen=True)
class PolicyRecord:
    """asset_access_policies 行的不可变快照（user/role 互斥由 DB CHECK 兜底）。"""

    user_id: str | None
    role_id: str | None
    asset_id: str | None
    environment: str | None
    permission: str


class ScopePermission(StrEnum):
    """范围判定关注的权限维度（D1-A 业务只用到 read）。"""

    READ = "read"


@dataclass(frozen=True)
class AssetScope:
    """用户在某权限维度上的可见资产集合（并集语义）。

    - is_admin: Admin 不受资产范围限制（CONTRACTS §7.2）
    - all_assets: 命中"asset_id 与 environment 均为空 = 全部"的策略
    - asset_ids / environments: 显命中的资产/环境集合
    - 默认拒绝：三者皆空且非 admin 时，任何资产均不可见
    """

    is_admin: bool
    all_assets: bool
    asset_ids: frozenset[str]
    environments: frozenset[str]

    def allows(self, asset_id: str, environment: str) -> bool:
        """单资产访问判定；调用方对 False 结果按 404 伪装处理（不暴露存在性）。"""
        if self.is_admin or self.all_assets:
            return True
        return asset_id in self.asset_ids or environment in self.environments

    def is_empty(self) -> bool:
        return not (self.is_admin or self.all_assets or self.asset_ids or self.environments)


def resolve_scope(
    role_codes: list[str] | frozenset[str],
    policies: list[PolicyRecord],
    permission: str = ScopePermission.READ,
) -> AssetScope:
    """纯函数：用户级策略 ∪ 角色级策略（CONTRACTS §7.2）。

    无匹配策略且非 Admin => 默认拒绝（空集合），不存在"空策略=全开放"。
    """
    is_admin = RoleCode.ADMIN in role_codes
    all_assets = False
    asset_ids: set[str] = set()
    environments: set[str] = set()
    if not is_admin:
        for policy in policies:
            if policy.permission != permission:
                continue
            if policy.asset_id is None and policy.environment is None:
                all_assets = True  # 均为空 = 全部
            elif policy.asset_id is not None:
                asset_ids.add(policy.asset_id)
            elif policy.environment is not None:
                environments.add(policy.environment)
    return AssetScope(
        is_admin=is_admin,
        all_assets=all_assets,
        asset_ids=frozenset(asset_ids),
        environments=frozenset(environments),
    )


def admin_scope() -> AssetScope:
    """Admin 全范围（不受资产范围限制）。"""
    return AssetScope(
        is_admin=True,
        all_assets=True,
        asset_ids=frozenset(),
        environments=frozenset(Environment),
    )
