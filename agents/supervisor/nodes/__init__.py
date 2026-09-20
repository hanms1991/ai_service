"""Supervisor 图的节点模块包。

按职责拆分：
- planner: Plan 子图（Plan 生成 + Schema 定义 + 提示词加载）
- executor: 执行节点（按计划三分支调度）
- helpers: Executor 的辅助函数（worker 调用、引用解析、中枢自处理）
- router: 条件边
"""
