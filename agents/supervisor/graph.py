"""顶层 Graph 串联：planner → executor → [route_after_executor]。

本文件只做"节点 + 边 + checkpointer"的组装，不含任何节点逻辑。
改节点逻辑 → 改 nodes/ 下对应文件；改文案 → 改 prompts/ 下 yaml。
"""
from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from agents.supervisor.nodes.executor import executor_node
from agents.supervisor.nodes.planner import planner_node
from agents.supervisor.nodes.router import route_after_executor
from agents.supervisor.registry_init import init_capability_registry
from agents.supervisor.state import SupervisorState


def build_supervisor_graph(config: dict, base_dir=None, checkpointer: Any = None) -> Any:
    """构建规划-执行分离的 Supervisor LangGraph。

    Args:
        config:   supervisor YAML 配置字典
        base_dir: YAML 文件所在目录（用于解析 system_prompt 外部文件引用）
        checkpointer: 可注入的 LangGraph checkpointer（默认 None → 内部 MemorySaver）。
                      FastAPI 启动时注入 AsyncSqliteSaver 实现跨进程持久化，
                      test/test.py 等独立运行场景保持默认 MemorySaver 不变。

    Returns:
        编译后的 CompiledStateGraph，支持 invoke / stream，
        内置 Checkpointer 支持多轮记忆。
        调用时需传 config={"configurable": {"thread_id": "xxx"}}。
    """
    # 初始化能力注册表（agents/configs + skills yaml 绑定校验，planner 目录据此生成）
    init_capability_registry(config)

    # ── 构建 StateGraph ──
    graph = StateGraph(SupervisorState)

    # 添加节点
    graph.add_node("planner", planner_node)
    graph.add_node("executor", executor_node)

    # 添加边
    graph.add_edge(START, "planner")          # 入口 → planner
    graph.add_edge("planner", "executor")      # planner → executor
    graph.add_conditional_edges(              # executor → 条件路由
        "executor",
        route_after_executor,
        # 路由目标：继续执行 or 结束
        {
            "executor": "executor",
            END: END,
        },
    )

    # ── 编译图，带 Checkpointer 支持多轮记忆 ──
    # 未注入时回退到 MemorySaver（保持向后兼容 test.py）；API 启动时注入 AsyncSqliteSaver
    if checkpointer is None:
        checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)
