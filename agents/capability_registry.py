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

logger = logging.getLogger(__name__)


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


def _resolve_knowledge_block(skill_cfg: dict, inputs: dict[str, Any]) -> str:
    """按 knowledge 声明检索 auto-kb 历史项目参考，返回追加到用户消息的只读段落。

    knowledge:
        domain: 功能安全             # 领域名，对应 agents/configs/knowledge.yaml 的 domains
        query_from: item_definition  # 用哪个输入参数构造检索 query
        top_k: 5                     # 可选，默认 5
        score_threshold: 0.3         # 可选
        meta_filter: {车型: X}       # 可选，文档级元数据过滤

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
        query = str(inputs.get(query_from) or "").strip() if query_from else ""
        if not domain or not query:
            # 未声明 domain/query_from，或核心参数为空（如用户只上传了文档未直述）→ 跳过
            return ""
        chunks = retrieve_knowledge(
            domain,
            query,
            top_k=kb_cfg.get("top_k"),
            score_threshold=kb_cfg.get("score_threshold"),
            meta_filter=kb_cfg.get("meta_filter") or None,
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


def _extract_safety_goals(artifact_path: Path, limit: int = 15) -> list[dict]:
    """从团队标准 HARA 工作簿「整车安全目标」表右区读取合并后的整车安全目标（尽力而为）。

    右区表头在 R4：G 序号 / H 整车安全目标ID / I 安全目标合并 / J ASIL /
    K Safe State / L FTTI / M 备注；数据从 R5 起。
    """
    try:
        import openpyxl

        wb = openpyxl.load_workbook(artifact_path, read_only=True, data_only=False)
        if "整车安全目标" not in wb.sheetnames:
            return []
        ws = wb["整车安全目标"]
        goals: list[dict] = []
        for row in ws.iter_rows(min_row=5, values_only=True):
            vh_id = row[7] if len(row) > 7 else None  # H 列
            if not vh_id or not str(vh_id).strip():
                continue
            goals.append({
                "sg_id": vh_id,
                "asil": row[9] if len(row) > 9 else "",   # J 列
                "goal": row[8] if len(row) > 8 else "",   # I 列
            })
        wb.close()
        rank = {"D": 4, "C": 3, "B": 2, "A": 1, "QM": 0}
        goals.sort(key=lambda g: rank.get(str(g["asil"]).strip(), -1), reverse=True)
        return goals[:limit]
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
            "functions": "整车功能数",
            "sc_malfunctions": "安全关键失效条目",
            "hara_events": "HARA 危害事件数",
            "events_qm": "QM 事件",
            "events_asil": "ASIL≥A 显著事件",
            "safety_goals_vh": "整车安全目标数（合并后）",
            "reused": "沿用历史条目",
            "adapted": "改编历史条目",
            "new": "新增条目",
        }
        lines.append("**工作簿统计**：")
        for key, label in stat_labels.items():
            if key in stats:
                lines.append(f"- {label}：{stats[key]}")
        lines.append("")

    if skill_cfg.get("name") == "hazard_analysis" and artifact_path is not None:
        goals = _extract_safety_goals(artifact_path)
        if goals:
            lines.append(f"**整车安全目标清单（按最高 ASIL 排序，前 {len(goals)} 条，全量见工作簿）**：")
            lines.append("")
            lines.append("| 整车安全目标 ID | ASIL | 安全目标 |")
            lines.append("|---|---|---|")
            for g in goals:
                goal_text = str(g["goal"]).replace("|", "／").replace("\n", " ")
                lines.append(f"| {g['sg_id']} | {g['asil']} | {goal_text} |")
            lines.append("")

    lines.append(
        "> 工作簿为团队标准 11-Sheet 模板：版本管理、命名规则、相关项功能清单、"
        "失效模式（11 个标准失效词）、HAZOP 分析、HARA 分析（逐场景 S/E/C 评级，"
        "ASIL 按 ISO 26262 矩阵确定性反算）、整车安全目标（同文本目标自动合并取最高 ASIL）、"
        "参考场景与 S/E/C/ASIL 评定参考。历史沿用情况见各 sheet 备注列；"
        "所有 S/E/C 评级均为建议值，请逐条复核理由列后由责任人签署确认。"
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

    # ── 两阶段 map_reduce 技能（规划层 1 次 + 切片并发评级 N 次 → 合并 → 渲染） ──
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

    # ── 执行链前置：读取上传文档（file_id → Markdown 段落） ──
    doc_block = _resolve_document_block(skill_cfg, inputs or {})

    # ── 知识注入：按 knowledge 声明检索 auto-kb 历史项目参考（软降级，失败即空串） ──
    kb_block = _resolve_knowledge_block(skill_cfg, inputs or {})

    # ── 外部 reference_data 注入（API 后台预取资料） ──
    ref_block = _build_reference_block(reference_data)

    # ── 输出 JSON Schema 说明与示例段落 ──
    output_cfg = skill_cfg.get("output", {}) or {}
    output_format = output_cfg.get("format", "plain_text")
    output_schema = output_cfg.get("schema")
    schema_example_block = (
        _build_schema_example_block(skill_cfg) if output_format == "json" else ""
    )

    user_message = rendered + doc_block + kb_block + ref_block + schema_example_block

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


# ════════════════════════════════════════════════════════════════
# 5A2. 两阶段 map_reduce 执行（规划层 + 切片并发评级，供长输出技能使用）
# ════════════════════════════════════════════════════════════════

def _build_stage_system_prompt(skill_cfg: dict, stage_spec: Any) -> str:
    """组装分阶段技能某一阶段的 system prompt（阶段提示 + 技能 reference_files）。"""
    from agents.generate_agent import _resolve_system_prompt

    return (
        _resolve_system_prompt(stage_spec, _skill_base_dir(skill_cfg))
        + _load_reference_texts(skill_cfg)
    )


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

    invoke_kwargs: dict[str, Any] = {}
    if runnable_config is not None:
        invoke_kwargs["config"] = runnable_config
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
    return parsed, response


def _execute_staged_skill(
    skill_cfg: dict,
    agent_cfg: dict,
    inputs: dict[str, Any],
    *,
    reference_data: dict[str, Any] | None = None,
    runnable_config: Any = None,
) -> SkillResult:
    """map_reduce 两阶段执行内核。

    契约（技能 YAML）：
        execution:
          mode: map_reduce
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
      1. 规划层（1 次调用）：item/功能/失效矩阵/HAZOP 条目（含充分场景清单）；
         知识注入为四层分组大召回；
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
    doc_block = _resolve_document_block(skill_cfg, inputs)
    ref_block = _build_reference_block(reference_data)
    schema_example_block = (
        _build_schema_example_block(skill_cfg) if output_cfg.get("format") == "json" else ""
    )

    # ── 规划层知识注入：四层分组大召回（软降级） ──
    plan_kb_block = _resolve_layered_knowledge_block(skill_cfg, inputs)

    base_llm = _build_model_with_hint(
        _resolve_model(agent_cfg.get("model")), skill_cfg.get("model_hint")
    )

    def _bind_json_model(max_tokens: Any) -> Any:
        bind_kwargs: dict[str, Any] = {"response_format": {"type": "json_object"}}
        if isinstance(max_tokens, int) and max_tokens > 0:
            bind_kwargs["max_tokens"] = max_tokens
        return base_llm.bind(**bind_kwargs)

    # ── 阶段 1：规划 ──
    plan_user_message = (
        rendered + doc_block + plan_kb_block + ref_block + schema_example_block
    )
    plan_system_prompt = _build_stage_system_prompt(
        skill_cfg, plan_cfg.get("system_prompt")
    )
    plan_parsed, plan_response = _invoke_json_stage(
        _bind_json_model(plan_cfg.get("max_tokens")),
        plan_system_prompt,
        plan_user_message,
        stage_name="规划层",
        max_tokens=plan_cfg.get("max_tokens"),
        runnable_config=runnable_config,
    )

    slices = plan_parsed.get(slice_path)
    if not isinstance(slices, list) or not slices:
        raise ValueError(
            f"规划层输出缺少非空列表 {slice_path!r}，无法进入评级阶段；"
            "请检查相关项描述是否包含可分析的整车功能与安全关键失效。"
        )

    # ── 阶段 2：逐切片并发评级（单片失败重试 1 次） ──
    map_system_prompt = _build_stage_system_prompt(
        skill_cfg, map_cfg.get("system_prompt")
    )
    map_max_tokens = map_cfg.get("max_tokens")
    item_brief = plan_parsed.get("item") or {}
    item_header = (
        f"相关项名称：{item_brief.get('name', '')}；"
        f"缩写/域前缀：{item_brief.get('abbr', '')} / {item_brief.get('domain_prefix', '')}"
    )
    kb_cfg = skill_cfg.get("knowledge") or {}
    domain = str(kb_cfg.get("domain") or "").strip()
    map_layer = str(kb_cfg.get("map_layer") or "").strip() or None
    map_top_k = kb_cfg.get("map_top_k")

    def _rate_one(index: int, unit: Any) -> tuple[int, list, dict]:
        if not isinstance(unit, dict):
            raise ValueError(f"第 {index + 1} 个评级切片不是 JSON 对象")
        unit_json = json.dumps(unit, ensure_ascii=False, indent=2)

        def _attempt() -> tuple[list, dict]:
            # 每切片按其危害关键词小召回历史 HARA 事件（软降级，失败为空）
            map_kb_block = ""
            if domain:
                try:
                    from core.kb_client import format_knowledge_block, retrieve_knowledge

                    query = " ".join(
                        str(unit.get(k) or "")
                        for k in ("word", "malfunction_behavior", "vehicle_hazard")
                    ).strip()
                    chunks = retrieve_knowledge(
                        domain,
                        query,
                        top_k=map_top_k,
                        score_threshold=kb_cfg.get("score_threshold"),
                        layer=map_layer,
                    )
                    map_kb_block = format_knowledge_block(chunks, domain)
                except Exception as exc:  # noqa: BLE001 —— 知识注入永不阻断
                    logger.warning(
                        "[capability_registry] 评级切片知识注入降级：%s", exc
                    )
            user_message = (
                f"{item_header}\n\n"
                "以下是一个「功能失效单元」，其中 scenarios 已给出该失效需要分析的"
                "【完整场景清单】。请对清单中每一个场景输出一条 HARA 事件，"
                "禁止遗漏、禁止自行增删场景；只输出 JSON 对象。\n\n"
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
            return events, _extract_usage(map_response)

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
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_rate_one, i, unit): i
            for i, unit in enumerate(slices)
        }
        errors: list[BaseException] = []
        for future in as_completed(futures):
            try:
                idx, events, map_usage = future.result()
                slices[idx][events_key] = events
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
        "prompt_tokens": plan_usage.get("prompt_tokens", 0) + map_usage_sum["prompt_tokens"],
        "completion_tokens": plan_usage.get("completion_tokens", 0) + map_usage_sum["completion_tokens"],
        "total_tokens": plan_usage.get("total_tokens", 0) + map_usage_sum["total_tokens"],
        "stage_plan_calls": 1,
        "stage_map_calls": len(slices),
    }

    result = SkillResult(text=raw_merged, structured=structured, usage=total_usage)

    # ── 确定性渲染：JSON → 文件交付物（与单阶段内核一致） ──
    if output_cfg.get("format") == "json" and structured is not None:
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
                skill_cfg, artifact_meta, stats, artifact_path
            )
    return result


def _resolve_layered_knowledge_block(skill_cfg: dict, inputs: dict[str, Any]) -> str:
    """map_reduce 规划层的知识注入：一次大召回 + 按 meta.layer 分组注入（软降级）。

    knowledge 扩展字段：
        plan_top_k: 40                         # 规划层大召回量
        layers: [function_list, ...]           # 分层分段顺序
    未声明 plan_top_k/layers 时回退普通单层注入。
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
        query = str(inputs.get(query_from) or "").strip() if query_from else ""
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
