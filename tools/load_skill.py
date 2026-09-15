"""技能加载工具：从 skills/ 目录读取 Markdown 格式的专业技能提示词。

技能以 Markdown 文件形式存在，新增技能只需在 skills/ 下新增 <skill_name>.md，
无需改动任何注册代码。使用 LangChain 的 @tool 装饰器，
函数 docstring 即为 LLM 可见的工具描述。
"""
from pathlib import Path

from langchain.tools import tool

# 本文件位于 <项目根>/tools/load_skill.py，技能目录固定为 <项目根>/skills
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = PROJECT_ROOT / "skills"


@tool
def load_skill(skill_name: str) -> str:
    """加载专业技能提示词。在执行任何专业任务之前，必须先调用本工具加载对应技能，
    并严格按照技能中规定的方法与输出模板完成任务。

    当前可用技能：
    - feature_definition：汽车功能定义专家，用于汽车电子功能（如 AEB、ACC、LKA、APA 等）的结构化定义。

    参数：
        skill_name: 技能名称（不含 .md 后缀），例如 feature_definition
    """
    skill_path = SKILLS_DIR / f"{skill_name}.md"
    if not skill_path.exists():
        available = [p.stem for p in SKILLS_DIR.glob("*.md")]
        return f"技能 {skill_name} 不存在。当前可用技能：{', '.join(available) or '（空）'}"
    return skill_path.read_text(encoding="utf-8")
