"""产品定义子智能体（product_agent）。

能力：汽车电子功能定义。
技能：feature_definition（位于 skills/feature_definition.md，通过 load_skill 工具加载）。

基于 LangChain 1.x 的 create_agent 构建（返回 LangGraph 智能体图）。
后续扩展新技能（如 PRD 写作、干系人分析）时，只需在 skills/ 下增加 md 文件，
并同步更新 load_skill 工具描述与系统提示中的可用技能清单。
"""
from langchain.agents import create_agent

from core.llm import model
from tools.load_skill import load_skill

product_agent = create_agent(
    model=model,
    tools=[load_skill],
    system_prompt=(
        "你是汽车 EEA（电子电气架构）产品定义专家，负责把用户简短的功能诉求"
        "转化为结构化、专业、可落地的功能定义。\n"
        "工作规则：\n"
        "1. 接到功能定义类任务时，必须先调用 load_skill 工具加载 feature_definition 技能；\n"
        "2. 严格按照技能中规定的工作方法与输出模板组织内容，章节号保持一致；\n"
        "3. 使用规范的汽车工程术语，无法确定的阈值用“<待标定>”、不确定项用“<待确认>”标注；\n"
        "4. 直接输出功能定义正文，不要解释你的调用过程，也不要输出与任务无关的寒暄。"
    ),
)
