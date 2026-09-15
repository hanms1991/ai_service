"""中枢智能体（supervisor_agent）。

职责：理解用户意图，把任务调度（delegate）给合适的专业子智能体，
并将子智能体的产出作为最终答复返回给用户。

基于 LangChain 1.x 的 create_agent 构建。子智能体通过 @tool 包装为普通工具：
对子智能体而言一次完整的 invoke()，对中枢 LLM 来说就是一次工具调用。
新增子智能体（如 hara_agent、test_agent）时，按同样方式包装并加入 tools 即可。
"""
from langchain.agents import create_agent
from langchain.tools import tool

from agents.product_agent import product_agent
from core.llm import model


@tool
def product_expert(query: str) -> str:
    """产品定义专家，负责汽车电子功能的结构化定义与描述，输出内容包括：功能概述、
    功能边界、使能条件、触发条件、执行输出、退出/抑制条件、输入输出信号、
    异常降级及法规安全关注。
    当用户要求定义、梳理或描述某个汽车功能（例如 AEB、ACC、LKA、APA 等）时调用本工具。

    参数：
        query: 转达给产品定义专家的完整任务描述，需包含功能名称及用户的具体诉求
    """
    result = product_agent.invoke(
        {"messages": [{"role": "user", "content": query}]}
    )
    # create_agent 返回状态中最后一条消息即为子智能体的最终自然语言输出
    return result["messages"][-1].content


supervisor_agent = create_agent(
    model=model,
    tools=[product_expert],
    system_prompt=(
        "你是汽车 EEA 智能平台的中枢协调者（Supervisor）。\n"
        "你不直接生产专业内容，而是理解用户意图并调度专业子智能体：\n"
        "- 功能定义 / 产品需求类任务 -> 调用 product_expert\n"
        "工作要求：\n"
        "1. 准确判断意图，把用户的原始诉求（功能名称、场景、约束）完整转达给子智能体；\n"
        "2. 拿到子智能体的结果后，保持其专业结构与内容完整，作为最终答复呈现给用户；\n"
        "3. 不要自行编造专业内容，也不要省略子智能体结果中的关键章节。"
    ),
)
