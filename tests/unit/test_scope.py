"""资产范围判定（resolve_scope / AssetScope）单元测试：纯函数，无 IO。

覆盖：默认拒绝、Admin 旁路、user 级 ∪ role 级并集、通配策略、单资产策略、
权限维度过滤、单资产访问判定（越权伪装语义的判定来源）。
"""

from __future__ import annotations

from app.domains.access.scope import AssetScope, PolicyRecord, resolve_scope


def _policy(
    *,
    user_id: str | None = None,
    role_id: str | None = None,
    asset_id: str | None = None,
    environment: str | None = None,
    permission: str = "read",
) -> PolicyRecord:
    return PolicyRecord(
        user_id=user_id,
        role_id=role_id,
        asset_id=asset_id,
        environment=environment,
        permission=permission,
    )


# ---- 默认拒绝 ----


def test_default_deny_no_policies() -> None:
    scope = resolve_scope(["operator"], [])
    assert scope.is_empty()
    assert not scope.allows("as_any", "production")
    assert not scope.allows("as_any", "sandbox")


def test_default_deny_viewer_role_without_policy() -> None:
    """空策略 != 全开放。"""
    scope = resolve_scope(["viewer"], [])
    assert scope.asset_ids == frozenset()
    assert scope.environments == frozenset()
    assert not scope.all_assets


# ---- Admin 旁路 ----


def test_admin_bypasses_scope() -> None:
    scope = resolve_scope(["admin"], [])
    assert scope.is_admin
    assert scope.allows("as_prod_1", "production")


# ---- user 级策略 ----


def test_user_level_environment_grant() -> None:
    scope = resolve_scope(["operator"], [_policy(user_id="usr_1", environment="sandbox")])
    assert scope.environments == frozenset({"sandbox"})
    assert scope.allows("as_sb", "sandbox")
    assert not scope.allows("as_pd", "production")


def test_user_level_asset_grant() -> None:
    scope = resolve_scope(["operator"], [_policy(user_id="usr_1", asset_id="as_x")])
    assert scope.asset_ids == frozenset({"as_x"})
    assert scope.allows("as_x", "production")  # 单资产授权与环境无关
    assert not scope.allows("as_y", "sandbox")


# ---- role 级策略与并集 ----


def test_role_level_environment_grant() -> None:
    scope = resolve_scope(["operator"], [_policy(role_id="rol_operator", environment="staging")])
    assert scope.environments == frozenset({"staging"})
    assert scope.allows("as_st", "staging")


def test_user_role_union() -> None:
    """授权 = user 级策略 ∪ 所属 role 级策略。"""
    scope = resolve_scope(
        ["operator"],
        [
            _policy(user_id="usr_1", environment="sandbox"),
            _policy(role_id="rol_operator", environment="staging"),
        ],
    )
    assert scope.environments == frozenset({"sandbox", "staging"})
    assert not scope.allows("as_pd", "production")


# ---- 通配与权限维度 ----


def test_wildcard_policy_all_assets() -> None:
    """asset_id 与 environment 均为空 = 全部。"""
    scope = resolve_scope(["operator"], [_policy(user_id="usr_1")])
    assert scope.all_assets
    assert scope.allows("as_any", "production")


def test_permission_dimension_filter() -> None:
    """approve 权限策略不授予 read 范围。"""
    scope = resolve_scope(
        ["operator"], [_policy(user_id="usr_1", environment="sandbox", permission="approve")]
    )
    assert scope.is_empty()
    investigate = resolve_scope(
        ["operator"],
        [_policy(user_id="usr_1", environment="sandbox", permission="approve")],
        permission="approve",
    )
    assert investigate.environments == frozenset({"sandbox"})


# ---- 判定辅助 ----


def test_is_empty_and_admin_scope_helper() -> None:
    assert AssetScope(
        is_admin=False, all_assets=False, asset_ids=frozenset(), environments=frozenset()
    ).is_empty()
    admin = resolve_scope(["admin"], [_policy(user_id="usr_9", environment="sandbox")])
    assert admin.is_admin  # Admin 策略被忽略，直接全范围
