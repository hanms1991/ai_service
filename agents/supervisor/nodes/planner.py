"""Plan 子图模块：Plan 生成 + Schema 定义。

本文件是 Plan 子图的唯一事实来源：
- 改 PlanStep / PlanSchema 字段、加校验、加修复重试 → 只动本文件
- 改 Planner 提示词文案 → 只改 prompts/planner_prompt.yaml
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, field_validator

from agents.supervisor import constants
from agents.supervisor.state import SupervisorState
from core.llm import model


# ════════════════════════════════════════════════════════════════
# 1. Schema 定义（Plan 子图专用）
# ════════════════════════════════════════════════════════════════

class PlanStep(BaseModel):
    """执行计划中的单个步骤。"""
    step_id: str = Field(description="步骤唯一标识，如 step_1")
    tool: str = Field(
        default="",
        description=(
            "要调用的 worker agent 名称，必须取自能力注册表；"
            "闲聊/通用问答等中枢自行处理的步骤留空（tool=''）；"
            "compose 等不调用 worker 的步骤也留空"
        ),
    )
    skill: str = Field(
        default="",
        description=(
            "要执行的技能名，必须取自该 tool 在能力注册表中的技能列表；"
            "Executor 据此一步直达，无需 worker 内部再选技能；"
            "compose/ask/confirm/闲聊 等不执行业务技能的步骤留空"
        ),
    )
    inputs: dict[str, str] | None = Field(
        default=None,
        description=(
            "技能输入参数，键名和取值必须符合技能 inputs 契约；"
            "值中可用 ${output_key} 引用前序步骤输出；必填项缺失时应改用 ask 模式"
        ),
    )
    input: str = Field(
        default="",
        description="未指定 skill 时的自由文本任务输入，可用 ${output_key} 引用前序步骤输出",
    )
    output_key: str = Field(description="本步骤结果的存储键名")
    is_final: bool = Field(default=False, description="是否为最终输出步骤")
    mode: str = Field(
        default="single",
        description=(
            "single: 单步执行 | "
            "chain: 链式，前步输出作后步输入 | "
            "compose: 组合多个结果为最终输出（不调worker，文本拼装）| "
            "ask: 暂停等待用户补充信息 | "
            "confirm: 高风险，暂停等待人工确认"
        ),
    )

    # LLM 常输出 null 表示"无"，Pydantic 默认拒绝 null → 强制转为空串
    @field_validator("tool", "skill", mode="before")
    @classmethod
    def _coerce_none_to_empty(cls, v: Any) -> str:
        return v if isinstance(v, str) else ""


class PlanSchema(BaseModel):
    """LLM 输出的完整执行计划（用于 with_structured_output）。"""
    steps: list[PlanStep] = Field(description="按顺序执行的步骤列表")


# ════════════════════════════════════════════════════════════════
# 2. 提示词加载（YAML 缓存）
# ════════════════════════════════════════════════════════════════

# 包根：agents/supervisor/（本文件在 agents/supervisor/nodes/ 下，回退两级）
_PKG_ROOT = Path(__file__).resolve().parent.parent
_PROMPTS_DIR = _PKG_ROOT / "prompts"

# 模块级缓存：首次访问后 yaml 不再读盘（与 capability_registry 的缓存风格一致）
_TEMPLATE_CACHE: dict[str, str] = {}


def _load_planner_template() -> str:
    """加载 Planner system prompt 模板（带模块级缓存）。"""
    if "planner" not in _TEMPLATE_CACHE:
        path = _PROMPTS_DIR / "planner_prompt.yaml"
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        template = data.get("template")
        if not isinstance(template, str) or not template.strip():
            raise ValueError(
                f"Planner 提示词配置 {path} 缺少 template 字段或为空"
            )
        _TEMPLATE_CACHE["planner"] = template
    return _TEMPLATE_CACHE["planner"]


def _build_planner_prompt(extra_instructions: str = "") -> str:
    """根据能力注册表动态生成 planner 的系统提示（注册表是唯一事实来源）。

    模板占位符：
      {catalog}            ← render_planner_catalog() 渲染的可用能力目录
      {extra_instructions} ← supervisor YAML 的 system_prompt 字段

    用 str.replace 替换（不用 .format，避免 JSON 示例里的裸 { 触发 KeyError）。
    """
    from agents.capability_registry import render_planner_catalog

    catalog = render_planner_catalog()
    template = _load_planner_template()
    return (
        template
        .replace("{catalog}", catalog)
        .replace("{extra_instructions}", extra_instructions)
    )


# ════════════════════════════════════════════════════════════════
# 3. Planner 节点
# ════════════════════════════════════════════════════════════════

def planner_node(state: SupervisorState) -> dict:
    """Planner 节点：LLM 只输出 JSON 执行计划，不产出业务内容。

    带 Schema 修复重试：LLM 输出格式不合法时，把错误信息追加为
    HumanMessage 提示 LLM 修正（最多重试 2 次）。最终仍失败则
    fallback 到 self_handle 单步计划，由中枢直接回答用户。
    """
    # 用 json_mode（response_format=json_object），兼容不支持 json_schema 的端点（如 DeepSeek）
    # 提示词已强约束输出格式，Pydantic 做二次校验
    structured_model = model.with_structured_output(
        PlanSchema, method="json_mode"
    )

    # 通过模块对象访问 _EXTRA_INSTRUCTIONS，拿到 registry_init 写入的最新值
    prompt = _build_planner_prompt(constants._EXTRA_INSTRUCTIONS)
    messages = [SystemMessage(content=prompt)] + state["messages"]

    plan: PlanSchema | None = None
    max_retries = 2  # 首次 + 2 次修复重试

    for attempt in range(max_retries + 1):
        try:
            plan = structured_model.invoke(messages)
            break
        except Exception as e:
            if attempt < max_retries:
                # 修复重试：把错误信息加入 messages 提示 LLM 修正
                print(f"[planner] 第 {attempt + 1} 次输出校验失败：{type(e).__name__}: {e}，正在重试...")
                messages = messages + [
                    HumanMessage(
                        content=(
                            f"上一次输出格式有误：{type(e).__name__}: {e}。"
                            "请重新输出严格符合格式的 JSON 计划，不要附加任何解释。"
                        )
                    )
                ]
            else:
                # 最终 fallback：生成 self_handle 单步计划
                print(f"[planner] 重试 {max_retries} 次后仍失败，fallback 到 self_handle")
                user_msg = state["messages"][-1].content if state["messages"] else ""
                plan = PlanSchema(steps=[
                    PlanStep(
                        step_id="step_1",
                        tool="",
                        skill="",
                        input=user_msg,
                        output_key="result_1",
                        is_final=True,
                        mode="single",
                    )
                ])

    return {
        "plan": [step.model_dump() for step in plan.steps],
        "step_index": 0,
        "results": {},
        "final_output": None,
        "needs_human": False,
    }
