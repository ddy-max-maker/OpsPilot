"""D7 回归：容器身份解析（资产登记优先于实例前缀推导）。

真实缺陷复现：演示资产名为 `redis` 时前缀推导得到 `opspilot-demo-redis`（正确）；
但同一容器的另一个资产登记名为 `demo-redis` 时，前缀推导会得到
`opspilot-demo-demo-redis`，docker.inspect / docker.logs 恒判 CONTAINER_NOT_FOUND，
证据链把"没看到容器"误当成"容器无误"。
"""

from __future__ import annotations

from app.domains.tools.gateway import resolve_container_name


def test_registered_container_name_wins() -> None:
    """资产登记了 container_name 时直接使用，不再叠加实例前缀。"""
    assert (
        resolve_container_name(
            endpoints={"container_name": "opspilot-demo-redis"},
            instance_config={"container_prefix": "opspilot-demo-"},
            asset_name="demo-redis",
        )
        == "opspilot-demo-redis"
    )


def test_fallback_to_instance_prefix_when_not_registered() -> None:
    """未登记容器名时保留历史推导（兼容 D2 早期种子：资产名不含前缀）。"""
    assert (
        resolve_container_name(
            endpoints={},
            instance_config={"container_prefix": "opspilot-demo-"},
            asset_name="redis",
        )
        == "opspilot-demo-redis"
    )


def test_empty_registered_value_falls_back() -> None:
    """登记值为空串/None 时同样回退，不产生空容器名。"""
    for empty in ({}, {"container_name": ""}, {"container_name": None}, None):
        assert (
            resolve_container_name(
                endpoints=empty,
                instance_config={"container_prefix": "opspilot-demo-"},
                asset_name="crasher",
            )
            == "opspilot-demo-crasher"
        )


def test_no_prefix_and_no_registration() -> None:
    """前缀为空时容器名即资产名（评估执行器使用空前缀的场景）。"""
    assert (
        resolve_container_name(
            endpoints={}, instance_config={"container_prefix": ""}, asset_name="app"
        )
        == "app"
    )
