"""动作 Allowlist（D5，PRD §8）：唯一合法的有副作用动作注册表。

红线：
- 模型生成的字符串不能直接作为执行命令——执行请求的 action_key 必须在
  本注册表中登记，参数必须匹配该动作声明的 schema；
- r2/r3 不进入执行链路（本表仅含 r0/r1）；
- 新增动作 = 修改本表 + 代码审查 + 部署，不存在运行时动态注册。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import cast


@dataclass(frozen=True)
class ActionSpec:
    """allowlist 动作规格（执行语义固定于代码，不由消息内容决定）。"""

    action_key: str  # 形如 restart_container@v1
    risk_level: str  # r0 | r1
    # 外部接口是否声明支持幂等执行与状态查询（CONTRACTS §2.13：
    # 仅 True 时可声明"恰好一次外部效果"；否则默认保证=零重复提交）
    idempotent: bool
    description: str
    # 参数 JSON Schema（简化校验：required/pattern/enum）
    parameters_schema: dict[str, object] = field(default_factory=dict)


# 参数约束：容器名必须落在演示环境命名空间内（基础设施兜底之外的
# 应用层二次防线；ops-action 进程侧还会按容器名前缀再做一次校验）
_ACTION_CONTAINER_NAME_PATTERN = r"^opspilot-demo-[a-z0-9][a-z0-9-]*$"

ACTION_ALLOWLIST: dict[str, ActionSpec] = {
    "restart_container@v1": ActionSpec(
        action_key="restart_container@v1",
        risk_level="r1",
        idempotent=False,  # docker restart 无原生幂等键/状态查询 → 承诺零重复提交
        description="受控重启演示环境白名单容器（精确容器名绑定，禁止任意 docker 命令）",
        parameters_schema={
            "type": "object",
            "required": ["container_name"],
            "additionalProperties": False,
            "properties": {
                "container_name": {
                    "type": "string",
                    "pattern": _ACTION_CONTAINER_NAME_PATTERN,
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 120,
                    "default": 30,
                },
            },
        },
    ),
}

_CONTAINER_NAME_RE = re.compile(r"^opspilot-demo-[a-z0-9][a-z0-9-]*$")


def is_registered_container_name(name: str) -> bool:
    """容器名是否落在演示环境命名空间（执行侧兜底校验复用）。"""
    return bool(_CONTAINER_NAME_RE.match(name))


def get_action(action_key: str) -> ActionSpec | None:
    """按 action_key 查注册表（未登记动作返回 None → 调用方必须拒绝）。"""
    return ACTION_ALLOWLIST.get(action_key)


def validate_parameters(spec: ActionSpec, parameters: dict[str, object]) -> list[str]:
    """按动作声明的 schema 校验参数（返回错误列表；空列表 = 通过）。

    简化实现覆盖 allowlist 实际用到的约束：type/required/pattern/
    minimum/maximum/enum/additionalProperties。
    """
    errors: list[str] = []
    schema = spec.parameters_schema
    if not isinstance(parameters, dict):
        return ["parameters must be an object"]
    for key in cast_list(schema.get("required")):
        if key not in parameters:
            errors.append(f"missing required parameter '{key}'")
    props = schema.get("properties")
    allowed = {str(k) for k in props} if isinstance(props, dict) else set()
    if schema.get("additionalProperties") is False:
        for key in parameters:
            if key not in allowed:
                errors.append(f"unknown parameter '{key}'")
    if isinstance(props, dict):
        for key, value in parameters.items():
            prop = props.get(key)
            if not isinstance(prop, dict):
                continue
            errors.extend(_validate_value(key, value, prop))
    return errors


def cast_list(value: object) -> list[str]:
    """schema 片段的宽松字符串列表视图（校验辅助，非业务数据路径）。"""
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    if isinstance(value, dict):
        return [str(k) for k in value]
    return []


def _validate_value(key: str, value: object, prop: dict[str, object]) -> list[str]:
    errors: list[str] = []
    expected = prop.get("type")
    if expected == "string":
        if not isinstance(value, str):
            errors.append(f"parameter '{key}' must be a string")
        elif isinstance(prop.get("pattern"), str) and not re.match(str(prop["pattern"]), value):
            errors.append(f"parameter '{key}' violates pattern {prop['pattern']}")
    elif expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"parameter '{key}' must be an integer")
        else:
            minimum, maximum = prop.get("minimum"), prop.get("maximum")
            if isinstance(minimum, int) and value < minimum:
                errors.append(f"parameter '{key}' must be >= {minimum}")
            if isinstance(maximum, int) and value > maximum:
                errors.append(f"parameter '{key}' must be <= {maximum}")
    if isinstance(prop.get("enum"), list) and value not in cast("list[object]", prop["enum"]):
        errors.append(f"parameter '{key}' must be one of {prop['enum']}")
    return errors


def resolve_container_name(
    parameters: dict[str, object], asset_endpoints: dict[str, object] | None
) -> str | None:
    """解析执行目标容器名：参数显式声明优先，其次资产 endpoints 登记。

    目标身份必须与登记一致（资产↔容器精确绑定；不能凭空指定容器）。
    """
    from_params = parameters.get("container_name")
    from_asset = (asset_endpoints or {}).get("container_name")
    if isinstance(from_params, str) and isinstance(from_asset, str):
        return from_params if from_params == from_asset else None
    if isinstance(from_params, str):
        return from_params
    if isinstance(from_asset, str):
        return from_asset
    return None
