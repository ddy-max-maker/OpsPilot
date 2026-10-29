"""Allowlist 动作执行器（D5）：动作前状态检查 + 受控外部动作 + 结果归类。

结果归类（CONTRACTS §2.13 崩溃语义，对应用户指令 §4 A–D 场景）：
- PRE_CHECK_FAILED：动作前检查未通过（外部动作从未发起，外部效果确定为零）；
- SUCCEEDED：外部 API 明确返回成功；
- FAILED：外部系统明确拒绝（如容器不存在 404 / 状态冲突 409）——效果确定为未发生；
- UNKNOWN：请求可能已发出但结果不可观测（超时/连接中断/5xx）——禁止盲目重试。

模型生成的字符串永远不进入执行路径：容器名在 allowlist 校验与资产绑定
复核后才被使用，且仅作为 URL 段调用固定端点。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

from app.action.docker_client import DockerClient
from app.domains.approvals.allowlist import is_registered_container_name

logger = logging.getLogger("ops-action.executor")


class OutcomeStatus(StrEnum):
    """执行器结果归类（action_executions.status 的执行器侧子集）。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    PRE_CHECK_FAILED = "pre_check_failed"


@dataclass(frozen=True)
class ExecutionOutcome:
    """单次动作执行的完整结果（审计落库用；不含敏感信息）。"""

    status: OutcomeStatus
    result_detail: dict[str, Any] = field(default_factory=dict)
    external_ref: str | None = None


class ActionExecutor:
    """Allowlist 动作执行器：目前仅支持 restart_container@v1。

    安全门禁（D6）：execution_enabled=False（默认）时所有动作直接
    PRE_CHECK_FAILED（demo-only 守卫）——应用层 allowlist 不等于基础设施
    级隔离，真实执行仅在专用隔离演示 Daemon 上显式开启。
    """

    def __init__(
        self,
        *,
        container_name_prefix: str,
        docker: DockerClient | None = None,
        execution_enabled: bool = True,
    ) -> None:
        self._prefix = container_name_prefix
        self._docker = docker or DockerClient()
        self._execution_enabled = execution_enabled

    @property
    def execution_enabled(self) -> bool:
        return self._execution_enabled

    async def precheck_container(self, container_name: str) -> dict[str, Any] | None:
        """动作前检查：命名空间 allowlist + 容器实际存在（真实状态记录进审计）。

        不强制 Running：restart 对 stopped/paused 容器同样是合法受控操作
        （语义 = 确保容器以全新状态运行）；容器真实状态随 inspect 记录。
        """
        if not is_registered_container_name(container_name):
            raise PreCheckError(f"container '{container_name}' is outside the registered namespace")
        if not container_name.startswith(self._prefix):
            raise PreCheckError(
                f"container '{container_name}' does not match the deployment prefix "
                f"'{self._prefix}'"
            )
        info = await self._docker.inspect_container(container_name)
        if info is None:
            raise PreCheckError(f"container '{container_name}' does not exist on this host")
        return info

    async def execute_restart_container(
        self, *, container_name: str, timeout_seconds: int = 30
    ) -> ExecutionOutcome:
        """restart_container@v1：前检查 → 受控重启 → 真实结果归类。"""
        try:
            info = await self.precheck_container(container_name)
        except PreCheckError as exc:
            return ExecutionOutcome(
                status=OutcomeStatus.PRE_CHECK_FAILED,
                result_detail={"reason": str(exc)},
            )
        info_map: dict[str, Any] = info or {}
        container_id = str(info_map.get("Id") or "")[:12]
        old_started_at = (info_map.get("State") or {}).get("StartedAt")
        try:
            await self._docker.restart_container(container_name, timeout_seconds=timeout_seconds)
        except httpx.TimeoutException:
            # 场景 D：请求可能已发出，结果不可观测 → UNKNOWN（禁止盲目重试）
            return ExecutionOutcome(
                status=OutcomeStatus.UNKNOWN,
                result_detail={
                    "reason": "restart request timed out; external outcome unverifiable",
                    "container": container_name,
                    "container_id": container_id,
                },
                external_ref=f"docker:{container_id}",
            )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code in (404, 409):
                # 明确拒绝：容器不存在/状态不允许 → 效果确定为未发生
                return ExecutionOutcome(
                    status=OutcomeStatus.FAILED,
                    result_detail={
                        "reason": f"docker api rejected restart ({status_code})",
                        "container": container_name,
                        "container_id": container_id,
                    },
                    external_ref=f"docker:{container_id}",
                )
            # 5xx/其他：无法确定重启是否生效 → UNKNOWN
            return ExecutionOutcome(
                status=OutcomeStatus.UNKNOWN,
                result_detail={
                    "reason": f"docker api error ({status_code}); outcome unverifiable",
                    "container": container_name,
                    "container_id": container_id,
                },
                external_ref=f"docker:{container_id}",
            )
        except httpx.HTTPError as exc:
            # 连接中断：请求可能已到达 → UNKNOWN
            return ExecutionOutcome(
                status=OutcomeStatus.UNKNOWN,
                result_detail={
                    "reason": f"docker connection error ({type(exc).__name__})",
                    "container": container_name,
                    "container_id": container_id,
                },
                external_ref=f"docker:{container_id}",
            )
        # 成功后回读真实状态（StartedAt 应已刷新；作为外部执行证据）
        new_info = await self._docker.inspect_container(container_name)
        new_state = (new_info or {}).get("State") or {}
        new_started_at = new_state.get("StartedAt")
        running = bool(new_state.get("Running"))
        if new_info is not None and new_started_at == old_started_at:
            return ExecutionOutcome(
                status=OutcomeStatus.UNKNOWN,
                result_detail={
                    "reason": "restart acknowledged but container StartedAt unchanged",
                    "container": container_name,
                    "container_id": container_id,
                },
                external_ref=f"docker:{container_id}",
            )
        return ExecutionOutcome(
            status=OutcomeStatus.SUCCEEDED if running else OutcomeStatus.UNKNOWN,
            result_detail={
                "container": container_name,
                "container_id": container_id,
                "started_at": new_started_at,
                "running": running,
            },
            external_ref=f"docker:{container_id}@{new_started_at}",
        )

    async def execute(self, *, action_key: str, parameters: dict[str, Any]) -> ExecutionOutcome:
        """统一入口：action_key → 具体动作（未登记动作显式拒绝）。"""
        if not self._execution_enabled:
            # D6 安全门禁：未显式开启真实执行时一律拒绝（demo-only）
            return ExecutionOutcome(
                status=OutcomeStatus.PRE_CHECK_FAILED,
                result_detail={
                    "reason": "action execution is disabled (demo-only guard); real "
                    "execution requires ACTION_EXECUTION_ENABLED=true on an isolated "
                    "demo daemon"
                },
            )
        if action_key == "restart_container@v1":
            return await self.execute_restart_container(
                container_name=str(parameters["container_name"]),
                timeout_seconds=int(parameters.get("timeout_seconds", 30)),
            )
        return ExecutionOutcome(
            status=OutcomeStatus.PRE_CHECK_FAILED,
            result_detail={"reason": f"action '{action_key}' is not implemented by this executor"},
        )


class PreCheckError(RuntimeError):
    """动作前检查未通过（外部动作不发起）。"""
