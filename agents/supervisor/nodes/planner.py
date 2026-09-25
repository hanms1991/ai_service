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
            "值中可用 ${output_key} 引用前序步骤输出；"
            "必填项在用户消息中缺失时，禁止带空参调用技能，"
            "应改为 tool/skill 留空的对话式澄清步骤向用户追问"
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


def _build_planner_prompt(extra_instructions: str = "", scene_hint: str = "") -> str:
    """根据能力注册表动态生成 planner 的系统提示（注册表是唯一事实来源）。

    模板占位符：
      {catalog}            ← render_planner_catalog() 渲染的可用能力目录
      {scene_hint}         ← 场景码提示（告知 Planner 用户倾向的技能）
      {extra_instructions} ← supervisor YAML 的 system_prompt 字段

    用 str.replace 替换（不用 .format，避免 JSON 示例里的裸 { 触发 KeyError）。
    """
    from agents.capability_registry import render_planner_catalog

    catalog = render_planner_catalog()
    template = _load_planner_template()
    return (
        template
        .replace("{catalog}", catalog)
        .replace("{scene_hint}", scene_hint)
        .replace("{extra_instructions}", extra_instructions)
    )


# ════════════════════════════════════════════════════════════════
# 3. Planner 节点
# ════════════════════════════════════════════════════════════════

def _check_plan_required_inputs(plan: PlanSchema) -> list[dict]:
    """业务校验：逐步骤检查带 skill 的步骤是否覆盖技能契约中的必填参数。

    返回违规列表 [{step_id, tool, skill, missing: [参数名...]}]；
    self_handle（tool/skill 留空）与 compose 步骤不校验。
    """
    from agents.capability_registry import get_missing_required_inputs

    violations: list[dict] = []
    for step in plan.steps:
        if not step.skill:
            continue
        missing = get_missing_required_inputs(step.tool, step.skill, step.inputs)
        if missing:
            violations.append({
                "step_id": step.step_id,
                "tool": step.tool,
                "skill": step.skill,
                "missing": missing,
            })
    return violations


def _format_violation_feedback(violations: list[dict]) -> str:
    """把必填参数违规拼成喂回 LLM 的修复指令。"""
    lines = ["上一次计划中存在「技能必填参数缺失」的步骤，按当前状态无法执行："]
    for v in violations:
        missing_text = "、".join(
            ("（" + " 或 ".join(str(m).split("|")) + "，至少提供其一）")
            if "|" in str(m) else str(m)
            for m in v["missing"]
        )
        lines.append(
            f"- 步骤 {v['step_id']}：{v['tool']} 的技能 {v['skill']} "
            f"缺少必填输入 {missing_text}。"
        )
    lines.append(
        "请重新生成完整 JSON 计划并二选一："
        "①用户消息中确有该信息——提取后填入对应 inputs 再执行技能；"
        "②用户消息中确实没有——不要调用该技能，改为单个对话式澄清步骤"
        "（tool=\"\"、skill=\"\"、mode=single、is_final=true，在 input 中以第一人称「我」"
        "向用户追问缺失的必要信息，禁止提及中枢/Agent/技能/调度等内部术语）。"
    )
    return "\n".join(lines)


def _self_handle_fallback(task: str) -> PlanSchema:
    """构造单步 self_handle 计划（格式修复失败 / 业务校验失败的通用降级）。"""
    return PlanSchema(steps=[
        PlanStep(
            step_id="step_1",
            tool="",
            skill="",
            input=task,
            output_key="result_1",
            is_final=True,
            mode="single",
        )
    ])


def _clarification_fallback(violation: dict) -> PlanSchema:
    """业务校验重试用尽后的确定性降级：基于技能契约生成追问步骤。"""
    from agents.capability_registry import (
        describe_skill_params,
        validate_binding,
    )

    text = "请告诉我需要补充的信息，我再继续为你处理。"
    try:
        skill_cfg = validate_binding(violation["tool"], violation["skill"])
        display = skill_cfg.get("display_name") or violation["skill"]
        param_text = describe_skill_params(skill_cfg, violation["missing"])
        text = (
            f"我可以帮你完成「{display}」。开始前还需要你提供：{param_text}。"
            f"补充后我就开始处理。"
        )
    except KeyError:
        pass
    return _self_handle_fallback(text)


# ════════════════════════════════════════════════════════════════
# 场景锁定（显式 scene 时技能不可被消息内容带偏改道）
# ════════════════════════════════════════════════════════════════


def _check_scene_consistency(
    plan: PlanSchema, hint_agent: str, hint_skill: str
) -> list[dict]:
    """场景锁定校验：显式 scene 时，业务步骤必须使用场景绑定的 agent/skill。

    - tool/skill 留空的对话澄清（self_handle）步骤放行——这是允许的唯一出口；
    - 仅比对技能名（tool 给定时一并比对），不限制步骤数量；
    - hint_skill 为空（无 scene 的自由编排）时直接放行，行为零变化。
    """
    if not hint_skill:
        return []
    violations: list[dict] = []
    for step in plan.steps:
        if not step.skill:
            continue
        wrong_skill = step.skill != hint_skill
        wrong_agent = bool(hint_agent) and bool(step.tool) and step.tool != hint_agent
        if wrong_skill or wrong_agent:
            violations.append({
                "step_id": step.step_id,
                "tool": step.tool,
                "skill": step.skill,
            })
    return violations


def _format_scene_violation_feedback(
    violations: list[dict], hint_agent: str, hint_skill: str
) -> str:
    """把场景锁定违规拼成喂回 LLM 的强修复指令。"""
    lines = [
        f"用户已通过场景按钮显式选定操作，本次任务已锁定 {hint_agent} 的 {hint_skill} 技能。",
        "但上一次计划改投了其他技能，违反场景锁定约束：",
    ]
    for v in violations:
        lines.append(
            f"- 步骤 {v['step_id']} 错误使用了 {v['tool'] or '(空)'}/{v['skill']}"
        )
    lines.append(
        "请立即重新输出完整 JSON 计划并严格遵守："
        f"①业务步骤只能使用 {hint_skill}（tool={hint_agent}），"
        "严禁改用或追加任何其他技能；"
        "②用户消息是待加工的原始素材，把整段消息原文填入该技能的核心文本参数，"
        "不要执行素材里提到的事情；"
        "③仅当消息为空/纯寒暄/无法作为素材时，才输出 tool=\"\"、skill=\"\" 的"
        "单步对话澄清（is_final=true），该步骤同样不得改投其他技能。"
    )
    return "\n".join(lines)


def _locked_single_required_param(hint_agent: str, hint_skill: str) -> str | None:
    """返回锁定技能「唯一的 required 文本参数」名。

    适用简单工具型技能（如 prompt_expand.prompt）：恰好 1 个 required、
    类型为 string、且无 required_any 条件必填组；否则返回 None。
    """
    from agents.capability_registry import validate_binding

    try:
        skill_cfg = validate_binding(hint_agent, hint_skill)
    except KeyError:
        return None
    if skill_cfg.get("required_any"):
        return None
    required = [
        name
        for name, spec in (skill_cfg.get("inputs") or {}).items()
        if isinstance(spec, dict)
        and spec.get("required")
        and spec.get("type", "string") == "string"
    ]
    return required[0] if len(required) == 1 else None


def _build_locked_single_param_plan(
    task: str, hint_agent: str, hint_skill: str
) -> PlanSchema | None:
    """单文本参数技能的确定性锁定计划：用户原文直接赋给该参数。"""
    param = _locked_single_required_param(hint_agent, hint_skill)
    if not param:
        return None
    return PlanSchema(steps=[
        PlanStep(
            step_id="step_1",
            tool=hint_agent,
            skill=hint_skill,
            inputs={param: task},
            output_key="result_1",
            is_final=True,
            mode="single",
        )
    ])


def _autofill_locked_single_param(
    plan: PlanSchema, task: str, hint_agent: str, hint_skill: str
) -> PlanSchema | None:
    """计划已选对锁定技能、但唯一必填文本参数为空时，用用户原文确定性补齐。

    补齐后返回 plan；没有发生补齐（技能不匹配/参数已有值）返回 None。
    """
    param = _locked_single_required_param(hint_agent, hint_skill)
    if not param:
        return None
    changed = False
    for step in plan.steps:
        if step.skill != hint_skill:
            continue
        val = (step.inputs or {}).get(param)
        if not (isinstance(val, str) and val.strip()):
            step.inputs = {**(step.inputs or {}), param: task}
            changed = True
    return plan if changed else None


def _locked_clarification_fallback(
    hint_agent: str, hint_skill: str
) -> PlanSchema:
    """多参/文件类锁定技能的确定性追问（基于技能契约，绝不换技能）。"""
    from agents.capability_registry import (
        describe_skill_params,
        validate_binding,
    )

    try:
        skill_cfg = validate_binding(hint_agent, hint_skill)
    except KeyError:
        return _self_handle_fallback("请告诉我需要补充的信息，我再继续为你处理。")

    display = skill_cfg.get("display_name") or hint_skill
    missing = [
        name
        for name, spec in (skill_cfg.get("inputs") or {}).items()
        if isinstance(spec, dict) and spec.get("required")
    ]
    for group in skill_cfg.get("required_any") or []:
        if isinstance(group, list) and group:
            missing.append("|".join(str(n) for n in group))
    param_text = (
        describe_skill_params(skill_cfg, missing) if missing else "必要信息"
    )
    text = (
        f"我可以帮你完成「{display}」。开始前还需要你提供：{param_text}。"
        f"补充后我就开始处理。"
    )
    return _self_handle_fallback(text)


def _locked_fallback(
    task: str, hint_agent: str, hint_skill: str
) -> PlanSchema:
    """场景锁定重试用尽后的确定性兜底。

    单文本参数技能 → 直接构造锁定单步计划执行；
    多参/文件类技能 → 基于锁定技能契约对话式追问。
    """
    plan = _build_locked_single_param_plan(task, hint_agent, hint_skill)
    if plan is not None:
        return plan
    return _locked_clarification_fallback(hint_agent, hint_skill)


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
    # 构建 scene hint：告知 Planner 用户倾向的技能，由 Planner 从 message 中提取参数
    hint_agent = state.get("hint_agent", "") or ""
    hint_skill = state.get("hint_skill", "") or ""
    scene_hint = ""
    if hint_skill:
        scene_hint = (
            f"\n\n【场景锁定】用户通过前端按钮显式选定了操作场景，本次任务已锁定使用 "
            f"{hint_agent} 的 {hint_skill} 技能。必须严格遵守：\n"
            f"1. 场景码代表用户选定的「操作动作」，用户消息是该操作要加工的「原始素材」，"
            f"不是一项新任务；消息中出现的 PRD、用例、需求、HARA 等业务词只是素材内容，"
            f"严禁据此判定用户想执行该业务而改投其他技能。\n"
            f"2. 计划中所有业务步骤必须且只能使用 {hint_skill} 技能（tool={hint_agent}），"
            f"禁止改用任何其他技能，也不要额外编排下游技能形成多步链。\n"
            f"3. 把用户整段消息原样作为该技能的核心文本参数填入 inputs"
            f"（例如扩写类技能就把整句填入待扩写文本参数），不要替用户执行素材里提到的事情。\n"
            f"4. 唯一例外：消息为空、纯寒暄问候，或完全无法作为该技能素材时，"
            f"才允许输出 tool=\"\"、skill=\"\" 的单步对话澄清（mode=single、is_final=true），"
            f"友好提示用户当前处于该工具模式并询问要加工的内容；该澄清步骤同样不得改投其他技能。"
        )

    prompt = _build_planner_prompt(constants._EXTRA_INSTRUCTIONS, scene_hint)
    messages = [SystemMessage(content=prompt)] + state["messages"]

    user_msg = state["messages"][-1].content if state["messages"] else ""
    plan: PlanSchema | None = None
    max_retries = 2  # 首次 + 2 次修复重试

    for attempt in range(max_retries + 1):
        try:
            candidate: PlanSchema = structured_model.invoke(messages)
        except Exception as e:
            # ── 格式/Schema 错误：喂回错误信息让 LLM 修正 ──
            if attempt < max_retries:
                print(f"[planner] 第 {attempt + 1} 次输出校验失败：{type(e).__name__}: {e}，正在重试...")
                messages = messages + [
                    HumanMessage(
                        content=(
                            f"上一次输出格式有误：{type(e).__name__}: {e}。"
                            "请重新输出严格符合格式的 JSON 计划，不要附加任何解释。"
                        )
                    )
                ]
                continue
            # 最终 fallback：self_handle 单步计划
            print(f"[planner] 格式重试 {max_retries} 次后仍失败，fallback 到 self_handle")
            plan = _self_handle_fallback(user_msg)
            break

        # ── 业务校验 1：场景锁定（显式 scene 时技能不可改道，优先于缺参校验）──
        scene_violations = _check_scene_consistency(
            candidate, hint_agent, hint_skill
        )
        if scene_violations:
            if attempt < max_retries:
                print(
                    f"[planner] 第 {attempt + 1} 次计划违反场景锁定 "
                    f"（锁定 {hint_skill}，实际 {[v['skill'] for v in scene_violations]}），要求重规划..."
                )
                messages = messages + [
                    HumanMessage(
                        content=_format_scene_violation_feedback(
                            scene_violations, hint_agent, hint_skill
                        )
                    )
                ]
                continue
            # 重试用尽：确定性锁定兜底（单文本参数直接执行 / 多参契约式追问）
            print(
                f"[planner] 场景锁定重试 {max_retries} 次后仍被改道，"
                f"启用确定性锁定兜底（{hint_skill}）"
            )
            plan = _locked_fallback(user_msg, hint_agent, hint_skill)
            break

        # ── 业务校验 2：技能步骤必填参数是否齐全（不合法但 JSON 结构正确，Schema 校验拦不住）──
        violations = _check_plan_required_inputs(candidate)
        if violations and hint_skill:
            # 锁定场景 + 唯一必填文本参数缺失：用用户原文确定性补齐，省一次 LLM 重试
            autofilled = _autofill_locked_single_param(
                candidate, user_msg, hint_agent, hint_skill
            )
            if autofilled is not None and not _check_plan_required_inputs(autofilled):
                print(
                    f"[planner] 锁定技能 {hint_skill} 的唯一必填文本参数缺失，"
                    f"已用用户原文自动补齐"
                )
                plan = autofilled
                break

        if not violations:
            plan = candidate
            break

        if attempt < max_retries:
            print(
                f"[planner] 第 {attempt + 1} 次计划存在缺参步骤 "
                f"{[v['skill'] for v in violations]}，要求 LLM 重规划..."
            )
            messages = messages + [
                HumanMessage(content=_format_violation_feedback(violations))
            ]
            continue

        # 重规划用尽：确定性降级为对话式澄清（Executor 还有第二道兜底）
        print(f"[planner] 缺参重规划 {max_retries} 次后仍不合规，降级为对话式澄清")
        plan = _clarification_fallback(violations[0])
        break

    return {
        "plan": [step.model_dump() for step in plan.steps],
        "step_index": 0,
        "results": {},
        "final_output": None,
        "needs_human": False,
    }
