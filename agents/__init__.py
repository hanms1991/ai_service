"""Agent 注册表：启动时从 configs/ 批量加载全部 YAML 配置。"""
from pathlib import Path

from agents.generate_agent import load_agents_from_dir

AGENTS: dict = load_agents_from_dir(Path(__file__).parent / "configs")


def __getattr__(name: str):
    """按属性名惰性访问 AGENTS 中已注册的 agent。"""
    if name in AGENTS:
        return AGENTS[name]
    raise AttributeError(f"未注册的 agent: {name!r}")