"""技能加载工具：从 skills/ 目录读取 YAML 格式的技能契约。

技能以 skills/**/<skill_name>.yaml 形式存在（支持按域分子文件夹，
如 skills/requirement/uc_analyze.yaml；能力注册表的同源文件），
新增技能只需新增 yaml 并在对应 Agent 的 skills 字段中声明。

本工具供 worker Agent 独立运行（未经过 Planner 直连）时使用：
Agent 调用 load_skill 拿到「输入说明 + prompt 模板」后自行组织执行。
若计划已由 Planner 指定 tool + skill，Executor 会直接执行技能，无需再调本工具。

用法（在 generate_agent.py 中）：
    tool = make_load_skill_tool(["feature_definition", "feature_expand"])
"""
from pathlib import Path

from langchain_core.tools import tool

# 本文件位于 <项目根>/tools/load_skill.py，技能目录固定为 <项目根>/skills
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = PROJECT_ROOT / "skills"


def _load_skill_cfg(skill_name: str) -> dict | None:
    """读取技能 yaml（递归查找 skills/**/<name>.yaml）；不存在返回 None。"""
    import yaml

    # 复用能力注册表的递归发现逻辑（重名检测、_template 排除保持一致）
    from agents.capability_registry import find_skill_path

    path = find_skill_path(skill_name)
    if path is None:
        return None
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _skill_summary(skill_name: str) -> str:
    """技能一句话摘要（来自 yaml 的 description）。"""
    cfg = _load_skill_cfg(skill_name)
    if cfg is None:
        return "（配置不存在）"
    return cfg.get("description", "（无描述）")


def _format_skill_content(cfg: dict) -> str:
    """把技能契约拼成给 LLM 阅读的内容：角色/输出格式(system_prompt) + 输入说明 + prompt 模板。

    注意：worker 独立运行时无法动态替换 system prompt，因此将 skill.system_prompt
    作为「必须遵循的角色与输出格式约束」置于返回内容顶部，优先级高于 agent 自身
    的 default_system_prompt，避免二者冲突误导模型。
    """
    lines = [f"技能：{cfg.get('display_name', cfg.get('name'))}"]

    skill_system = cfg.get("system_prompt")
    if skill_system:
        lines.append("")
        lines.append("【角色与输出格式约束（必须遵循，优先级高于 Agent 默认身份提示）】")
        lines.append(skill_system)

    lines.append("")
    lines.append("【输入参数】")
    for name, spec in (cfg.get("inputs") or {}).items():
        req = "必填" if spec.get("required") else "可选"
        lines.append(
            f"  - {name}（{spec.get('type', 'string')}，{req}）：{spec.get('desc', '')}"
        )
    output = cfg.get("output") or {}
    lines.append(f"输出形态：{output.get('format', 'plain_text')}")
    lines.append("")
    lines.append("【执行模板】请严格依据以下模板执行，模板中的 {{参数}} 用任务中的实际信息替换：")
    lines.append(cfg.get("prompt_template", ""))
    return "\n".join(lines)


def make_load_skill_tool(allowed_skills: list[str] | None = None):
    """创建一个 load_skill 工具实例。

    Args:
        allowed_skills: 允许加载的技能名列表。
                        为 None 时允许加载 skills/ 目录下全部 yaml 技能。

    Returns:
        LangChain Tool 实例，工具描述中动态列出可用技能。
    """
    # 确定可用技能列表
    if not allowed_skills:
        from agents.capability_registry import iter_skill_files

        allowed_skills = [p.stem for p in iter_skill_files()]

    skill_lines = "\n".join(
        f"    - {name}：{_skill_summary(name)}"
        for name in allowed_skills
    )

    @tool("load_skill", description=f"""加载专业技能契约。在执行专业任务之前，必须先调用本工具加载对应技能，
    并严格按照技能中规定的输入、输出模板完成任务。

    当前可用技能：
{skill_lines}

    参数：
        skill_name: 技能名称（不含 .yaml 后缀），例如 feature_definition
    """)
    def _load_skill(skill_name: str) -> str:
        # 限制只能加载 allowed_skills 中的技能
        if skill_name not in allowed_skills:
            return (
                f"技能 {skill_name} 不在当前 Agent 的可用技能列表中。"
                f"可用技能：{', '.join(allowed_skills) or '（空）'}"
            )
        cfg = _load_skill_cfg(skill_name)
        if cfg is None:
            return f"技能 {skill_name} 的配置文件不存在。"
        return _format_skill_content(cfg)

    return _load_skill
