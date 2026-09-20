"""中枢 LangGraph Supervisor 包。

代码结构（按职责解耦）：
- state.py:        SupervisorState 图全局状态
- constants.py:    HIGH_RISK_MODES + _EXTRA_INSTRUCTIONS 共享变量
- registry_init.py: 能力注册表初始化
- nodes/planner.py: Plan 子图（PlanStep/PlanSchema/planner_node + 提示词加载）
- nodes/helpers.py: Executor 辅助函数（_resolve_input/_call_worker/_self_handle）
- nodes/executor.py: Executor 节点（三分支调度）
- nodes/router.py:  条件边 route_after_executor
- graph.py:        build_supervisor_graph 顶层串联
- prompts/*.yaml:   节点提示词配置（改文案只动这里）

外部入口统一为 build_supervisor_graph（外部 3 处 import 都通过本入口）。
"""
from agents.supervisor.graph import build_supervisor_graph

__all__ = ["build_supervisor_graph"]
