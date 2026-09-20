"""Supervisor 包内共享常量与运行时可变全局变量。

跨模块共享方式：
- HIGH_RISK_MODES 是只读集合，直接 from agents.supervisor.constants import HIGH_RISK_MODES
- _EXTRA_INSTRUCTIONS 是运行时可写的模块级变量，必须通过
  `import agents.supervisor.constants as const` 然后访问 `const._EXTRA_INSTRUCTIONS`，
  否则 `from ... import _EXTRA_INSTRUCTIONS` 会拿到早期绑定的快照，
  registry_init.py 对它的修改不会反映到其他模块。
"""
from __future__ import annotations


# 需要人工审批的模式
HIGH_RISK_MODES = {"confirm"}

# supervisor YAML 中的 system_prompt（分派规则），作为 planner 额外指令。
# 启动期由 registry_init.init_capability_registry 写入。
_EXTRA_INSTRUCTIONS: str = ""
