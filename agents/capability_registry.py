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
import os
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


def _resolve_document_block(skill_cfg: dict, inputs: dict[str, Any]) -> str:
    """按 document_source 声明读取用户上传文档，返回追加到用户消息的只读段落。

    document_source:
        file_id_input: file_id   # inputs 中承载 file_id 的字段名
    读取失败（文件过期/损坏）抛 SkillInputError，由 Executor 转为对话式提示。
    """
    source_cfg = skill_cfg.get("document_source") or {}
    file_id_input = source_cfg.get("file_id_input")
    if not file_id_input:
        return ""
    file_id = str(inputs.get(file_id_input) or "").strip()
    if not file_id:
        return ""

    from tools.read_document import read_document

    content = read_document.invoke({"file_id": file_id})
    if not isinstance(content, str) or content.startswith("[读取失败]") or content.startswith("[读取成功但内容为空]"):
        raise SkillInputError(
            f"无法读取 file_id={file_id} 对应的文档：{content[:200]}。"
            "请提示用户重新上传文档（docx/xlsx/pptx/pdf 等），或直接用文字描述相关项信息。"
        )
    # content 已自带文件名/长度头，整体作为只读文档段落
    return "\n\n【相关项文档（Markdown，平台从用户上传文件转换，分析以此为主要事实来源）】\n" + content


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


def _extract_safety_goals(artifact_path: Path, limit: int = 15) -> list[dict]:
    """从生成的 HARA 工作簿 13_Safety_Goals 表读取安全目标摘要（尽力而为）。"""
    try:
        import openpyxl

        wb = openpyxl.load_workbook(artifact_path, read_only=True, data_only=False)
        if "13_Safety_Goals" not in wb.sheetnames:
            return []
        ws = wb["13_Safety_Goals"]
        goals: list[dict] = []
        for row in ws.iter_rows(min_row=5, values_only=True):
            sg_id = row[0] if len(row) > 0 else None
            if not sg_id or not str(sg_id).startswith("SG-"):
                break
            goals.append({
                "sg_id": sg_id,
                "function": row[1] if len(row) > 1 else "",
                "asil": row[3] if len(row) > 3 else "",
                "goal": row[5] if len(row) > 5 else "",
            })
            if len(goals) >= limit:
                break
        wb.close()
        return goals
    except Exception:
        return []


def _format_artifact_summary(
    skill_cfg: dict, meta: Any, stats: dict, artifact_path: Path | None
) -> str:
    """把渲染产物组织成面向用户的中文摘要（替代裸 JSON 输出）。"""
    name = skill_cfg.get("display_name") or skill_cfg.get("name")
    lines = [
        f"## {name}已完成",
        "",
        f"**交付物**：{meta.original_name}（{meta.size / 1024:.1f} KB）",
        f"文件 ID：`{meta.file_id}`",
        f"下载方式：`GET /api/v1/agent/files/{meta.file_id}/download`（需携带 API Key）",
        "",
    ]
    if stats:
        stat_labels = {
            "functions": "功能数",
            "function_malfunction_pairs": "功能×失效组合",
            "safety_critical_pairs": "安全关键（SC）组合",
            "hara_rows": "HARA 工况行（笛卡尔展开）",
            "significant_rows": "ASIL≥A 显著行",
            "safety_goals": "安全目标数",
        }
        lines.append("**工作簿统计**：")
        for key, label in stat_labels.items():
            if key in stats:
                lines.append(f"- {label}：{stats[key]}")
        lines.append("")

    if skill_cfg.get("name") == "hazard_analysis" and artifact_path is not None:
        goals = _extract_safety_goals(artifact_path)
        if goals:
            lines.append(f"**安全目标清单（按最高 ASIL 排序，前 {len(goals)} 条，全量见工作簿）**：")
            lines.append("")
            lines.append("| SG ID | ASIL | 功能 | 安全目标 |")
            lines.append("|---|---|---|---|")
            for g in goals:
                goal_text = str(g["goal"]).replace("|", "／").replace("\n", " ")
                lines.append(f"| {g['sg_id']} | {g['asil']} | {g['function']} | {goal_text} |")
            lines.append("")

    lines.append(
        "> 工作簿含封面、假设、架构边界、功能清单、M01–M14 失效词、S/E/C 参考表、"
        "ASIL 矩阵、功能×失效过滤表（全量保留可审计）、笛卡尔 HARA 工作表"
        "（ASIL 为活公式，修改 S/E/C 后自动重算）、安全目标与 FSC 交接表。"
        "自动 S/E/C 评级均为建议值，请逐条复核理由列后由责任人签署确认。"
    )
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
      3. 组装 system_prompt（skill + reference_files）；
      4. 结构化技能：json_mode 调用 → schema 轻量校验；
      5. output.renderer：JSON → 确定性文件渲染 → 落文件沙箱 → 中文摘要；
      6. 返回 SkillResult（text/structured/usage/artifacts）。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from agents.generate_agent import _resolve_model

    skill_cfg = validate_binding(agent_name, skill_name)
    agent_cfg = load_agent_config(agent_name)

    rendered = render_prompt_template(
        skill_cfg["prompt_template"],
        inputs or {},
        skill_cfg.get("inputs", {}),
    )

    # ── 执行链前置：读取上传文档（file_id → Markdown 段落） ──
    doc_block = _resolve_document_block(skill_cfg, inputs or {})

    # ── 外部 reference_data 注入（API 后台预取资料） ──
    ref_block = _build_reference_block(reference_data)

    # ── 输出 JSON Schema 说明与示例段落 ──
    output_cfg = skill_cfg.get("output", {}) or {}
    output_format = output_cfg.get("format", "plain_text")
    output_schema = output_cfg.get("schema")
    schema_example_block = (
        _build_schema_example_block(skill_cfg) if output_format == "json" else ""
    )

    user_message = rendered + doc_block + ref_block + schema_example_block

    # ── model_hint：叠加 skill 的推理开关/档位等 ──
    llm = _build_model_with_hint(
        _resolve_model(agent_cfg.get("model")),
        skill_cfg.get("model_hint"),
    )

    system_prompt = _build_skill_system_prompt(skill_cfg, agent_cfg)

    invoke_model = llm
    if output_format == "json" and output_schema:
        # json_mode + 技能级输出上限（HARA 等大 JSON 技能必须高于全局 LLM_MAX_TOKENS，
        # 否则可见输出在 max_tokens 处被截断，JSON 不闭合 → 解析失败）
        bind_kwargs: dict[str, Any] = {"response_format": {"type": "json_object"}}
        skill_max_tokens = output_cfg.get("max_tokens")
        if isinstance(skill_max_tokens, int) and skill_max_tokens > 0:
            bind_kwargs["max_tokens"] = skill_max_tokens
        invoke_model = llm.bind(**bind_kwargs)

    invoke_kwargs: dict[str, Any] = {}
    if runnable_config is not None:
        invoke_kwargs["config"] = runnable_config

    response = invoke_model.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=user_message)],
        **invoke_kwargs,
    )
    raw_text = response.content if isinstance(response.content, str) else str(response.content)

    # 截断预检：finish_reason=length 时 JSON 必然不完整，直接给出可操作的错误
    finish_reason = ""
    resp_meta = getattr(response, "response_metadata", None)
    if isinstance(resp_meta, dict):
        finish_reason = str(resp_meta.get("finish_reason") or "").lower()
    if finish_reason == "length":
        limit = output_cfg.get("max_tokens") or os.getenv("LLM_MAX_TOKENS", "?")
        raise ValueError(
            f"模型输出达到 token 上限（max_tokens={limit}）被截断，JSON 不完整。"
            f"已生成 {len(raw_text)} 字符；请在技能契约 output.max_tokens 中提高上限后重试。"
        )

    structured = _validate_structured_output(raw_text, output_schema)
    usage = _extract_usage(response)

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
            result.text = _format_artifact_summary(skill_cfg, meta, stats, artifact_path)

    return result


def execute_skill(
    agent_name: str,
    skill_name: str,
    inputs: dict[str, Any],
    runnable_config: Any = None,
) -> str:
    """直接用某 Agent 的模型执行指定技能（LangGraph Executor 入口）。

    支持：system_prompt/reference_files 装配、document_source 先读文档、
    结构化 JSON 输出、output.renderer 确定性文件渲染（渲染产物时返回中文摘要）。
    """
    return _execute_skill_core(
        agent_name, skill_name, inputs, runnable_config=runnable_config
    ).text


# ════════════════════════════════════════════════════════════════
# 5B. 技能执行 v2（对外 API 使用：返回结构化结果 + usage + reference_data 注入）
# ════════════════════════════════════════════════════════════════

# reference_data 注入阈值：50KB（硬阈值，超限直接报错，不做摘要降级）
REFERENCE_DATA_MAX_BYTES = 50 * 1024


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

    与 _execute_skill_core 共用同一执行链路：
      1. 返回 SkillResult(text, structured, usage, artifacts)；
      2. 支持 output.schema 结构化校验（json_mode + 轻量 schema 校验）；
      3. 支持 reference_data 注入（只读上下文追加到用户消息）；
      4. 支持 document_source（file_id 先读文档）与 output.renderer（文件交付物）。

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
