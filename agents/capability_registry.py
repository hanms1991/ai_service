"""能力注册表（Capability Registry）—— 平台能力的唯一事实来源。

解决的问题：
  Planner 无法依赖 worker YAML 里一句手写摘要去推断其真实能力。
  本模块扫描：
    - agents/configs/*.yaml      （Agent 声明：描述、拥有的技能）
    - skills/**/*.yaml           （技能契约：输入 schema、输出形态、prompt 模板；
                                  支持按域分子文件夹，如 skills/requirement/uc_analyze.yaml）
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
import json
import re
from dataclasses import dataclass, field
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


def iter_skill_files() -> list[Path]:
    """递归发现 skills/ 下所有技能 yaml。

    支持按域分子文件夹组织技能，例如：
        skills/prd_generation.yaml
        skills/requirement/uc_analyze.yaml
        skills/functional-safety/hara.yaml
    以下划线开头的文件夹（如 _template）会被排除。
    """
    result: list[Path] = []
    for path in sorted(SKILLS_DIR.rglob("*.y*ml")):
        rel_parts = path.relative_to(SKILLS_DIR).parts
        # rel_parts[:-1] 是所在目录段（文件名本身以 _ 开头不排除）
        if any(part.startswith("_") for part in rel_parts[:-1]):
            continue
        result.append(path)
    return result


def find_skill_path(skill_name: str) -> Path | None:
    """按技能名（文件 stem）在 skills/ 下递归查找配置文件；重名时报错。"""
    matches = [p for p in iter_skill_files() if p.stem == skill_name]
    if not matches:
        return None
    if len(matches) > 1:
        dup = ", ".join(str(p.relative_to(PROJECT_ROOT)) for p in matches)
        raise ValueError(f"技能名 {skill_name!r} 存在多个同名配置文件：{dup}")
    return matches[0]


def _skill_base_dir(skill_cfg: dict) -> Path:
    """技能 yaml 所在目录（用于解析 skill.system_prompt 的相对文件引用）。

    子文件夹中的技能可引用同目录下的 prompt 资源，如 {"file": "prompts/xxx.md"}。
    """
    source = skill_cfg.get("_source")
    if source:
        return (PROJECT_ROOT / source).parent
    return SKILLS_DIR


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
    """按名字加载技能配置（递归查找 skills/**/<skill_name>.yaml），带缓存。"""
    if not refresh and skill_name in _skill_cfg_cache:
        return _skill_cfg_cache[skill_name]

    path = find_skill_path(skill_name)
    if path is None:
        raise FileNotFoundError(
            f"技能 {skill_name!r} 的配置不存在：skills/ 目录下未找到 {skill_name}.yaml"
            f"（支持按域分子文件夹存放，如 skills/requirement/{skill_name}.yaml）"
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
    all_skills = {p.stem for p in iter_skill_files()}
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
            lines.append("技能（skill 可选；选用 skill 时必须取自本列表；省略 skill 表示用该 Agent 的通用对话能力）：")
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
        system_prompt = _resolve_system_prompt(skill_system, _skill_base_dir(skill_cfg))
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
# 5B. 技能执行 v2（对外 API 使用：返回结构化结果 + usage + reference_data 注入）
# ════════════════════════════════════════════════════════════════

# reference_data 注入阈值：50KB（硬阈值，超限直接报错，不做摘要降级）
REFERENCE_DATA_MAX_BYTES = 50 * 1024


@dataclass
class SkillResult:
    """技能执行的统一返回结构（v2）。

    text:       LLM 原始文本输出（结构化技能时为未解析的 JSON 字符串）
    structured: 按 output.schema 校验通过的对象；非结构化技能为 None
    usage:      token 使用统计（从 response.usage_metadata 提取）
    """
    text: str
    structured: dict | list | None = None
    usage: dict[str, Any] = field(default_factory=dict)


def _extract_usage(response: Any) -> dict[str, Any]:
    """从 LLM 响应中提取 token 使用统计（兼容 usage_metadata 与 response_metadata）。"""
    usage_meta = getattr(response, "usage_metadata", None)
    if usage_meta and isinstance(usage_meta, dict):
        return {
            "prompt_tokens": usage_meta.get("input_tokens", usage_meta.get("prompt_tokens", 0)),
            "completion_tokens": usage_meta.get("output_tokens", usage_meta.get("completion_tokens", 0)),
            "total_tokens": usage_meta.get("total_tokens", 0),
        }
    # 兜底：response_metadata.openai_token_usage
    resp_meta = getattr(response, "response_metadata", {}) or {}
    token_usage = resp_meta.get("token_usage") or resp_meta.get("openai_token_usage") or {}
    if token_usage:
        return {
            "prompt_tokens": token_usage.get("prompt_tokens", 0),
            "completion_tokens": token_usage.get("completion_tokens", 0),
            "total_tokens": token_usage.get("total_tokens", 0),
        }
    return {}


def _build_reference_block(reference_data: dict[str, Any] | None) -> str:
    """把 reference_data 渲染成注入 prompt 末尾的只读参考段。

    - None 或空 → 返回空串（不注入）
    - 超过 50KB → ValueError（调用方需捕获转 REFERENCE_DATA_TOO_LARGE）
    """
    if not reference_data:
        return ""

    # 紧凑 JSON 序列化后取字节数（UTF-8 编码下与字符长度的近似估计）
    compact = json.dumps(reference_data, ensure_ascii=False, separators=(",", ":"))
    if len(compact.encode("utf-8")) > REFERENCE_DATA_MAX_BYTES:
        raise ValueError(
            f"reference_data 超过 {REFERENCE_DATA_MAX_BYTES // 1024}KB 阈值，"
            f"当前 {len(compact.encode('utf-8'))} 字节；请后台预取时做裁剪/分页"
        )

    return (
        "\n\n【参考数据（只读资料，仅供你参考，不要原样罗列或照搬其字段名）】\n"
        f"{compact}"
    )


def _validate_structured_output(raw_text: str, schema: dict | None) -> dict | list | None:
    """对结构化技能：用 output.schema 校验 LLM 输出。

    - schema 为 None → 返回 None（非结构化技能）
    - 解析 + 校验失败 → ValueError（由调用方转为 SKILL_OUTPUT_INVALID）
    """
    if not schema:
        return None

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"结构化技能输出无法解析为 JSON：{e}；原始文本前 200 字符：{raw_text[:200]!r}"
        ) from e

    # 一期做轻量校验：type / required 字段存在性
    # （完整 JSON Schema 校验可后续引入 jsonschema 库）
    _lightweight_schema_check(parsed, schema)
    return parsed


def _lightweight_schema_check(instance: Any, schema: dict, path: str = "$") -> None:
    """对 JSON Schema Draft 2020-12 子集做轻量校验。

    覆盖：type / required / properties / items。
    不覆盖：additionalProperties、pattern、format、min/max 等。
    """
    if not isinstance(schema, dict):
        return

    # type 校验
    expected_type = schema.get("type")
    if expected_type:
        type_map = {
            "object": dict, "array": list, "string": str,
            "number": (int, float), "integer": int, "boolean": bool,
        }
        py_type = type_map.get(expected_type)
        if py_type and not isinstance(instance, py_type):
            raise ValueError(
                f"{path} 类型应为 {expected_type}，实际为 {type(instance).__name__}"
            )

    # object：required + properties 递归
    if isinstance(instance, dict):
        required = schema.get("required") or []
        missing = [k for k in required if k not in instance]
        if missing:
            raise ValueError(f"{path} 缺少必填字段：{', '.join(missing)}")
        properties = schema.get("properties") or {}
        for k, v in instance.items():
            if k in properties:
                _lightweight_schema_check(v, properties[k], f"{path}.{k}")

    # array：items 递归（校验每个元素）
    if isinstance(instance, list) and "items" in schema:
        items_schema = schema["items"]
        for i, item in enumerate(instance):
            _lightweight_schema_check(item, items_schema, f"{path}[{i}]")


def execute_skill_v2(
    agent_name: str,
    skill_name: str,
    inputs: dict[str, Any],
    *,
    reference_data: dict[str, Any] | None = None,
    runnable_config: Any = None,
) -> SkillResult:
    """对外 API 使用的技能执行入口（v2）。

    与旧 execute_skill 的区别：
      1. 返回 SkillResult(text, structured, usage)，而非裸字符串；
      2. 支持 output.schema 结构化校验（json_mode + 轻量 schema 校验）；
      3. 支持 reference_data 注入（模式 A，只读上下文追加到用户消息末尾）；
      4. 旧函数保留不动，向 LangGraph Executor 完全兼容。

    Args:
        agent_name:     Agent 名（必须存在于注册表）
        skill_name:     技能名（必须由该 Agent 绑定）
        inputs:         技能输入，键必须符合技能 inputs 契约
        reference_data: 后端预取的业务资料（只读注入，50KB 阈值）
        runnable_config: LangChain RunnableConfig（透传 callbacks，使日志记录生效）

    Raises:
        ValueError: reference_data 超阈值 / 结构化输出未通过校验
        KeyError:   Agent/技能未在注册表绑定
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

    # ── 模式 A：reference_data 注入（只读段追加到 prompt 末尾） ──
    ref_block = _build_reference_block(reference_data)
    user_message = rendered + ref_block

    # ── model_hint：叠加 skill 的推理开关/档位等 ──
    llm = _build_model_with_hint(
        _resolve_model(agent_cfg.get("model")),
        skill_cfg.get("model_hint"),
    )

    # ── system_prompt 覆盖策略：skill 优先，agent 兜底 ──
    skill_system = skill_cfg.get("system_prompt")
    if skill_system:
        system_prompt = _resolve_system_prompt(skill_system, _skill_base_dir(skill_cfg))
    else:
        system_prompt = _resolve_system_prompt(
            agent_cfg.get("default_system_prompt", ""), CONFIGS_DIR
        )

    # ── 结构化技能：json_mode 约束 LLM 输出 ──
    output_cfg = skill_cfg.get("output", {}) or {}
    output_format = output_cfg.get("format", "plain_text")
    output_schema = output_cfg.get("schema")

    invoke_model = llm
    if output_format == "json" and output_schema:
        # json_mode：要求 LLM 输出合法 JSON（提示词已强约束 schema）
        invoke_model = llm.bind(response_format={"type": "json_object"})

    invoke_kwargs: dict[str, Any] = {}
    if runnable_config is not None:
        invoke_kwargs["config"] = runnable_config

    response = invoke_model.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=user_message)],
        **invoke_kwargs,
    )
    raw_text = response.content if isinstance(response.content, str) else str(response.content)

    # ── 结构化校验：失败抛 ValueError，由 API 层转 SKILL_OUTPUT_INVALID ──
    structured = _validate_structured_output(raw_text, output_schema)

    usage = _extract_usage(response)

    return SkillResult(text=raw_text, structured=structured, usage=usage)


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
