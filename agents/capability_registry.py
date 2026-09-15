"""能力注册表（Capability Registry）—— 平台能力的唯一事实来源。

解决的问题：
  Planner 无法依赖 worker YAML 里一句手写摘要去推断其真实能力。
  本模块扫描：
    - agents/configs/*.yaml   （Agent 声明：描述、拥有的技能）
    - skills/*.yaml           （技能契约：输入 schema、输出形态、prompt 模板）
  把二者按 Agent.skills 绑定关系组装成注册表，用于：
    1. 动态生成 Planner 的系统提示词（不再手写技能描述）；
    2. Planner 输出计划时直接给出 tool + skill，Executor 一步直达，
       省掉 worker 内部“选技能”的额外 LLM 往返；
    3. 启动期校验：技能文件缺失、name 不一致、必填输入缺失等配置错误提前暴露。

命令行（在项目根目录下）：
    python -m agents.capability_registry            # 打印人类可读的能力目录
    python -m agents.capability_registry --json     # 打印 JSON 形式的注册表
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIGS_DIR = Path(__file__).resolve().parent / "configs"
SKILLS_DIR = PROJECT_ROOT / "skills"

# ---------- 缓存 ----------
_agent_cfg_cache: dict[str, dict] = {}
_skill_cfg_cache: dict[str, dict] = {}

# 当前 supervisor 构建出的注册表（供 planner/executor 共享）
_REGISTRY: dict[str, Any] = {"agents": {}}


# ════════════════════════════════════════════════════════════════
# 1. 加载与校验
# ════════════════════════════════════════════════════════════════

def _read_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _validate_skill(cfg: dict, path: Path) -> None:
    """校验技能 yaml 的必填字段与命名一致性。"""
    required = ["name", "display_name", "description", "inputs", "prompt_template"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise ValueError(f"技能配置 {path} 缺少必填字段: {', '.join(missing)}")
    if cfg["name"] != path.stem:
        raise ValueError(
            f"技能配置 {path} 的 name={cfg['name']!r} 与文件名 {path.stem!r} 不一致"
        )
    if not isinstance(cfg["inputs"], dict):
        raise ValueError(f"技能 {cfg['name']} 的 inputs 必须是映射表")
    cfg.setdefault("version", "0.0.0")
    cfg.setdefault("risk_level", "low")
    cfg.setdefault("output", {"format": "plain_text"})


def load_skill_config(skill_name: str, refresh: bool = False) -> dict:
    """按名字加载技能配置（skills/<skill_name>.yaml），带缓存。"""
    if not refresh and skill_name in _skill_cfg_cache:
        return _skill_cfg_cache[skill_name]

    path = SKILLS_DIR / f"{skill_name}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"技能 {skill_name!r} 的配置不存在：{path}。请在 skills/ 下新增 {skill_name}.yaml"
        )
    cfg = _read_yaml(path)
    _validate_skill(cfg, path)
    cfg["_source"] = str(path.relative_to(PROJECT_ROOT))
    _skill_cfg_cache[skill_name] = cfg
    return cfg


def load_agent_config(agent_name: str, refresh: bool = False) -> dict:
    """按名字加载 Agent 原始配置（agents/configs/<agent_name>.yaml），带缓存。"""
    if not refresh and agent_name in _agent_cfg_cache:
        return _agent_cfg_cache[agent_name]

    path = CONFIGS_DIR / f"{agent_name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Agent 配置不存在：{path}")
    cfg = _read_yaml(path)
    if cfg.get("name") and cfg["name"] != agent_name:
        raise ValueError(
            f"Agent 配置 {path} 的 name={cfg['name']!r} 与文件名 {agent_name!r} 不一致"
        )
    cfg["_source"] = str(path.relative_to(PROJECT_ROOT))
    _agent_cfg_cache[agent_name] = cfg
    return cfg


# ════════════════════════════════════════════════════════════════
# 2. 组装注册表
# ════════════════════════════════════════════════════════════════

def list_worker_agents() -> list[str]:
    """列出 configs 下所有 type != supervisor 的 Agent 名。"""
    names = []
    for path in sorted(CONFIGS_DIR.glob("*.y*ml")):
        cfg = _read_yaml(path)
        if cfg.get("type", "worker") != "supervisor":
            names.append(cfg.get("name") or path.stem)
    return names


def build_capability_registry(
    member_agents: list[str] | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """把 Agent 配置与其技能契约绑定，组装成完整能力注册表。

    Args:
        member_agents: 要纳入注册表的 Agent 名；None 表示全部 worker。
        refresh:       是否重新读盘（配置热更新用）。

    Returns:
        {
          "agents": {
            "<agent_name>": {
              "description": ..., "domain": ..., "tags": ...,
              "skills": {
                "<skill_name>": { ...技能契约... }
              }
            }
          }
        }
    """
    if member_agents is None:
        member_agents = list_worker_agents()

    registry: dict[str, Any] = {"agents": {}}
    bound: set[str] = set()

    for agent_name in member_agents:
        agent_cfg = load_agent_config(agent_name, refresh=refresh)
        skill_names = agent_cfg.get("skills") or []
        if not isinstance(skill_names, list):
            raise ValueError(f"Agent {agent_name} 的 skills 字段必须是列表")

        skills_entry: dict[str, dict] = {}
        for skill_name in skill_names:
            skill_cfg = load_skill_config(skill_name, refresh=refresh)
            skills_entry[skill_name] = skill_cfg
            bound.add(skill_name)

        registry["agents"][agent_name] = {
            "description": agent_cfg.get("description", ""),
            "domain": agent_cfg.get("domain", []),
            "tags": agent_cfg.get("tags", []),
            "skills": skills_entry,
        }

    # 孤儿技能检查：存在 yaml 但没有任何 Agent 声明 → 提示（不阻断，便于先写技能后挂接）
    all_skills = {p.stem for p in SKILLS_DIR.glob("*.y*ml")}
    registry["_orphan_skills"] = sorted(all_skills - bound)
    return registry


def init_registry_from_supervisor(config: dict) -> dict[str, Any]:
    """由 supervisor YAML 的 tools 成员清单初始化全局注册表。

    tools 支持两种写法：
        tools:
          - product_agent
          - { agent: product_agent }   # 可扩展风险覆盖等部署策略
    """
    members: list[str] = []
    for spec in config.get("tools", []) or []:
        if isinstance(spec, str):
            members.append(spec)
        elif isinstance(spec, dict) and "agent" in spec:
            members.append(spec["agent"])
    if not members:
        members = list_worker_agents()

    global _REGISTRY
    _REGISTRY = build_capability_registry(members, refresh=True)
    return _REGISTRY


def get_registry() -> dict[str, Any]:
    """获取当前全局注册表。"""
    return _REGISTRY


# ════════════════════════════════════════════════════════════════
# 3. 绑定校验与模板渲染
# ════════════════════════════════════════════════════════════════

def validate_binding(agent_name: str, skill_name: str) -> dict:
    """校验某 Agent 是否确实拥有某技能，返回技能配置。"""
    agents = _REGISTRY.get("agents", {})
    if agent_name not in agents:
        raise KeyError(
            f"计划引用了未注册的 Agent {agent_name!r}，可用：{', '.join(agents) or '（空）'}"
        )
    skills = agents[agent_name]["skills"]
    if skill_name not in skills:
        raise KeyError(
            f"Agent {agent_name!r} 未绑定技能 {skill_name!r}，"
            f"可用技能：{', '.join(skills) or '（空）'}"
        )
    return skills[skill_name]


_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render_prompt_template(
    template: str,
    inputs: dict[str, Any],
    input_schema: dict[str, dict],
) -> str:
    """用实际输入渲染技能模板（{{param}} 占位符）。

    - 必填输入缺失 → ValueError（Planner 应改用 ask 模式补齐）
    - 可选输入缺失 → 填充“（未提供）”
    - 模板中出现 schema 未声明的占位符 → ValueError
    """
    declared = set(input_schema.keys())
    used = set(_PLACEHOLDER_RE.findall(template))
    unknown = used - declared
    if unknown:
        raise ValueError(
            f"模板引用了 inputs 中未声明的参数: {', '.join(sorted(unknown))}"
        )

    missing_required = [
        name
        for name, spec in input_schema.items()
        if spec.get("required") and not inputs.get(name)
    ]
    if missing_required:
        raise ValueError(
            f"缺少必填输入: {', '.join(missing_required)}；"
            f"若用户信息不足，Planner 应使用 ask 模式先追问"
        )

    def _replacer(match: re.Match) -> str:
        name = match.group(1)
        value = inputs.get(name)
        return str(value) if value else "（未提供）"

    return _PLACEHOLDER_RE.sub(_replacer, template)


# ════════════════════════════════════════════════════════════════
# 4. Planner 目录（动态生成 system prompt 中的能力部分）
# ════════════════════════════════════════════════════════════════

def _format_inputs(input_schema: dict[str, dict]) -> str:
    parts = []
    for name, spec in input_schema.items():
        req = "必填" if spec.get("required") else "可选"
        parts.append(f"{name}: {spec.get('type', 'string')}（{req}，{spec.get('desc', '')}）")
    return "; ".join(parts) or "（无）"


def render_planner_catalog(registry: dict[str, Any] | None = None) -> str:
    """把注册表渲染成 Planner 系统提示词中的「可用能力目录」段落。"""
    registry = registry or _REGISTRY
    blocks: list[str] = []
    for agent_name, info in registry.get("agents", {}).items():
        lines = [f"### {agent_name}", f"定位：{info.get('description') or '（未填写 description）'}"]
        if info.get("skills"):
            lines.append("技能（tool 必须与 skill 配对，且 skill 必须取自本列表）：")
            for skill_name, skill in info["skills"].items():
                output = skill.get("output", {})
                out_desc = output.get("format", "plain_text")
                sections = output.get("sections")
                if sections:
                    out_desc += f"，含章节【{'/'.join(sections)}】"
                lines.append(
                    f"  - skill: {skill_name}"
                    f" ｜ {skill.get('display_name')}"
                    f" ｜ 能力: {skill.get('description')}"
                    f" ｜ 输入: {_format_inputs(skill.get('inputs', {}))}"
                    f" ｜ 输出: {out_desc}"
                    f" ｜ 风险: {skill.get('risk_level', 'low')}"
                )
        else:
            lines.append("技能：（该 Agent 未声明任何技能）")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def registry_as_jsonable(registry: dict[str, Any] | None = None) -> dict:
    """导出可 JSON 序列化的注册表（去掉内部路径以外的运行时对象）。"""
    registry = registry or _REGISTRY
    return yaml.safe_load(yaml.safe_dump(registry, allow_unicode=True))


# ════════════════════════════════════════════════════════════════
# 5. 技能直连执行（tool + skill 一步直达，无需 worker 内部选技能）
# ════════════════════════════════════════════════════════════════

# model_hint.reasoning → 实际 API 参数的映射。
# 所有参数统一通过 extra_body 透传：openai SDK 会把 extra_body 合并进请求 body，
# 且不会对其做签名校验，因此 enable_thinking（DeepSeek 原生）与 reasoning_effort
# （OpenAI 标准）都能兼容，避免 "unexpected keyword argument" 报错。
_REASONING_TO_MODEL_KWARGS: dict[str, dict[str, Any]] = {
    "disabled": {"enable_thinking": False},
    "low": {"reasoning_effort": "low"},
    "medium": {"reasoning_effort": "medium"},
    "high": {"reasoning_effort": "high"},
}


def _build_model_with_hint(base_llm: Any, model_hint: dict[str, Any] | None) -> Any:
    """根据 skill.model_hint 调整模型参数（推理档位、温度等），返回新的可调用对象。

    支持：
      - reasoning: disabled | low | medium | high   → 控制推理深度/开关
      - model_kwargs: { ... }                        → 透传任意原生参数（最高优先级）
    未声明 model_hint 时直接返回 base_llm，不做额外绑定。
    """
    if not model_hint:
        return base_llm

    model_kwargs: dict[str, Any] = {}

    reasoning = model_hint.get("reasoning")
    if reasoning:
        mapped = _REASONING_TO_MODEL_KWARGS.get(str(reasoning).lower())
        if mapped:
            model_kwargs.update(mapped)

    # 允许 skill 直接声明原生 model_kwargs（覆盖 reasoning 的映射）
    raw_kwargs = model_hint.get("model_kwargs")
    if isinstance(raw_kwargs, dict):
        model_kwargs.update(raw_kwargs)

    if not model_kwargs:
        return base_llm

    # 统一走 extra_body 透传，兼容各推理模型的非标准参数
    return base_llm.bind(extra_body=model_kwargs)


def execute_skill(
    agent_name: str,
    skill_name: str,
    inputs: dict[str, Any],
    runnable_config: Any = None,
) -> str:
    """直接用某 Agent 的模型执行指定技能，只产生一次 LLM 调用。

    system_prompt 优先级（避免 Agent 与 Skill 的约束冲突误导模型）：
      - skill.system_prompt       （角色 + 输出格式，优先）
      - agent.default_system_prompt（身份 + 安全边界，仅兜底）

    model_hint：skill 可声明 reasoning 档位（disabled/low/medium/high）或原生
      model_kwargs，调度层据此调整本次调用的模型行为（如简单技能关闭推理）。

    用户消息 = 技能 prompt_template 用 inputs 渲染后的内容。

    Args:
        runnable_config: LangChain RunnableConfig（透传 callbacks，使日志记录生效）
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from agents.generate_agent import _resolve_model, _resolve_system_prompt

    skill_cfg = validate_binding(agent_name, skill_name)
    agent_cfg = load_agent_config(agent_name)

    rendered = render_prompt_template(
        skill_cfg["prompt_template"],
        inputs or {},
        skill_cfg.get("inputs", {}),
    )
    # 基于 Agent 的模型配置，叠加 skill 的 model_hint（推理开关/档位等）
    llm = _build_model_with_hint(
        _resolve_model(agent_cfg.get("model")),
        skill_cfg.get("model_hint"),
    )

    # ── system_prompt 覆盖策略：skill 优先，agent 兜底 ──
    skill_system = skill_cfg.get("system_prompt")
    if skill_system:
        system_prompt = _resolve_system_prompt(skill_system, SKILLS_DIR)
    else:
        system_prompt = _resolve_system_prompt(
            agent_cfg.get("default_system_prompt", ""), CONFIGS_DIR
        )

    invoke_kwargs = {}
    if runnable_config is not None:
        invoke_kwargs["config"] = runnable_config
    response = llm.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=rendered)],
        **invoke_kwargs,
    )
    return response.content


# ════════════════════════════════════════════════════════════════
# 6. 命令行入口
# ════════════════════════════════════════════════════════════════

def _main() -> None:
    parser = argparse.ArgumentParser(description="能力注册表工具")
    parser.add_argument("--json", action="store_true", help="输出 JSON 形式注册表")
    parser.add_argument("--member", nargs="*", default=None, help="只纳入指定 Agent")
    args = parser.parse_args()

    registry = build_capability_registry(args.member, refresh=True)

    if args.json:
        print(yaml.safe_dump(registry_as_jsonable(registry), allow_unicode=True, sort_keys=False))
        return

    print("=" * 70)
    print("能力注册表（唯一事实来源）—— 以下内容即注入 Planner 系统提示词的目录")
    print("=" * 70)
    print(render_planner_catalog(registry))
    orphans = registry.get("_orphan_skills", [])
    if orphans:
        print("\n[提示] 以下技能尚未被任何 Agent 绑定：" + ", ".join(orphans))
    print("=" * 70)


if __name__ == "__main__":
    _main()
