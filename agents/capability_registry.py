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
import logging
import os
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIGS_DIR = Path(__file__).resolve().parent / "configs"
SKILLS_DIR = PROJECT_ROOT / "skills"

# ---------- 缓存 ----------
_agent_cfg_cache: dict[str, dict] = {}
_skill_cfg_cache: dict[str, dict] = {}

# 当前 supervisor 构建出的注册表（供 planner/executor 共享）
_REGISTRY: dict[str, Any] = {"agents": {}}

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 技能内部结构化 LLM 调用（json_mode：规划层/评级层/单阶段 JSON 技能）的 tag。
# 这些调用产出的 JSON 是中间产物（随后走确定性渲染或直接落 structured），
# 不是面向用户的回复；流式接口（api/services/agent_runner.py）按此 tag 过滤，
# 避免把内部 JSON token 推给前端。
SKILL_INTERNAL_LLM_TAG = "skill_internal_llm"

# 技能内部 LLM 调用的单次超时（秒）。全局模型 streaming=True，该超时对流式调用
# 的语义是「相邻 chunk 之间的最大静默间隔」：上游断流/连接挂起时在超时后抛异常，
# 走既有重试/失败路径，而不是让整个技能执行永久卡死（前端只能干等事件超时）。
# 通过 llm.bind(timeout=...) 以 SDK 每请求超时的方式叠加，不改全局共享模型实例。
# 可用环境变量 SKILL_LLM_TIMEOUT_SECONDS 覆盖。
SKILL_LLM_TIMEOUT_SECONDS = int(os.getenv("SKILL_LLM_TIMEOUT_SECONDS", "300"))

# ── 技能执行进度上报 ────────────────────────────────────────────
# 技能内核在关键阶段（规划/逐项评估/渲染交付物等）调用 emit_skill_progress，
# 把人类可读的步骤说明推给流式执行方（agent_runner）转换为前端 status 事件，
# 让长任务（如 HARA 数分钟）期间用户能看到后台在持续推进。
# 通过 ContextVar 传递：executor 节点同步执行于线程池时 contextvars 随
# copy_context 复制，内核线程内 emit 即可达；无监听方（CLI/同步调用）为 no-op。
_skill_progress_cb: ContextVar = ContextVar("skill_progress_cb", default=None)


def emit_skill_progress(text: str) -> None:
    cb = _skill_progress_cb.get()
    if not cb:
        return
    try:
        cb(str(text))
    except Exception:  # noqa: BLE001 —— 进度上报永不影响技能执行
        pass


def bind_skill_progress(cb: Callable[[str], None]):
    """绑定当前上下文的技能进度回调；返回 token，供执行方结束时 reset。"""
    return _skill_progress_cb.set(cb)


def unbind_skill_progress(token) -> None:
    _skill_progress_cb.reset(token)


def _with_internal_tag(runnable_config: Any) -> dict:
    """在 invoke config 上叠加「技能内部调用」tag（保留原 config 的 callbacks/tags）。"""
    base = dict(runnable_config or {})
    base["tags"] = [*(base.get("tags") or []), SKILL_INTERNAL_LLM_TAG]
    return base


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
    # inputs 允许空映射：inputs: {} 表示无入参技能（输入走 reference_data，
    # 如后端直达的 FR 生成/子系统分配），故 inputs 只判是否声明，其余字段必须非空
    missing = []
    for key in required:
        value = cfg.get(key)
        if key == "inputs":
            if value is None:
                missing.append(key)
        elif not value:
            missing.append(key)
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


def _is_value_provided(value: Any) -> bool:
    """输入值是否算"已提供"：None/空白字符串/空集合视为缺失。"""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list, tuple, set)):
        return bool(value)
    return True


def get_missing_required_inputs(
    agent_name: str,
    skill_name: str,
    inputs: dict[str, Any] | None,
) -> list[str]:
    """预校验技能步骤的必填输入是否齐全，返回缺失项列表。

    两种必填约束：
      1. inputs.<name>.required: true     —— 单参数必填
      2. required_any: [[a, b], ...]      —— 每组至少一个有值（条件必填），
         整组缺失时以 "a|b" 形式作为一个元素返回，由 describe_skill_params
         渲染成"a 或 b（至少提供其一）"。

    供 Planner 出口自修复与 Executor 兜底共用，把"缺参"拦截在执行崩溃之前。
    - 未指定 skill（self_handle/compose/worker 通用对话）→ 不校验，返回 []
    - agent/skill 未注册（绑定错误）→ 不拦截，交给执行期原有的 KeyError 路径
    - 值为 ${output_key} 引用（chain 模式引用前序结果）→ 视为已提供
    """
    if not skill_name:
        return []
    try:
        skill_cfg = validate_binding(agent_name, skill_name)
    except KeyError:
        return []
    provided = inputs or {}

    def _provided(name: str) -> bool:
        value = provided.get(name)
        if isinstance(value, str) and value.strip().startswith("${"):
            return True  # chain 前序结果引用
        return _is_value_provided(value)

    missing: list[str] = []
    for name, spec in (skill_cfg.get("inputs") or {}).items():
        if spec.get("required") and not _provided(name):
            missing.append(name)

    for group in skill_cfg.get("required_any") or []:
        if isinstance(group, list) and group and not any(_provided(n) for n in group):
            missing.append("|".join(str(n) for n in group))
    return missing


def describe_skill_params(skill_cfg: dict, param_names: list[str]) -> str:
    """把缺失参数渲染成给用户看的自然语言说明（用技能契约中的 desc）。

    元素可能是单参数名（"file_id"）或条件必填组（"item_definition|file_id"）。
    """
    schema = skill_cfg.get("inputs") or {}
    parts = []
    for token in param_names:
        names = token.split("|")
        if len(names) > 1:
            rendered = " 或 ".join(
                f"{(schema.get(n) or {}).get('desc') or n}（{n}）" for n in names
            )
            parts.append(f"{rendered}，至少提供其一")
        else:
            name = names[0]
            spec = schema.get(name) or {}
            parts.append(f"{spec.get('desc') or name}（{name}）")
    return "、".join(parts)


_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render_prompt_template(
    template: str,
    inputs: dict[str, Any],
    input_schema: dict[str, dict],
) -> str:
    """用实际输入渲染技能模板（{{param}} 占位符）。

    - 必填输入缺失 → ValueError（正常情况下 Planner 出口预校验与 Executor
      兜底已将缺参转为对话式澄清，不应到达此处；该异常是最后防线）
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
            f"调度层应先以对话式澄清（tool/skill 留空的 self_handle 步骤）"
            f"向用户追问补齐后再执行该技能，禁止带缺参直接调用"
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
                renderer = output.get("renderer") or {}
                if renderer.get("format"):
                    out_desc += f"，确定性渲染交付物：{renderer['format']}（返回文件 ID 与下载链接）"
                # 条件必填组（required_any）在目录中显式提示，避免 Planner 误判缺参
                required_any = skill.get("required_any") or []
                any_desc = ""
                if required_any:
                    groups = [" 或 ".join(str(n) for n in g) for g in required_any if isinstance(g, list)]
                    any_desc = f" ｜ 条件必填（每组至少提供其一）：{'；'.join(groups)}"
                lines.append(
                    f"  - skill: {skill_name}"
                    f" ｜ {skill.get('display_name')}"
                    f" ｜ 能力: {skill.get('description')}"
                    f" ｜ 输入: {_format_inputs(skill.get('inputs', {}))}"
                    f"{any_desc}"
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


class SkillInputError(ValueError):
    """技能执行期的可纠正输入错误（如 file_id 对应文档不存在/无法解析）。

    Executor 捕获后转为对话式提示，而不是让请求 500。
    """


def _load_reference_texts(skill_cfg: dict) -> str:
    """拼接技能声明的 reference_files（相对技能 yaml 所在目录）。"""
    base = _skill_base_dir(skill_cfg)
    blocks: list[str] = []
    for rel in skill_cfg.get("reference_files") or []:
        path = base / rel
        if not path.is_file():
            raise FileNotFoundError(
                f"技能 {skill_cfg.get('name')} 的参考文档不存在：{path}"
            )
        blocks.append(
            f"\n\n# ══ 参考文档：{path.name} ══\n"
            + path.read_text(encoding="utf-8")
        )
    return "".join(blocks)


def _build_skill_system_prompt(skill_cfg: dict, agent_cfg: dict) -> str:
    """组装系统提示词：skill.system_prompt + reference_files；skill 缺省时用 agent 兜底。"""
    from agents.generate_agent import _resolve_system_prompt

    skill_system = skill_cfg.get("system_prompt")
    if skill_system:
        prompt = _resolve_system_prompt(skill_system, _skill_base_dir(skill_cfg))
    else:
        prompt = _resolve_system_prompt(
            agent_cfg.get("default_system_prompt", ""), CONFIGS_DIR
        )
    return prompt + _load_reference_texts(skill_cfg)


def _read_uploaded_document(skill_cfg: dict, inputs: dict[str, Any]) -> str:
    """按 document_source 声明读取上传文档，返回 read_document 的原始文本。

    无 document_source / 无 file_id 时返回空串；文件损坏/过期抛 SkillInputError。
    """
    source_cfg = skill_cfg.get("document_source") or {}
    file_id_input = source_cfg.get("file_id_input")
    if not file_id_input:
        return ""
    file_id = str(inputs.get(file_id_input) or "").strip()
    if not file_id:
        return ""

    from tools.read_document import read_document

    # 内部确定性读取：禁用回调，避免在 per-thread 日志中以 TOOL_START/END 形式
    # 再回显一遍整份文档（该工具调用不是 LLM 决策，产物随后会注入用户消息）
    content = read_document.invoke(
        {"file_id": file_id}, config={"callbacks": []}
    )
    if not isinstance(content, str) or content.startswith("[读取失败]") or content.startswith("[读取成功但内容为空]"):
        raise SkillInputError(
            f"无法读取 file_id={file_id} 对应的文档：{content[:200]}。"
            "请提示用户重新上传文档（docx/xlsx/pptx/pdf 等），或直接用文字描述相关项信息。"
        )
    return content


def _document_query_text(raw_content: str, limit: int = 500) -> str:
    """从 read_document 原始文本提取用于知识库检索的查询片段。

    read_document 内容首行是 "# 文档内容：<文件名>（file_id=…，提取字符数=…）"，
    随后是分隔线与正文。检索 query 取正文（跳过头部元信息行）前 limit 字符。
    """
    body = raw_content
    if body.startswith("# 文档内容"):
        # 跳过 read_document 头部：第 1 行"# 文档内容：<文件名>"、
        # 第 2 行"（file_id=…，类型=…，大小=…，提取字符数=…）"与分隔线
        lines = body.split("\n")
        kept: list[str] = []
        for line in lines[1:]:
            s = line.strip()
            if not kept and (not s or s == "---" or s.startswith("（file_id=")):
                continue
            kept.append(line)
        body = "\n".join(kept)
    # 折叠连续空白与 Markdown 标记噪声
    body = re.sub(r"\s+", " ", body).strip()
    return body[:limit]


def _resolve_knowledge_block(
    skill_cfg: dict,
    inputs: dict[str, Any],
    *,
    fallback_query: str = "",
) -> str:
    """按 knowledge 声明检索 auto-kb 历史项目参考，返回追加到用户消息的只读段落。

    knowledge:
        domain: 功能安全             # 领域名，对应 agents/configs/knowledge.yaml 的 domains
        query_from: item_definition  # 用哪个输入参数构造检索 query
        top_k: 5                     # 可选，默认 5
        score_threshold: 0.3         # 可选
        meta_filter: {车型: X}       # 可选，文档级元数据过滤

    query 解析顺序：inputs[query_from] → fallback_query（file_id-only 时的文档正文头部）。
    软降级承诺：功能未启用 / query 为空 / 服务不可达 / 任何异常 → 返回空串并记日志，
    绝不阻断技能执行；检索实现在 core/kb_client.py。
    """
    kb_cfg = skill_cfg.get("knowledge") or {}
    if not isinstance(kb_cfg, dict) or not kb_cfg:
        return ""
    try:
        from core.kb_client import format_knowledge_block, retrieve_knowledge

        domain = str(kb_cfg.get("domain") or "").strip()
        query_from = str(kb_cfg.get("query_from") or "").strip()
        raw_query = str(inputs.get(query_from) or "").strip() if query_from else ""
        if not raw_query:
            raw_query = str(fallback_query or "").strip()
        query = raw_query[:500] if len(raw_query) > 500 else raw_query
        if not domain or not query:
            # 未声明 domain/query_from，或核心参数与文档均为空 → 跳过
            return ""
        chunks = retrieve_knowledge(
            domain,
            query,
            top_k=kb_cfg.get("top_k"),
            score_threshold=kb_cfg.get("score_threshold"),
            meta_filter=kb_cfg.get("meta_filter") or None,
        )
        logger.info(
            "[capability_registry] 技能 %s 知识检索命中 %d 块，注入 %d 字符",
            skill_cfg.get("name"), len(chunks),
        )
        return format_knowledge_block(chunks, domain)
    except Exception as exc:  # noqa: BLE001 —— 注入过程任何异常都不阻断技能执行
        logger.warning(
            "[capability_registry] 技能 %s 知识注入降级：%s", skill_cfg.get("name"), exc
        )
        return ""


def _build_schema_example_block(skill_cfg: dict) -> str:
    """把 output.example_file 的样例 JSON 渲染成输出示例段落（无则空串）。"""
    output_cfg = skill_cfg.get("output", {}) or {}
    rel = output_cfg.get("example_file")
    if not rel:
        return ""
    path = _skill_base_dir(skill_cfg) / rel
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    return (
        "\n\n【输出 JSON 结构示例（展示字段结构与枚举写法；内容必须来自本次分析，"
        "不要照抄示例数据）】\n" + text
    )


# 渲染器模块缓存（同一脚本只导入一次）
_renderer_cache: dict[str, Any] = {}


def _run_renderer(
    skill_cfg: dict, structured: dict | list
) -> tuple[Any, dict]:
    """执行技能声明的 output.renderer，把结构化结果渲染成文件并落入文件沙箱。

    返回 (artifact_meta, stats)；未声明渲染器返回 (None, {})。
    """
    output_cfg = skill_cfg.get("output", {}) or {}
    renderer_cfg = output_cfg.get("renderer")
    if not renderer_cfg:
        return None, {}
    if not isinstance(structured, dict):
        raise ValueError("渲染器要求结构化输出为 JSON 对象")

    import importlib.util
    import tempfile
    from datetime import datetime

    from core.file_sandbox import save_generated

    base = _skill_base_dir(skill_cfg)
    script_rel = renderer_cfg.get("script")
    entrypoint = renderer_cfg.get("entrypoint", "generate")
    out_format = renderer_cfg.get("format", "xlsx").lstrip(".")
    if not script_rel:
        raise ValueError(f"技能 {skill_cfg.get('name')} 的 renderer 缺少 script 配置")
    script_path = (base / script_rel).resolve()
    if not script_path.is_file():
        raise FileNotFoundError(f"渲染器脚本不存在：{script_path}")

    cache_key = str(script_path)
    module = _renderer_cache.get(cache_key)
    if module is None:
        spec = importlib.util.spec_from_file_location(
            f"skill_renderer_{skill_cfg.get('name')}_{script_path.stem}", script_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _renderer_cache[cache_key] = module
    fn = getattr(module, entrypoint, None)
    if not callable(fn):
        raise AttributeError(f"渲染器 {script_path} 不存在入口函数 {entrypoint!r}")

    # 文件名模板：{abbr}/{name}/{date}，净化为安全文件名
    item = structured.get("item") or {}
    raw_abbr = str(item.get("abbr") or item.get("name") or skill_cfg.get("name") or "out")
    safe_abbr = re.sub(r"[^\w\-.]+", "_", raw_abbr).strip("_")[:40] or "out"
    template = renderer_cfg.get("filename_template") or f"{skill_cfg.get('name')}_{'{date}'}.{out_format}"
    filename = (
        template
        .replace("{abbr}", safe_abbr)
        .replace("{name}", safe_abbr)
        .replace("{date}", datetime.now().strftime("%Y%m%d"))
    )

    tmp_in = tmp_out = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(structured, f, ensure_ascii=False, indent=2)
            tmp_in = f.name
        fd, tmp_out = tempfile.mkstemp(suffix=f".{out_format}")
        os.close(fd)

        stats = fn(tmp_in, tmp_out) or {}
        content = Path(tmp_out).read_bytes()
        meta = save_generated(filename, content)
        return meta, stats if isinstance(stats, dict) else {}
    finally:
        for p in (tmp_in, tmp_out):
            try:
                if p:
                    Path(p).unlink(missing_ok=True)
            except OSError:
                pass


def _run_renderer_summary(
    skill_cfg: dict, structured: dict, artifact_path: Path
) -> str:
    """调用渲染器可选的 summary_entrypoint，返回技能自定义的附加 markdown 段落。

    入口签名 summarize(in_json_path, artifact_path) -> str；未声明/失败均返回 ""。
    业务专属摘要（如安全目标清单）由此落在技能脚本里，引擎不内置任何业务格式。
    """
    renderer_cfg = (skill_cfg.get("output") or {}).get("renderer") or {}
    entrypoint = renderer_cfg.get("summary_entrypoint")
    if not entrypoint or not isinstance(structured, dict):
        return ""
    script_rel = renderer_cfg.get("script")
    if not script_rel:
        return ""

    import tempfile

    base = _skill_base_dir(skill_cfg)
    script_path = (base / str(script_rel)).resolve()
    module = _renderer_cache.get(str(script_path))
    if module is None:  # 理论上 generate 刚加载过，防御性补加载
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            f"skill_renderer_{skill_cfg.get('name')}_{script_path.stem}", script_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _renderer_cache[str(script_path)] = module
    fn = getattr(module, entrypoint, None)
    if not callable(fn):
        logger.warning(
            "[capability_registry] 渲染器 %s 不存在摘要入口 %r，跳过附加摘要",
            script_path, entrypoint,
        )
        return ""

    tmp_in = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(structured, f, ensure_ascii=False, indent=2)
            tmp_in = f.name
        md = fn(tmp_in, str(artifact_path))
        return str(md or "").strip()
    except Exception as exc:  # noqa: BLE001 —— 摘要失败不影响交付
        logger.warning("[capability_registry] 渲染器摘要生成失败：%s", exc)
        return ""
    finally:
        if tmp_in:
            try:
                Path(tmp_in).unlink(missing_ok=True)
            except OSError:
                pass


# ════════════════════════════════════════════════════════════════
# 技能前置钩子（execution.prepare）：与 output.renderer 对称的技能侧插件
# 技能自定义的识别/检索/前置加工放在技能目录 scripts/ 下，引擎不内置业务逻辑。
# ════════════════════════════════════════════════════════════════

@dataclass
class PrepareContext:
    """prepare 钩子运行上下文（稳定契约：只增字段、不改既有字段语义）。

    数据：skill_cfg/inputs/rendered/doc_raw/doc_block/runnable_config/reference_data；
    能力：模型构造、阶段提示装配、json 阶段调用、token 计量、知识库检索/格式化。

    回调能力字段对普通（非 map_reduce）技能的 prepare 钩子可选，传 None 即可；
    map_reduce 技能的 prepare 钩子会用到全部字段。
    """

    skill_cfg: dict
    inputs: dict[str, Any]
    rendered: str
    doc_raw: str
    doc_block: str
    runnable_config: Any
    logger: logging.Logger
    reference_data: dict[str, Any] | None = None
    bind_json_model: Callable[[Any], Any] | None = None
    build_stage_prompt: Callable[[Any], str] | None = None
    invoke_json_stage: Callable[..., Any] | None = None
    extract_usage: Callable[[Any], dict] | None = None
    retrieve_knowledge: Callable[..., list] | None = None
    format_knowledge_layered_block: Callable[..., str] | None = None


_prepare_cache: dict[str, Any] = {}


def _load_skill_script_module(skill_cfg: dict, script_rel: str,
                              cache: dict[str, Any], prefix: str):
    """importlib 按技能目录动态加载脚本（带缓存）；脚本不存在抛异常。"""
    import importlib.util

    base = _skill_base_dir(skill_cfg)
    script_path = (base / str(script_rel)).resolve()
    if not script_path.is_file():
        raise FileNotFoundError(f"技能脚本不存在：{script_path}")

    module = cache.get(str(script_path))
    if module is None:
        spec = importlib.util.spec_from_file_location(
            f"{prefix}_{skill_cfg.get('name')}_{script_path.stem}", script_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cache[str(script_path)] = module
    return module


def _run_prepare_hook(ctx: PrepareContext) -> dict | None:
    """加载并执行技能声明的 execution.prepare 脚本；未声明返回 None。

    约定入口 prepare(ctx) -> dict，引擎解释约定键：
      - kb_block：注入规划层的历史知识块（空串 → 引擎回退粗召回）；
      - usage：钩子内 LLM 调用的 token 计量；
      - doc_supplement：可选，技能侧对上传文档的确定性结构化补充
        （如直接解析 DOCX 功能清单表格），非空时紧随文档块注入规划层；
    其余键透传忽略；异常由调用方捕获软降级。
    """
    prepare_cfg = (ctx.skill_cfg.get("execution") or {}).get("prepare") or {}
    script_rel = prepare_cfg.get("script")
    if not script_rel:
        return None

    module = _load_skill_script_module(
        ctx.skill_cfg, script_rel, _prepare_cache, "skill_prepare"
    )
    entrypoint = prepare_cfg.get("entrypoint", "prepare")
    fn = getattr(module, entrypoint, None)
    if not callable(fn):
        raise AttributeError(f"prepare 脚本 {script_rel} 不存在入口 {entrypoint!r}")

    result = fn(ctx)
    return result if isinstance(result, dict) else {}


# ── 技能后置钩子（execution.postprocess）：与 prepare 对称，LLM 输出清洗/业务校验 ──
# guard 脚本用于剥未知字段、规整结构、校验引用完整性等；失败抛 ValueError → SKILL_OUTPUT_INVALID

@dataclass
class PostprocessContext:
    """postprocess 钩子运行上下文（与 PrepareContext 对称，用于 LLM 输出后处理）。

    数据：skill_cfg/structured（schema 校验后的 LLM 输出）/reference_data/inputs/logger。
    钩子 postprocess(ctx) -> dict：返回 {"structured": <cleaned>} 覆盖原输出；
    返回 None 或不声明 → 保留原输出。
    钩子抛 ValueError → 视为输出校验失败，向上抛为 SKILL_OUTPUT_INVALID；
    其他异常 → 软降级保留原输出。
    """

    skill_cfg: dict
    structured: dict | list | None
    reference_data: dict[str, Any] | None = None
    inputs: dict[str, Any] | None = None
    logger: logging.Logger | None = None


_postprocess_cache: dict[str, Any] = {}


def _run_postprocess_hook(ctx: PostprocessContext) -> dict | None:
    """加载并执行技能声明的 execution.postprocess 脚本；未声明返回 None。

    约定入口 postprocess(ctx) -> dict，引擎解释约定键：
      - structured：清洗后的结构化输出（覆盖原 structured）；
    其余键透传忽略。

    钩子抛 ValueError 视为校验失败，由调用方转为 SKILL_OUTPUT_INVALID；
    其他异常软降级保留原输出。
    """
    postprocess_cfg = (ctx.skill_cfg.get("execution") or {}).get("postprocess") or {}
    script_rel = postprocess_cfg.get("script")
    if not script_rel:
        return None

    module = _load_skill_script_module(
        ctx.skill_cfg, script_rel, _postprocess_cache, "skill_postprocess"
    )
    entrypoint = postprocess_cfg.get("entrypoint", "postprocess")
    fn = getattr(module, entrypoint, None)
    if not callable(fn):
        raise AttributeError(
            f"postprocess 脚本 {script_rel} 不存在入口 {entrypoint!r}"
        )

    result = fn(ctx)
    return result if isinstance(result, dict) else None


@dataclass
class MapSliceContext:
    """map_reduce 每切片检索钩子上下文（稳定契约：只增字段）。

    数据：skill_cfg/index（0 基切片序号）/unit（当前切片 dict）/plan_parsed/
          inputs/runnable_config；
    能力：retrieve_knowledge（domain, query, top_k, score_threshold, layer）。
    钩子 retrieve_entrypoint(ctx) -> list[分块]；空列表表示零命中；
    抛异常时引擎软降级为通用单路检索。
    """

    skill_cfg: dict
    index: int
    unit: dict
    plan_parsed: dict
    inputs: dict[str, Any]
    runnable_config: Any
    logger: logging.Logger
    retrieve_knowledge: Callable[..., list]


def _run_map_retrieve_hook(ctx: MapSliceContext) -> list | None:
    """加载执行 execution.map 声明的每切片检索钩子；未声明返回 None。

    脚本默认复用 execution.prepare.script，可用 map.retrieve_script 覆盖；
    入口由 map.retrieve_entrypoint 指定（默认 retrieve_map）。
    """
    exec_cfg = ctx.skill_cfg.get("execution") or {}
    map_cfg = exec_cfg.get("map") or {}
    entrypoint = str(map_cfg.get("retrieve_entrypoint") or "").strip()
    script_rel = map_cfg.get("retrieve_script") or (
        (exec_cfg.get("prepare") or {}).get("script")
    )
    if not entrypoint or not script_rel:
        return None

    module = _load_skill_script_module(
        ctx.skill_cfg, script_rel, _prepare_cache, "skill_prepare"
    )
    fn = getattr(module, entrypoint, None)
    if not callable(fn):
        raise AttributeError(f"map 检索脚本 {script_rel} 不存在入口 {entrypoint!r}")
    result = fn(ctx)
    return result if isinstance(result, list) else []


@dataclass
class MapReviewContext:
    """map_reduce 每切片结果评审钩子上下文（稳定契约：只增字段）。

    数据：skill_cfg/index（0 基切片序号）/unit（当前切片 dict）/plan_parsed/
          events（评级层原始输出事件列表）/chunks（本切片召回的历史知识分块，
          可能为空列表——不依赖历史召回的复核如"显著事件缺少安全目标"仍会执行，
          钩子须自行处理空 chunks）/logger；
    能力：bind_json_model / build_stage_prompt（传完整阶段配置）/
          invoke_json_stage / extract_usage（复核 LLM 调用与 token 计量）。
    钩子 review_entrypoint(ctx) -> {"events": [...], "usage": {...}}（list 视为仅
    events，usage 记空）；异常由引擎捕获软降级保留原评级结果。
    """

    skill_cfg: dict
    index: int
    unit: dict
    plan_parsed: dict
    events: list
    chunks: list
    logger: logging.Logger
    bind_json_model: Callable[[Any], Any]
    build_stage_prompt: Callable[[Any], str]
    invoke_json_stage: Callable[..., Any]
    extract_usage: Callable[[Any], dict]


def _run_map_review_hook(ctx: MapReviewContext) -> dict | list | None:
    """加载执行 execution.map 声明的每切片评审钩子；未声明返回 None。

    脚本默认复用 execution.prepare.script，可用 map.review_script 覆盖；
    入口由 map.review_entrypoint 指定。
    """
    exec_cfg = ctx.skill_cfg.get("execution") or {}
    map_cfg = exec_cfg.get("map") or {}
    entrypoint = str(map_cfg.get("review_entrypoint") or "").strip()
    script_rel = map_cfg.get("review_script") or (
        (exec_cfg.get("prepare") or {}).get("script")
    )
    if not entrypoint or not script_rel:
        return None

    module = _load_skill_script_module(
        ctx.skill_cfg, script_rel, _prepare_cache, "skill_prepare"
    )
    fn = getattr(module, entrypoint, None)
    if not callable(fn):
        raise AttributeError(f"map 评审脚本 {script_rel} 不存在入口 {entrypoint!r}")
    return fn(ctx)


@dataclass
class PlanPostprocessContext:
    """map_reduce 规划后处理钩子上下文（稳定契约：只增字段）。

    数据：skill_cfg/plan_parsed（规划层完整输出，钩子可就地修改）/inputs/logger；
    能力：retrieve_knowledge（技能侧按 ref_id 回查知识库原文）。
    钩子 execution.plan.postprocess_entrypoint(ctx) -> dict|None：返回诊断统计
    （引擎仅记日志）；异常由引擎捕获软降级，保留规划层原输出。
    """

    skill_cfg: dict
    plan_parsed: dict
    inputs: dict[str, Any]
    logger: logging.Logger
    retrieve_knowledge: Callable[..., list]


def _run_plan_postprocess_hook(ctx: PlanPostprocessContext) -> dict | None:
    """加载执行 execution.plan 声明的规划后处理钩子；未声明返回 None。

    脚本默认复用 execution.prepare.script，可用 plan.postprocess_script 覆盖；
    入口由 plan.postprocess_entrypoint 指定。钩子就地修改 plan_parsed
    （如按 source.ref_id 用知识库原文覆盖沿用条目），返回诊断 dict。
    """
    exec_cfg = ctx.skill_cfg.get("execution") or {}
    plan_cfg = exec_cfg.get("plan") or {}
    entrypoint = str(plan_cfg.get("postprocess_entrypoint") or "").strip()
    script_rel = plan_cfg.get("postprocess_script") or (
        (exec_cfg.get("prepare") or {}).get("script")
    )
    if not entrypoint or not script_rel:
        return None

    module = _load_skill_script_module(
        ctx.skill_cfg, script_rel, _prepare_cache, "skill_prepare"
    )
    fn = getattr(module, entrypoint, None)
    if not callable(fn):
        raise AttributeError(f"规划后处理脚本 {script_rel} 不存在入口 {entrypoint!r}")
    result = fn(ctx)
    return result if isinstance(result, dict) else {}


def _format_artifact_summary(
    skill_cfg: dict,
    meta: Any,
    stats: dict,
    artifact_path: Path | None,
    structured: dict | list | None = None,
) -> str:
    """把渲染产物组织成面向用户的中文摘要（替代裸 JSON 输出）。

    完全由技能契约驱动，不含任何业务专属格式：
      - 统计标签：output.renderer.stat_labels（{stats 键: 中文标签}，按声明顺序）；
      - 附加段落：渲染器 output.renderer.summary_entrypoint（技能脚本生成）；
      - 尾注：output.summary_footer。
    """
    output_cfg = skill_cfg.get("output", {}) or {}
    renderer_cfg = output_cfg.get("renderer") or {}
    name = skill_cfg.get("display_name") or skill_cfg.get("name")
    lines = [
        f"## {name}已完成",
        "",
        f"**交付物**：{meta.original_name}（{meta.size / 1024:.1f} KB）",
        f"文件 ID：`{meta.file_id}`",
        f"下载方式：`GET /api/v1/agent/files/{meta.file_id}/download`（需携带 API Key）",
        "",
    ]
    stat_labels = renderer_cfg.get("stat_labels") or {}
    if stats and isinstance(stat_labels, dict):
        lines.append("**工作簿统计**：")
        for key, label in stat_labels.items():
            if key in stats:
                lines.append(f"- {label}：{stats[key]}")
        lines.append("")

    if artifact_path is not None and isinstance(structured, dict):
        extra = _run_renderer_summary(skill_cfg, structured, artifact_path)
        if extra:
            lines.append(extra)
            lines.append("")

    footer = str(output_cfg.get("summary_footer") or "").strip()
    if footer:
        lines.append(footer)
    return "\n".join(lines)


def _execute_skill_core(
    agent_name: str,
    skill_name: str,
    inputs: dict[str, Any],
    *,
    reference_data: dict[str, Any] | None = None,
    runnable_config: Any = None,
) -> SkillResult:
    """技能执行统一内核（v1/v2 共用）。

    流程：
      1. 校验绑定、渲染 prompt 模板；
      2. document_source：先读用户上传文档，追加为只读文档段落；
      3. knowledge：按技能声明检索 auto-kb 历史项目参考，追加为只读参考段落（软降级）；
      4. 组装 system_prompt（skill + reference_files）；
      5. 结构化技能：json_mode 调用 → schema 轻量校验；
      6. output.renderer：JSON → 确定性文件渲染 → 落文件沙箱 → 中文摘要；
      7. 返回 SkillResult（text/structured/usage/artifacts）。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from agents.generate_agent import _resolve_model

    skill_cfg = validate_binding(agent_name, skill_name)
    agent_cfg = load_agent_config(agent_name)

    # ── map_reduce 技能（识别层 1 次 + 规划层 1 次 + 切片并发评级 N 次 → 合并 → 渲染） ──
    if (skill_cfg.get("execution") or {}).get("mode") == "map_reduce":
        return _execute_staged_skill(
            skill_cfg,
            agent_cfg,
            inputs or {},
            reference_data=reference_data,
            runnable_config=runnable_config,
        )

    rendered = render_prompt_template(
        skill_cfg["prompt_template"],
        inputs or {},
        skill_cfg.get("inputs", {}),
    )

    # ── 执行链前置：读取上传文档（file_id → Markdown 段落），只读一次 ──
    doc_raw = _read_uploaded_document(skill_cfg, inputs or {})
    doc_block = (
        "\n\n【相关项文档（Markdown，平台从用户上传文件转换，分析以此为主要事实来源）】\n"
        + doc_raw
    ) if doc_raw else ""
    doc_query = _document_query_text(doc_raw)

    # ── 知识注入：按 knowledge 声明检索 auto-kb 历史项目参考（软降级，失败即空串） ──
    kb_block = _resolve_knowledge_block(
        skill_cfg, inputs or {}, fallback_query=doc_query
    )

    # ── 外部 reference_data 注入（API 后台预取资料） ──
    ref_block = _build_reference_block(reference_data)

    # ── 技能侧 prepare 钩子（可选）：自定义前置加工，如拉取远端模板、解析文档结构等 ──
    # 与 map_reduce 路径对称；钩子失败软降级，不阻断技能执行。
    # 轻量检索类技能（如 knowledge_lookup）可在钩子内完成「调 LLM 解析意图 →
    # 路由域适配器检索 → 渲染表格」全流程，返回 final_text 短路跳过后续 LLM 调用。
    prepare_supplement = ""
    prepare_final_text: str | None = None
    prepare_usage: dict[str, Any] = {}
    if (skill_cfg.get("execution") or {}).get("prepare"):
        # 懒加载检索/格式化/模型回调：仅声明 prepare 钩子的技能可能需要
        # （单阶段技能原本不传这些回调，为支持检索类 prepare 钩子而补齐，
        #   与 map_reduce 路径保持对称；不声明 prepare 的技能不受影响）
        try:
            from core.kb_client import (
                format_knowledge_layered_block,
                retrieve_knowledge,
            )
        except Exception:  # noqa: BLE001 —— 回调加载失败时 prepare 钩子软降级
            format_knowledge_layered_block = None  # type: ignore[assignment]
            retrieve_knowledge = None  # type: ignore[assignment]
        base_llm_for_hook = _build_model_with_hint(
            _resolve_model(agent_cfg.get("model")), skill_cfg.get("model_hint")
        )

        def _bind_json_model_for_hook(max_tokens: Any) -> Any:
            bind_kwargs: dict[str, Any] = {
                "response_format": {"type": "json_object"},
                "timeout": SKILL_LLM_TIMEOUT_SECONDS,
            }
            if isinstance(max_tokens, int) and max_tokens > 0:
                bind_kwargs["max_tokens"] = max_tokens
            return base_llm_for_hook.bind(**bind_kwargs)

        try:
            prepare_ctx = PrepareContext(
                skill_cfg=skill_cfg,
                inputs=inputs or {},
                rendered=rendered,
                doc_raw=doc_raw,
                doc_block=doc_block,
                runnable_config=runnable_config,
                logger=logger,
                reference_data=reference_data,
                bind_json_model=_bind_json_model_for_hook,
                build_stage_prompt=lambda stage_cfg: _build_stage_system_prompt(
                    skill_cfg, stage_cfg
                ),
                invoke_json_stage=_invoke_json_stage,
                extract_usage=_extract_usage,
                retrieve_knowledge=retrieve_knowledge,
                format_knowledge_layered_block=format_knowledge_layered_block,
            )
            hook_result = _run_prepare_hook(prepare_ctx) or {}
            prepare_supplement = str(hook_result.get("doc_supplement") or "")
            # 短路：prepare 钩子返回 final_text 时跳过后续 LLM 调用，
            # 直接以该文本作为技能最终输出（轻量检索类技能走此路径）
            ft = hook_result.get("final_text")
            if isinstance(ft, str) and ft.strip():
                prepare_final_text = ft
            hu = hook_result.get("usage") or {}
            if isinstance(hu, dict):
                prepare_usage = dict(hu)
            if prepare_supplement:
                logger.info(
                    "[capability_registry] 技能 %s prepare 钩子注入补充段 %d 字符",
                    skill_cfg.get("name"), len(prepare_supplement),
                )
        except Exception as exc:  # noqa: BLE001 —— 前置钩子失败必须软降级
            logger.warning(
                "[capability_registry] 技能 %s prepare 钩子失败，忽略补充段：%s",
                skill_cfg.get("name"), exc,
            )
    prepare_supplement_block = (
        f"\n\n{prepare_supplement.strip()}\n" if prepare_supplement else ""
    )

    # 短路返回：prepare 钩子已产出最终文本（如知识库检索表格），不再调 LLM
    if prepare_final_text is not None:
        logger.info(
            "[capability_registry] 技能 %s prepare 钩子短路返回（%d 字符）",
            skill_cfg.get("name"), len(prepare_final_text),
        )
        return SkillResult(text=prepare_final_text, usage=prepare_usage)

    # ── 输出 JSON Schema 说明与示例段落 ──
    output_cfg = skill_cfg.get("output", {}) or {}
    output_format = output_cfg.get("format", "plain_text")
    output_schema = output_cfg.get("schema")
    schema_example_block = (
        _build_schema_example_block(skill_cfg) if output_format == "json" else ""
    )

    user_message = (
        rendered
        + doc_block
        + kb_block
        + ref_block
        + prepare_supplement_block
        + schema_example_block
    )

    # ── model_hint：叠加 skill 的推理开关/档位等 ──
    llm = _build_model_with_hint(
        _resolve_model(agent_cfg.get("model")),
        skill_cfg.get("model_hint"),
    )

    system_prompt = _build_skill_system_prompt(skill_cfg, agent_cfg)

    # 技能级输出上限：json 与 markdown 长文档技能均可声明 output.max_tokens
    # 覆盖全局 LLM_MAX_TOKENS（PRD/UC 等长文档在默认 8192 下会被硬截断）
    skill_max_tokens = output_cfg.get("max_tokens")
    has_skill_max = isinstance(skill_max_tokens, int) and skill_max_tokens > 0
    if output_format == "json" and output_schema:
        # json_mode + 技能级输出上限（HARA 等大 JSON 技能必须高于全局 LLM_MAX_TOKENS，
        # 否则可见输出在 max_tokens 处被截断，JSON 不闭合 → 解析失败）
        bind_kwargs: dict[str, Any] = {
            "response_format": {"type": "json_object"},
            "timeout": SKILL_LLM_TIMEOUT_SECONDS,
        }
        if has_skill_max:
            bind_kwargs["max_tokens"] = skill_max_tokens
        invoke_model = llm.bind(**bind_kwargs)
    elif has_skill_max:
        invoke_model = llm.bind(timeout=SKILL_LLM_TIMEOUT_SECONDS,
                                max_tokens=skill_max_tokens)
    else:
        invoke_model = llm.bind(timeout=SKILL_LLM_TIMEOUT_SECONDS)

    # 内部 JSON 调用打 tag（流式接口据此过滤 token）；纯文本技能不打，其输出即交付文本
    if output_format == "json" and output_schema:
        invoke_kwargs: dict[str, Any] = {"config": _with_internal_tag(runnable_config)}
    else:
        invoke_kwargs: dict[str, Any] = {}
        if runnable_config is not None:
            invoke_kwargs["config"] = runnable_config

    response = invoke_model.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=user_message)],
        **invoke_kwargs,
    )
    raw_text = response.content if isinstance(response.content, str) else str(response.content)

    # 截断预检：finish_reason=length
    finish_reason = ""
    resp_meta = getattr(response, "response_metadata", None)
    if isinstance(resp_meta, dict):
        finish_reason = str(resp_meta.get("finish_reason") or "").lower()
    if finish_reason == "length":
        limit = skill_max_tokens or os.getenv("LLM_MAX_TOKENS", "?")
        if output_format == "json" and output_schema:
            # JSON 必然不闭合，无法解析/渲染，直接给出可操作的错误
            raise ValueError(
                f"模型输出达到 token 上限（max_tokens={limit}）被截断，JSON 不完整。"
                f"已生成 {len(raw_text)} 字符；请在技能契约 output.max_tokens 中提高上限后重试。"
            )
        # markdown/纯文本：已生成内容对用户仍有价值。旧逻辑直接抛异常，导致用户
        # 看着流式输出的几千字被丢弃、终态替换成一句安抚话术；现保留部分正文，
        # 追加醒目的截断提示后正常返回（流式已吐出的内容与终态一致，不会被覆盖）。
        skill_name_log = skill_cfg.get("name") or skill_cfg.get("id") or "?"
        logger.warning(
            "技能 %s 输出达到 max_tokens=%s 被截断，已生成 %s 字符，按部分结果返回",
            skill_name_log, limit, len(raw_text),
        )
        raw_text = raw_text.rstrip() + (
            f"\n\n> ⚠️ **输出长度达到模型上限（max_tokens={limit}），文档在此处被截断，"
            "后续内容未生成。** 可以缩小本次生成范围（例如只生成指定章节）后重试，"
            "或在技能契约的 output.max_tokens 中提高上限。"
        )

    structured = _validate_structured_output(raw_text, output_schema)
    usage = _extract_usage(response)

    # ── 技能侧 postprocess 钩子（可选）：清洗 LLM 输出 / 业务规则校验 ──
    # 与 prepare 钩子对称；guard 脚本抛 ValueError → SKILL_OUTPUT_INVALID（HTTP 422）
    if (skill_cfg.get("execution") or {}).get("postprocess"):
        postprocess_ctx = PostprocessContext(
            skill_cfg=skill_cfg,
            structured=structured,
            reference_data=reference_data,
            inputs=inputs or {},
            logger=logger,
        )
        try:
            pp_result = _run_postprocess_hook(postprocess_ctx)
            if pp_result and "structured" in pp_result:
                structured = pp_result["structured"]
                # 同步 raw_text，保证 text/structured 一致（无 renderer 时 text 即 JSON 串）
                raw_text = json.dumps(structured, ensure_ascii=False, indent=2)
        except ValueError:
            # guard 校验失败：向上抛为 SKILL_OUTPUT_INVALID（由 agent_runner 捕获转换）
            raise
        except Exception as exc:  # noqa: BLE001 —— 非校验异常软降级保留原输出
            logger.warning(
                "[capability_registry] 技能 %s postprocess 钩子失败，保留原输出：%s",
                skill_cfg.get("name"), exc,
            )

    result = SkillResult(text=raw_text, structured=structured, usage=usage)

    # ── 确定性渲染：JSON → 文件交付物 ──
    if output_format == "json" and structured is not None:
        meta, stats = _run_renderer(skill_cfg, structured)
        if meta is not None:
            from core.file_sandbox import resolve_stored_path

            artifact_path = resolve_stored_path(meta.file_id)
            result.artifacts = [{
                "file_id": meta.file_id,
                "filename": meta.original_name,
                "format": meta.ext.lstrip("."),
                "size": meta.size,
            }]
            result.text = _format_artifact_summary(
                skill_cfg, meta, stats, artifact_path, structured
            )

    return result


# ════════════════════════════════════════════════════════════════
# 5A2. map_reduce 执行（可选 prepare 钩子 + 规划层 + 切片并发评级，供长输出技能使用）
# ════════════════════════════════════════════════════════════════

def _build_stage_system_prompt(skill_cfg: dict, stage_cfg: Any) -> str:
    """组装分阶段技能某一阶段的 system prompt（阶段提示 + 参考文档）。

    入参支持两种形态：
      - 完整阶段配置 {system_prompt: {file: ...}, references: [...]}（推荐）
      - 裸 system_prompt 规格 {file: ...}（向后兼容）

    references 可覆盖本阶段加载的参考文档：
      - 未声明：加载技能全部 reference_files（默认，plan/map 用）；
      - 声明为列表：只加载列出的文件（前置阶段只需部分参考文档时）；
      - 空列表：不加载参考文档。
    """
    from agents.generate_agent import _resolve_system_prompt

    if isinstance(stage_cfg, dict) and "system_prompt" in stage_cfg:
        prompt_spec: Any = stage_cfg.get("system_prompt")
        refs_override: Any = stage_cfg.get("references") if "references" in stage_cfg else None
    else:
        prompt_spec = stage_cfg
        refs_override = None

    base_dir = _skill_base_dir(skill_cfg)
    base_prompt = _resolve_system_prompt(prompt_spec, base_dir)
    if refs_override is None:
        ref_text = _load_reference_texts(skill_cfg)
    else:
        parts: list[str] = []
        for rel in refs_override:
            path = base_dir / str(rel)
            if not path.is_file():
                raise FileNotFoundError(
                    f"技能 {skill_cfg.get('name')} 的阶段参考文档不存在：{path}"
                )
            parts.append(f"\n\n# ══ 参考文档：{path.name} ══\n" + path.read_text(encoding="utf-8"))
        ref_text = "".join(parts)
    return base_prompt + ref_text


def _invoke_json_stage(
    bound_model: Any,
    system_prompt: str,
    user_message: str,
    *,
    stage_name: str,
    max_tokens: Any,
    runnable_config: Any = None,
) -> tuple[dict, Any]:
    """单阶段 json_mode 调用 → (parsed dict, response)。

    finish_reason=length 时抛可操作错误；JSON 解析失败抛 ValueError（map 阶段
    调用方据此重试）。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    # 始终叠加内部调用 tag（评级切片在工作线程内无 runnable_config，tag 也应存在）
    invoke_kwargs: dict[str, Any] = {
        "config": _with_internal_tag(runnable_config)
    }
    response = bound_model.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=user_message)],
        **invoke_kwargs,
    )
    raw_text = response.content if isinstance(response.content, str) else str(response.content)

    finish_reason = ""
    resp_meta = getattr(response, "response_metadata", None)
    if isinstance(resp_meta, dict):
        finish_reason = str(resp_meta.get("finish_reason") or "").lower()
    if finish_reason == "length":
        raise ValueError(
            f"{stage_name}输出达到 token 上限（max_tokens={max_tokens}）被截断，JSON 不完整。"
            f"已生成 {len(raw_text)} 字符；请提高该阶段 max_tokens 或缩小一次分析的功能范围。"
        )
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"{stage_name}输出无法解析为 JSON：{e}；原始文本前 200 字符：{raw_text[:200]!r}"
        ) from e
    if not isinstance(parsed, dict):
        raise ValueError(f"{stage_name}输出 JSON 顶层必须是对象，实际为 {type(parsed).__name__}")
    logger.info(
        "[capability_registry] %s 完成，输出 %d 字符，摘要：%s",
        stage_name, len(raw_text), raw_text[:500],
    )
    return parsed, response


def _execute_staged_skill(
    skill_cfg: dict,
    agent_cfg: dict,
    inputs: dict[str, Any],
    *,
    reference_data: dict[str, Any] | None = None,
    runnable_config: Any = None,
) -> SkillResult:
    """map_reduce 执行内核（可选 prepare 钩子 → plan 规划 → map 并发评级）。

    契约（技能 YAML）：
        execution:
          mode: map_reduce
          prepare:                      # 可选技能侧前置钩子（识别/自定义检索等）
            script: scripts/xxx.py
            entrypoint: prepare
            # 其余键由钩子脚本自行解释（如 identify 提示词/top_k），引擎不读取
          plan: {system_prompt: {file: ...}, max_tokens: 32768}
          map:
            system_prompt: {file: ...}
            slice_path: hazop_items   # 规划输出中待切片的列表键
            events_key: events        # 每个切片评级结果挂回切片的键
            max_workers: 3
            max_tokens: 16384
        knowledge:
          ...
          plan_top_k: 40
          layers: [function_list, failure_mode, hara_event, safety_goal]
          map_layer: hara_event
          map_top_k: 8

    流程：
      0. prepare 钩子（可选，技能脚本）：技能自定义识别/检索，返回规划层知识块；
         钩子缺失/异常/空结果时软降级为四层分组大召回；
      1. 规划层（1 次调用）：item/功能/失效矩阵/HAZOP 条目（含充分场景清单）；
      2. 评级层（按 slice_path 切片，ThreadPoolExecutor 并发）：每切片独立
         小召回历史 hara_event → 逐条事件 S/E/C 评级；单片失败自动重试 1 次；
      3. 评级事件挂回对应切片 → 合并为完整 JSON → schema 校验 → 渲染器出文件。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from agents.generate_agent import _resolve_model

    exec_cfg = skill_cfg.get("execution") or {}
    plan_cfg = exec_cfg.get("plan") or {}
    map_cfg = exec_cfg.get("map") or {}
    slice_path = str(map_cfg.get("slice_path") or "").strip()
    events_key = str(map_cfg.get("events_key") or "events").strip()
    max_workers = int(map_cfg.get("max_workers") or 3)
    if not slice_path:
        raise ValueError(
            f"技能 {skill_cfg.get('name')} 的 execution.map 缺少 slice_path 配置"
        )

    output_cfg = skill_cfg.get("output", {}) or {}
    output_schema = output_cfg.get("schema")

    # ── 共享的用户消息前置块（与单阶段内核保持一致的注入顺序） ──
    rendered = render_prompt_template(
        skill_cfg["prompt_template"], inputs, skill_cfg.get("inputs", {})
    )
    # 文档只读一次：正文同时用于消息注入与（无 item_definition 时的）检索 query
    logger.info(
        "[capability_registry] 分阶段技能开始（skill=%s）：读取上传文档（file_id=%s）",
        skill_cfg.get("name"), inputs.get("file_id") or "-",
    )
    doc_raw = _read_uploaded_document(skill_cfg, inputs)
    logger.info("[capability_registry] 读取上传文档完成：%d 字符", len(doc_raw or ""))
    doc_block = (
        "\n\n【相关项文档（Markdown，平台从用户上传文件转换，分析以此为主要事实来源）】\n"
        + doc_raw
    ) if doc_raw else ""
    doc_query = _document_query_text(doc_raw)
    ref_block = _build_reference_block(reference_data)
    schema_example_block = (
        _build_schema_example_block(skill_cfg) if output_cfg.get("format") == "json" else ""
    )

    base_llm = _build_model_with_hint(
        _resolve_model(agent_cfg.get("model")), skill_cfg.get("model_hint")
    )

    def _bind_json_model(max_tokens: Any) -> Any:
        bind_kwargs: dict[str, Any] = {
            "response_format": {"type": "json_object"},
            "timeout": SKILL_LLM_TIMEOUT_SECONDS,
        }
        if isinstance(max_tokens, int) and max_tokens > 0:
            bind_kwargs["max_tokens"] = max_tokens
        return base_llm.bind(**bind_kwargs)

    # ── Stage 0（可选）：技能侧 prepare 钩子（识别/自定义检索等业务逻辑） ──
    prepare_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    prepare_invoked = False
    plan_kb_block = ""
    # 技能侧对上传文档的确定性结构化补充（如 DOCX 功能清单直解析，
    # 修正平台 Markdown 表格转换的合并单元格错列）；紧随 doc_block 注入规划层
    plan_doc_supplement = ""
    prepare_ctx: PrepareContext | None = None
    if (exec_cfg.get("prepare") or {}).get("script"):
        from core.kb_client import format_knowledge_layered_block, retrieve_knowledge

        emit_skill_progress("正在识别功能项并检索历史知识…")
        logger.info(
            "[capability_registry] prepare 钩子开始（script=%s）",
            exec_cfg["prepare"].get("script"),
        )

        prepare_ctx = PrepareContext(
            skill_cfg=skill_cfg,
            inputs=inputs,
            rendered=rendered,
            doc_raw=doc_raw,
            doc_block=doc_block,
            runnable_config=runnable_config,
            logger=logger,
            bind_json_model=_bind_json_model,
            build_stage_prompt=lambda stage_cfg: _build_stage_system_prompt(skill_cfg, stage_cfg),
            invoke_json_stage=_invoke_json_stage,
            extract_usage=_extract_usage,
            retrieve_knowledge=retrieve_knowledge,
            format_knowledge_layered_block=format_knowledge_layered_block,
        )
        try:
            hook_result = _run_prepare_hook(prepare_ctx) or {}
            prepare_invoked = True
            plan_kb_block = str(hook_result.get("kb_block") or "")
            plan_doc_supplement = str(hook_result.get("doc_supplement") or "")
            hook_usage = hook_result.get("usage") or {}
            if isinstance(hook_usage, dict):
                for k in prepare_usage:
                    prepare_usage[k] = int(hook_usage.get(k, 0) or 0)
            logger.info(
                "[capability_registry] prepare 钩子知识注入：%d 字符；文档结构化补充：%d 字符",
                len(plan_kb_block), len(plan_doc_supplement),
            )
        except Exception as exc:  # noqa: BLE001 —— 前置钩子失败必须软降级，不阻断主流程
            logger.warning(
                "[capability_registry] prepare 钩子失败，回退粗召回：%s", exc
            )

    # 回退：无钩子 / 钩子异常 / 零命中 → item_definition 或文档头部粗召回
    if not plan_kb_block:
        plan_kb_block = _resolve_layered_knowledge_block(
            skill_cfg, inputs, fallback_query=doc_query
        )
        logger.info(
            "[capability_registry] 规划层知识注入（粗召回%s）：%d 字符",
            "回退" if prepare_invoked else "",
            len(plan_kb_block),
        )

    # ── 阶段 1：规划 ──
    plan_doc_supplement_block = (
        f"\n\n{plan_doc_supplement.strip()}\n" if plan_doc_supplement else ""
    )
    plan_user_message = (
        rendered
        + doc_block
        + plan_doc_supplement_block
        + plan_kb_block
        + ref_block
        + schema_example_block
    )
    plan_system_prompt = _build_stage_system_prompt(skill_cfg, plan_cfg)
    emit_skill_progress("正在规划：梳理功能、失效模式与场景清单…")
    logger.info(
        "[capability_registry] 规划层开始：注入 %d 字符（文档 %d + 结构化补充 %d "
        "+ 知识 %d），max_tokens=%s",
        len(plan_user_message), len(doc_block), len(plan_doc_supplement_block),
        len(plan_kb_block), plan_cfg.get("max_tokens"),
    )
    plan_parsed, plan_response = _invoke_json_stage(
        _bind_json_model(plan_cfg.get("max_tokens")),
        plan_system_prompt,
        plan_user_message,
        stage_name="规划层",
        max_tokens=plan_cfg.get("max_tokens"),
        runnable_config=runnable_config,
    )

    # ── 规划后处理（可选）：技能侧按 ref_id 回查知识库原文校正规划输出 ──
    if str((plan_cfg.get("postprocess_entrypoint") or "")).strip():
        try:
            from core.kb_client import retrieve_knowledge as _pp_retrieve

            pp_result = _run_plan_postprocess_hook(PlanPostprocessContext(
                skill_cfg=skill_cfg,
                plan_parsed=plan_parsed,
                inputs=inputs,
                logger=logger,
                retrieve_knowledge=_pp_retrieve,
            ))
            if isinstance(pp_result, dict):
                brief = {
                    k: v for k, v in pp_result.items()
                    if isinstance(v, (int, float, str))
                }
                logger.info("[capability_registry] 规划后处理完成：%s", brief)
        except Exception as exc:  # noqa: BLE001 —— 后处理失败软降级，保留规划原输出
            logger.warning(
                "[capability_registry] 规划后处理钩子失败（软降级保留原输出）：%s", exc
            )

    slices = plan_parsed.get(slice_path)
    if not isinstance(slices, list) or not slices:
        raise ValueError(
            f"规划层输出缺少非空列表 {slice_path!r}，无法进入并发处理阶段；"
            "请检查输入描述是否包含可分析的功能单元与充分的切片内容。"
        )
    funcs = plan_parsed.get("functions") or []
    matrix = plan_parsed.get("malfunction_matrix") or []
    logger.info(
        "[capability_registry] 规划层结果：%d 个待处理切片（%s）；"
        "functions=%d，malfunction_matrix=%d",
        len(slices), slice_path, len(funcs), len(matrix),
    )

    # ── 阶段 2：逐切片并发评级（单片失败重试 1 次） ──
    map_system_prompt = _build_stage_system_prompt(skill_cfg, map_cfg)
    map_max_tokens = map_cfg.get("max_tokens")
    emit_skill_progress(
        f"正在逐项评估严重度/暴露率/可控度（共 {len(slices)} 项，可并行）…"
    )
    logger.info(
        "[capability_registry] 评级阶段开始：%d 个切片，并发 %d，max_tokens=%s",
        len(slices), max_workers, map_max_tokens,
    )
    item_brief = plan_parsed.get("item") or {}
    item_header = (
        f"相关项名称：{item_brief.get('name', '')}；"
        f"缩写/域前缀：{item_brief.get('abbr', '')} / {item_brief.get('domain_prefix', '')}"
    )
    kb_cfg = skill_cfg.get("knowledge") or {}
    domain = str(kb_cfg.get("domain") or "").strip()
    map_layer = str(kb_cfg.get("map_layer") or "").strip() or None
    map_top_k = kb_cfg.get("map_top_k")
    query_fields = tuple(
        map_cfg.get("query_fields")
        or ("word", "malfunction_behavior", "vehicle_hazard")
    )
    user_instruction = str(map_cfg.get("user_instruction") or "").strip() or (
        "请按系统提示词要求处理以下切片中每一个待分析项，结果项数量与输入清单一致；"
        "只输出 JSON 对象。"
    )

    def _retrieve_slice_chunks(index: int, unit: dict) -> list:
        """每切片历史知识召回：优先技能侧钩子；未声明/异常时回退通用单路检索。"""
        from core.kb_client import retrieve_knowledge

        hook_ctx = MapSliceContext(
            skill_cfg=skill_cfg,
            index=index,
            unit=unit,
            plan_parsed=plan_parsed,
            inputs=inputs,
            runnable_config=runnable_config,
            logger=logger,
            retrieve_knowledge=retrieve_knowledge,
        )
        try:
            hook_chunks = _run_map_retrieve_hook(hook_ctx)
        except Exception as exc:  # noqa: BLE001 —— 钩子失败软降级到通用检索
            logger.warning(
                "[capability_registry] 评级切片 %d 检索钩子降级为通用检索：%s",
                index + 1, exc,
            )
            hook_chunks = None
        if hook_chunks is not None:
            return hook_chunks
        query = " ".join(
            str(unit.get(k) or "") for k in query_fields
        ).strip()
        return retrieve_knowledge(
            domain,
            query,
            top_k=map_top_k,
            score_threshold=kb_cfg.get("score_threshold"),
            layer=map_layer,
        )

    def _rate_one(index: int, unit: Any) -> tuple[int, list, dict]:
        if not isinstance(unit, dict):
            raise ValueError(f"第 {index + 1} 个评级切片不是 JSON 对象")
        unit_json = json.dumps(unit, ensure_ascii=False, indent=2)

        # 历史知识每切片只召回一次，重试复用同一批分块
        map_chunks: list = []
        if domain:
            try:
                map_chunks = _retrieve_slice_chunks(index, unit)
            except Exception as exc:  # noqa: BLE001 —— 知识注入永不阻断
                logger.warning(
                    "[capability_registry] 评级切片知识注入降级：%s", exc
                )

        def _attempt() -> tuple[list, dict]:
            map_kb_block = ""
            if map_chunks:
                from core.kb_client import format_knowledge_block

                map_kb_block = format_knowledge_block(map_chunks, domain)
            logger.info(
                "[capability_registry] 评级切片 %d（%s/%s）知识库命中 %d 块，注入 %d 字符",
                index + 1,
                unit.get("fid", ""),
                unit.get("word", ""),
                len(map_chunks),
                len(map_kb_block),
            )
            user_message = (
                f"{item_header}\n\n"
                f"{user_instruction}\n\n"
                f"```json\n{unit_json}\n```\n"
                + map_kb_block
                + schema_example_block
            )
            parsed, map_response = _invoke_json_stage(
                _bind_json_model(map_max_tokens),
                map_system_prompt,
                user_message,
                stage_name=f"评级层(切片{index + 1})",
                max_tokens=map_max_tokens,
                # 线程池内不传 runnable_config：避免日志回调跨线程并发写同一文件
            )
            events = parsed.get(events_key)
            if not isinstance(events, list):
                raise ValueError(
                    f"评级层(切片{index + 1})输出缺少列表字段 {events_key!r}"
                )

            # 评审钩子（技能侧代码级复核，疑似项回炉 LLM 二次校验）：
            # 只要技能声明了 review_entrypoint 就执行——部分复核（如显著事件
            # 缺少安全目标的补全）不依赖历史召回，chunks 允许为空；
            # 未声明入口时钩子返回 None；异常软降级保留原结果，usage 并入本切片。
            review_usage: dict = {}
            try:
                review_result = _run_map_review_hook(MapReviewContext(
                    skill_cfg=skill_cfg,
                    index=index,
                    unit=unit,
                    plan_parsed=plan_parsed,
                    events=events,
                    chunks=map_chunks,
                    logger=logger,
                    bind_json_model=_bind_json_model,
                    build_stage_prompt=lambda cfg: _build_stage_system_prompt(
                        skill_cfg, cfg
                    ),
                    invoke_json_stage=_invoke_json_stage,
                    extract_usage=_extract_usage,
                ))
            except Exception as exc:  # noqa: BLE001 —— 评审失败不阻断出表
                logger.warning(
                    "[capability_registry] 评级切片 %d 评审钩子降级（保留原结果）：%s",
                    index + 1, exc,
                )
            else:
                if isinstance(review_result, dict):
                    if isinstance(review_result.get("events"), list):
                        events = review_result["events"]
                    ru = review_result.get("usage")
                    if isinstance(ru, dict):
                        review_usage = ru
                elif isinstance(review_result, list):
                    events = review_result

            usage = _extract_usage(map_response)
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                usage[k] = int(usage.get(k, 0) or 0) + int(
                    review_usage.get(k, 0) or 0
                )
            return events, usage

        try:
            events, usage = _attempt()
            return index, events, usage
        except Exception as first_exc:  # noqa: BLE001 —— 单片统一重试 1 次
            logger.warning(
                "[capability_registry] 评级切片 %s 首次失败，重试一次：%s",
                index + 1, first_exc,
            )
            try:
                events, usage = _attempt()
                return index, events, usage
            except Exception as second_exc:
                raise ValueError(
                    f"评级切片 {index + 1}（{unit.get('fid', '')}/"
                    f"{unit.get('word', '')}）重试后仍失败：{second_exc}。"
                    "请缩小一次分析的功能/场景范围后重试。"
                ) from second_exc

    map_usage_sum = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    # 跨切片去重：同一历史事件（非空 ref_id）只允许被沿用一次，
    # 防止规划层把一个失效拆成多片时重复沿用同一条历史事件
    consumed_ref_ids: set[str] = set()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_rate_one, i, unit): i
            for i, unit in enumerate(slices)
        }
        errors: list[BaseException] = []
        done_count = 0
        for future in as_completed(futures):
            done_count += 1
            emit_skill_progress(
                f"正在逐项评估严重度/暴露率/可控度（已完成 {done_count}/{len(slices)} 项）…"
            )
            try:
                idx, events, map_usage = future.result()
                deduped: list = []
                dropped = 0
                for ev in events:
                    ref_id = ""
                    if isinstance(ev, dict):
                        src = ev.get("source")
                        if isinstance(src, dict):
                            ref_id = str(src.get("ref_id") or "").strip()
                    if ref_id:
                        if ref_id in consumed_ref_ids:
                            dropped += 1
                            continue
                        consumed_ref_ids.add(ref_id)
                    deduped.append(ev)
                if dropped:
                    logger.warning(
                        "[capability_registry] 评级切片 %d 有 %d 条事件因历史 ref_id "
                        "已被其他切片沿用而丢弃（疑似规划层重复切片）",
                        idx + 1, dropped,
                    )
                slices[idx][events_key] = deduped
                for k in map_usage_sum:
                    map_usage_sum[k] += int(map_usage.get(k, 0) or 0)
            except BaseException as exc:  # noqa: BLE001 —— 收集后统一抛出
                errors.append(exc)
    if errors:
        raise errors[0]

    # ── 合并 + schema 校验（切片即规划输出中的 hazop_items，事件已挂回） ──
    merged = plan_parsed
    raw_merged = json.dumps(merged, ensure_ascii=False)
    structured = _validate_structured_output(raw_merged, output_schema)

    plan_usage = _extract_usage(plan_response)
    total_usage = {
        "prompt_tokens": prepare_usage.get("prompt_tokens", 0)
        + plan_usage.get("prompt_tokens", 0) + map_usage_sum["prompt_tokens"],
        "completion_tokens": prepare_usage.get("completion_tokens", 0)
        + plan_usage.get("completion_tokens", 0) + map_usage_sum["completion_tokens"],
        "total_tokens": prepare_usage.get("total_tokens", 0)
        + plan_usage.get("total_tokens", 0) + map_usage_sum["total_tokens"],
        "stage_prepare_calls": 1 if prepare_invoked else 0,
        "stage_plan_calls": 1,
        "stage_map_calls": len(slices),
    }

    result = SkillResult(text=raw_merged, structured=structured, usage=total_usage)

    # ── 确定性渲染：JSON → 文件交付物（与单阶段内核一致） ──
    if output_cfg.get("format") == "json" and structured is not None:
        emit_skill_progress("正在汇总评估结果并生成交付文件…")
        artifact_meta, stats = _run_renderer(skill_cfg, structured)
        if artifact_meta is not None:
            from core.file_sandbox import resolve_stored_path

            artifact_path = resolve_stored_path(artifact_meta.file_id)
            result.artifacts = [{
                "file_id": artifact_meta.file_id,
                "filename": artifact_meta.original_name,
                "format": artifact_meta.ext.lstrip("."),
                "size": artifact_meta.size,
            }]
            result.text = _format_artifact_summary(
                skill_cfg, artifact_meta, stats, artifact_path, structured
            )
    return result


def _resolve_layered_knowledge_block(
    skill_cfg: dict,
    inputs: dict[str, Any],
    *,
    fallback_query: str = "",
) -> str:
    """map_reduce 规划层的知识注入：一次大召回 + 按 meta.layer 分组注入（软降级）。

    knowledge 扩展字段：
        plan_top_k: 40                         # 规划层大召回量
        layers: [function_list, ...]           # 分层分段顺序
    未声明 plan_top_k/layers 时回退普通单层注入。

    query 解析顺序：inputs[query_from]（用户显式描述）→ fallback_query
    （file_id-only 请求时由上传文档正文头部提供，否则 file_id 场景永远检索不到历史数据）。
    """
    kb_cfg = skill_cfg.get("knowledge") or {}
    if not isinstance(kb_cfg, dict) or not kb_cfg:
        return ""
    try:
        from core.kb_client import (
            format_knowledge_block,
            format_knowledge_layered_block,
            retrieve_knowledge,
        )

        domain = str(kb_cfg.get("domain") or "").strip()
        query_from = str(kb_cfg.get("query_from") or "").strip()
        raw_query = str(inputs.get(query_from) or "").strip() if query_from else ""
        query_source = "item_definition"
        if not raw_query:
            # file_id-only：回退到上传文档正文头部
            raw_query = str(fallback_query or "").strip()
            query_source = "上传文档正文头部"
        # 截断 query 避免 embedding 模型输入超限导致召回质量退化
        query = raw_query[:500] if len(raw_query) > 500 else raw_query
        if not domain or not query:
            return ""
        layers = kb_cfg.get("layers") or []
        plan_top_k = kb_cfg.get("plan_top_k")
        chunks = retrieve_knowledge(
            domain,
            query,
            top_k=plan_top_k if plan_top_k else kb_cfg.get("top_k"),
            score_threshold=kb_cfg.get("score_threshold"),
            meta_filter=kb_cfg.get("meta_filter") or None,
        )
        # 各层命中分布
        layer_counts: dict[str, int] = {}
        for c in chunks:
            ln = str((c.get("meta") or {}).get("layer") or "未标注")
            layer_counts[ln] = layer_counts.get(ln, 0) + 1
        logger.info(
            "[capability_registry] 规划层知识检索（query 来源=%s）：query=%r（原始 %d→截断 %d 字符），"
            "命中 %d 块，分布 %s",
            query_source,
            query[:80], len(raw_query), len(query), len(chunks), layer_counts,
        )
        if layers:
            layers = [str(x) for x in layers]
            return format_knowledge_layered_block(chunks, layers, domain)
        return format_knowledge_block(chunks, domain)
    except Exception as exc:  # noqa: BLE001 —— 注入过程任何异常都不阻断
        logger.warning(
            "[capability_registry] 技能 %s 分层知识注入降级：%s",
            skill_cfg.get("name"), exc,
        )
        return ""


def execute_skill(
    agent_name: str,
    skill_name: str,
    inputs: dict[str, Any],
    runnable_config: Any = None,
) -> str:
    """直接用某 Agent 的模型执行指定技能（LangGraph Executor 入口）。

    支持：system_prompt/reference_files 装配、document_source 先读文档、
    knowledge 历史项目参考注入、结构化 JSON 输出、output.renderer 确定性
    文件渲染（渲染产物时返回中文摘要）。
    """
    return _execute_skill_core(
        agent_name, skill_name, inputs, runnable_config=runnable_config
    ).text


# ════════════════════════════════════════════════════════════════
# 5B. 技能执行 v2（对外 API 使用：返回结构化结果 + usage + reference_data 注入）
# ════════════════════════════════════════════════════════════════

# reference_data 注入阈值：默认 512KB，可通过 AGENT_REFERENCE_DATA_MAX_KB 环境变量覆盖。
# 后端直连场景可能携带 100 条 UC/FR 快照，50KB 不够，故放宽到 512KB。
_REFERENCE_DATA_MAX_KB = int(os.getenv("AGENT_REFERENCE_DATA_MAX_KB", "512"))
REFERENCE_DATA_MAX_BYTES = _REFERENCE_DATA_MAX_KB * 1024


@dataclass
class SkillResult:
    """技能执行的统一返回结构（v2）。

    text:       面向用户的输出文本（渲染产物时为产物摘要，否则为 LLM 原始输出）
    structured: 按 output.schema 校验通过的对象；非结构化技能为 None
    usage:      token 使用统计（从 response.usage_metadata 提取）
    artifacts:  确定性渲染产物列表 [{file_id, filename, format, size}]
    """
    text: str
    structured: dict | list | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)


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

    # type 校验（支持单类型字符串或类型数组，如 ["integer", "null"]）
    expected_type = schema.get("type")
    if expected_type:
        type_map = {
            "object": dict, "array": list, "string": str,
            "number": (int, float), "integer": int, "boolean": bool,
            "null": type(None),
        }
        expected_types = expected_type if isinstance(expected_type, list) else [expected_type]
        py_types = tuple(
            t for pt in expected_types if (t := type_map.get(pt)) is not None
        )
        if py_types and not isinstance(instance, py_types):
            raise ValueError(
                f"{path} 类型应为 {'/'.join(map(str, expected_types))}，"
                f"实际为 {type(instance).__name__}"
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

    与 _execute_skill_core 共用同一执行链路：
      1. 返回 SkillResult(text, structured, usage, artifacts)；
      2. 支持 output.schema 结构化校验（json_mode + 轻量 schema 校验）；
      3. 支持 reference_data 注入（只读上下文追加到用户消息）；
      4. 支持 document_source（file_id 先读文档）、knowledge（auto-kb 历史参考注入）
         与 output.renderer（文件交付物）。

    Raises:
        ValueError: reference_data 超阈值 / 结构化输出未通过校验 / 渲染失败
        KeyError:   Agent/技能未在注册表绑定
    """
    return _execute_skill_core(
        agent_name,
        skill_name,
        inputs,
        reference_data=reference_data,
        runnable_config=runnable_config,
    )


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
