"""从 supervisor YAML 初始化能力注册表与额外指令。

独立成模块的原因：让 graph.py 不必直接持有可变全局状态，
把"配置 → 全局状态"的写入职责单独封装。
"""
from __future__ import annotations

from agents.supervisor import constants


def init_capability_registry(config: dict) -> None:
    """从 supervisor YAML 的成员清单初始化能力注册表（含 Agent↔技能绑定与校验）。

    同时把 supervisor YAML 的 system_prompt 字段写入 constants._EXTRA_INSTRUCTIONS，
    作为 planner 的额外补充指令。
    """
    from agents.capability_registry import init_registry_from_supervisor

    init_registry_from_supervisor(config)
    # 跨模块共享可变全局：必须修改 constants 模块对象属性，不是局部 `global`
    constants._EXTRA_INSTRUCTIONS = config.get("system_prompt", "") or ""
