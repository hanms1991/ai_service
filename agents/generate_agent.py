"""根据 YAML 配置生成 LangChain Agent。

设计要点：
- 所有依赖（模型 / 工具）都用 "module.path:attr" 字符串描述，运行时动态导入；
- system_prompt 支持两种写法：内联字符串，或 {"file": "xxx.md"} 引用外部文件
  （相对路径相对于 YAML 文件所在目录解析）；
- 支持单文件加载和目录批量加载两种入口。

用法：
    from generate_agent import load_agent, load_agents_from_dir
    product_agent = load_agent("agents/configs/product_agent.yaml")
    agents = load_agents_from_dir("agents/configs")
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Annotated, Any, Callable

import yaml
from langchain.agents import create_agent
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg

DEFAULT_MODEL_PATH = "core.llm:model"


# ---------- 基础工具 ----------
def _import_from_path(path: str) -> Any:
    """从 'module.submodule:attr' 形式的路径导入对象。"""
    if ":" not in path:
        raise ValueError(f"非法导入路径 {path!r}，应为 'module:attr' 形式")
    module_path, attr = path.split(":", 1)
    module = importlib.import_module(module_path)
    return getattr(module, attr)


def _resolve_model(spec: Any) -> Any:
    """model 字段：
      - 省略 / None         -> 默认 core.llm:model
      - "core.llm:model"    -> 按路径导入
      - {"path": "...", ...}-> 预留：未来可传入 kwargs 给 init_chat_model 之类
    """
    if spec is None:
        return _import_from_path(DEFAULT_MODEL_PATH)
    if isinstance(spec, str):
        return _import_from_path(spec)
    if isinstance(spec, dict) and "path" in spec:
        obj = _import_from_path(spec["path"])
        # 若 obj 是工厂函数且提供了 kwargs，则调用它
        kwargs = spec.get("kwargs")
        return obj(**kwargs) if kwargs else obj
    raise TypeError(f"不支持的 model 配置: {spec!r}")


def _resolve_tools(specs, registry_getter):
    """specs 里的每一项可能是：
       - "module:attr"              → 普通 tool
       - {"agent": "xxx", "description": "..."} → 子 agent 包成 tool
    """
    resolved = []
    for spec in specs or []:
        if isinstance(spec, str):
            resolved.append(_import_from_path(spec))
        elif isinstance(spec, dict) and "agent" in spec:
            resolved.append(_agent_as_tool(spec["agent"], spec["description"],
                                            registry_getter))
        else:
            raise TypeError(f"不支持的 tool 配置: {spec!r}")
    return resolved


def _agent_as_tool(name, description, registry_getter):
    from langchain_core.tools import tool

    @tool(name, description=description)
    def _call(
        task: str,
        config: Annotated[RunnableConfig, InjectedToolArg],
    ) -> str:
        sub = registry_getter(name)               # 从注册表惰性拿 agent
        # 传递 config 使 callbacks 穿透到子 agent 的 LLM 调用
        result = sub.invoke(
            {"messages": [{"role": "user", "content": task}]},
            config=config,
        )
        return result["messages"][-1].content

    return _call

def _resolve_system_prompt(spec: Any, base_dir: Path | None) -> str:
    """system_prompt 字段：
      - 字符串：直接作为系统提示
      - {"file": "prompts/xxx.md"}：从文件读取（相对 base_dir 解析）
    """
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict) and "file" in spec:
        path = Path(spec["file"])
        if base_dir is not None and not path.is_absolute():
            path = base_dir / path
        return path.read_text(encoding="utf-8")
    raise TypeError(f"不支持的 system_prompt 配置: {spec!r}")


# ---------- 组装 ----------
def build_agent(config: dict[str, Any], base_dir: Path | None = None) -> Any:
    """根据已解析的配置字典构建 Agent。

    - type=worker：走 LangChain create_agent（工具型叶子 Agent）
    - type=supervisor：走 LangGraph 规划-执行分离图（planner → executor → route）
    """
    from agents.registry import get_worker  # 惰性导入，避免循环依赖

    kind = config.get("type", "worker")

    if kind == "supervisor":
        from agents.supervisor_graph import build_supervisor_graph
        return build_supervisor_graph(config, base_dir)

    return create_agent(
        model=_resolve_model(config.get("model")),
        tools=_resolve_tools(config.get("tools"), registry_getter=get_worker),
        system_prompt=_resolve_system_prompt(config["system_prompt"], base_dir),
    )


def load_agent(yaml_path: str | Path) -> Any:
    """从单个 YAML 文件加载 Agent。"""
    path = Path(yaml_path)
    with path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    return build_agent(config, base_dir=path.parent)


def load_agents_from_dir(dir_path: str | Path) -> dict[str, Any]:
    """批量加载目录下所有 .yaml / .yml，返回 {agent_name: agent}。

    agent_name 优先取配置里的 `name` 字段，否则用文件名（去掉扩展名）。
    """
    dir_path = Path(dir_path)
    agents: dict[str, Any] = {}
    for yaml_file in sorted(dir_path.glob("*.y*ml")):
        with yaml_file.open("r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        name = config.get("name") or yaml_file.stem
        agents[name] = build_agent(config, base_dir=yaml_file.parent)
    return agents