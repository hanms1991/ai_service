"""兼容层：原 supervisor_graph.py 已按职责拆分到 agents/supervisor/ 包。

本文件仅 re-export build_supervisor_graph，保持外部 3 处 import 零改动：
  - api/main.py（lifespan 构建图）
  - agents/generate_agent.py（build_agent type=supervisor 分支）
  - test/test_api.py（readyz 字符串检查）

代码结构变更详见：.trae/documents/supervisor_graph_模块化拆分.md
"""
from agents.supervisor import build_supervisor_graph

__all__ = ["build_supervisor_graph"]
