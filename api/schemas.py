"""请求/响应 Pydantic 模型（设计文档第 6/7 节）。

M1：同步 invoke + capabilities 所需字段
M2：tasks/resume/cancel + 任务状态机 + 中断载荷
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ════════════════════════════════════════════════════════════════
# 任务状态机（设计文档第 8 节）
# ════════════════════════════════════════════════════════════════

class TaskStatus(str, Enum):
    """任务状态枚举（设计文档第 8 节状态机）。"""
    PENDING = "pending"            # 已入库，等待调度
    PLANNING = "planning"          # Planner 正在产出计划
    RUNNING = "running"            # 正在执行技能/步骤
    WAITING_HUMAN = "waiting_human"  # 命中 ask/confirm interrupt，等待 resume
    COMPLETED = "completed"        # 成功，输出可取
    FAILED = "failed"              # 失败（终态）
    TIMEOUT = "timeout"           # 超时（终态）
    CANCELLED = "cancelled"        # 已取消（终态）
    REJECTED = "rejected"          # 鉴权/契约校验未过，不落任务（终态）


# 终态集合：不再允许 resume/cancel（cancel 对 running 允许，对 completed 不允许）
TERMINAL_STATES = frozenset({
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.TIMEOUT,
    TaskStatus.CANCELLED,
    TaskStatus.REJECTED,
})


# ════════════════════════════════════════════════════════════════
# 请求体（与 /invoke 共用）
# ════════════════════════════════════════════════════════════════

class AgentRequest(BaseModel):
    """POST /agent/invoke 与 POST /agent/tasks 的统一请求体。

    scene 与 inputs 配对；给定 scene 时 inputs 的键由注册表校验。
    message 与 scene 可同时给出，scene 优先，message 作为补充说明。
    """
    message: str | None = Field(default=None, description="自由文本（智能编排必填）")
    scene: str | None = Field(default=None, description="场景码，取自 scenes.yaml；给定后跳过 Planner 直达技能")
    inputs: dict[str, Any] | None = Field(default=None, description="场景码对应技能的输入，键必须符合技能 inputs 契约")

    thread_id: str | None = Field(default=None, description="会话 ID，不传则新建并在响应返回")

    context: dict[str, Any] | None = Field(
        default=None,
        description="业务上下文（user_id/requirement_id 等），随调用透传，不作为技能必填入参",
    )
    reference_data: dict[str, Any] | None = Field(
        default=None,
        description="后端预取的参考资料（模式 A），只读注入到技能 prompt 末尾",
    )

    response_format: str | None = Field(
        default=None,
        description="text | json；不传则按 scenes.yaml 的 response_format；冲突时报错",
    )

    enabled_tools: list[str] | None = Field(
        default=None,
        description="模式 B 本次允许调用的后端工具白名单（不传则用技能/Agent 默认集合）",
    )

    # ── 异步选项（仅 /tasks 生效） ──
    callback_url: str | None = Field(
        default=None,
        description="任务终态时回调此 URL（仅内网域名白名单，不做签名）",
    )
    priority: str | None = Field(
        default="normal",
        description="low | normal | high（一期预留，暂不差异化调度）",
    )

    # ── 治理 ──
    timeout_seconds: int | None = Field(
        default=None,
        description="同步超时；异步为整体任务时限",
        ge=1, le=3600,
    )
    idempotency_key: str | None = Field(
        default=None,
        description="幂等键，TTL 内重复提交返回同一任务",
    )
    trace_id: str | None = Field(default=None, description="调用方传入的 trace_id，不传自动生成")

    @field_validator("scene")
    @classmethod
    def _scene_upper(cls, v: str | None) -> str | None:
        if v and v != v.upper():
            return v.upper()
        return v

    @field_validator("response_format")
    @classmethod
    def _response_format_values(cls, v: str | None) -> str | None:
        if v and v not in ("text", "json"):
            raise ValueError("response_format 只能是 text 或 json")
        return v

    @field_validator("priority")
    @classmethod
    def _priority_values(cls, v: str | None) -> str | None:
        if v and v not in ("low", "normal", "high"):
            raise ValueError("priority 只能是 low / normal / high")
        return v


# ════════════════════════════════════════════════════════════════
# 同步 invoke 响应（M1）
# ════════════════════════════════════════════════════════════════


class InvokeResponse(BaseModel):
    """POST /agent/invoke 的统一响应体（设计文档 7.1）。"""
    thread_id: str
    task_id: str
    mode: str = Field(description="orchestrated（所有请求统一走 Planner 编排）")
    scene: str | None = None
    output: str | None = None
    structured: dict | list | None = None
    plan: list[dict] | None = None
    interrupt: dict | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    trace_id: str


# ════════════════════════════════════════════════════════════════
# 异步任务相关模型（M2，设计文档 7.2-7.5）
# ════════════════════════════════════════════════════════════════


class SubmitTaskResponse(BaseModel):
    """POST /agent/tasks 成功响应（202，设计文档 7.2）。"""
    task_id: str
    thread_id: str
    status: TaskStatus = TaskStatus.PENDING


class InterruptPayload(BaseModel):
    """人机中断载荷（设计文档 7.3 waiting_human 时的 interrupt 字段）。"""
    type: str = Field(description="ask | confirm")
    step_id: str | None = None
    tool: str | None = None
    skill: str | None = None
    message: str | None = Field(default=None, description="给人工的提示文本")


class ProgressPayload(BaseModel):
    """任务进度（Planner 模式回显当前步骤）。"""
    step_index: int | None = None
    step_total: int | None = None
    current_skill: str | None = None


class TaskDetailResponse(BaseModel):
    """GET /agent/tasks/{task_id} 响应（设计文档 7.3）。

    字段对齐设计文档，无论何种状态都返回同一结构，调用方按 status 分支处理。
    """
    task_id: str
    thread_id: str
    status: TaskStatus
    mode: str | None = Field(default=None, description="orchestrated")
    scene: str | None = None
    progress: ProgressPayload | None = None
    plan: list[dict] | None = None
    output: str | None = None
    structured: dict | list | None = None
    interrupt: InterruptPayload | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    usage: dict[str, Any] = Field(default_factory=dict)
    trace_id: str


class ResumeRequest(BaseModel):
    """POST /agent/tasks/{task_id}/resume 请求体（设计文档 7.4）。"""
    reply: str = Field(description="人工回复文本，confirm 通常是 yes/no；ask 是补充信息")


class CancelResponse(BaseModel):
    """POST /agent/tasks/{task_id}/cancel 响应（设计文档 7.5）。"""
    task_id: str
    status: TaskStatus
    message: str | None = None


# ════════════════════════════════════════════════════════════════
# capabilities 响应（M1）
# ════════════════════════════════════════════════════════════════


class CapabilityScene(BaseModel):
    """capabilities 接口返回的场景码条目（对外不含 agent/skill 内部名）。"""
    scene: str
    description: str
    response_format: str
    timeout_seconds: int


class CapabilitiesResponse(BaseModel):
    """GET /agent/capabilities 响应。"""
    agents: dict[str, Any] = Field(description="按 agent 名组织的技能清单（含 inputs/output/risk_level）")
    scenes: list[CapabilityScene] = Field(description="场景码清单，供后台渲染按钮")


# ════════════════════════════════════════════════════════════════
# 统一错误响应（M1）
# ════════════════════════════════════════════════════════════════


class ErrorPayload(BaseModel):
    """统一错误响应体。"""
    code: str
    message: str
    task_id: str | None = None
    trace_id: str | None = None
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorPayload
