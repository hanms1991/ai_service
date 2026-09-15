# agents/registry.py
from pathlib import Path
from agents.generate_agent import load_agent

_CONFIG_DIR = Path(__file__).parent / "configs"
_cache: dict = {}


def get_worker(name: str):
    """按名字惰性加载并缓存一个叶子 agent。"""
    if name not in _cache:
        _cache[name] = load_agent(_CONFIG_DIR / f"{name}.yaml")
    return _cache[name]