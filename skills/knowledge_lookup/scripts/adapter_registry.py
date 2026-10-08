"""适配器注册表：加载 adapters.yaml，提供目录与路由查询。"""
import yaml
from pathlib import Path

_ADAPTERS_YAML = Path(__file__).resolve().parent.parent / "adapters.yaml"
_registry = None

def _load() -> list[dict]:
    global _registry
    if _registry is None:
        data = yaml.safe_load(_ADAPTERS_YAML.read_text(encoding="utf-8")) or {}
        _registry = data.get("adapters") or []
    return _registry

def list_catalog() -> list[dict]:
    return _load()

def find(domain: str, query_type: str) -> dict | None:
    for a in _load():
        if str(a.get("domain") or "").strip() == domain and \
           str(a.get("query_type") or "").strip() == query_type:
            return a
    return None

def format_catalog_text(adapters: list[dict]) -> str:
    """把适配器列表格式化为供 LLM 读取的文本目录。"""
    if not adapters:
        return "（暂无可用检索能力）"
    lines = []
    for a in adapters:
        domain = a.get("domain", "?")
        qt = a.get("query_type", "?")
        desc = a.get("description", "")
        params = a.get("params") or []
        if params:
            param_desc = "、".join(
                f"{p.get('name','?')}（{p.get('desc','')}，"
                f"{'必填' if p.get('required') else '可选'}，类型 {p.get('type','string')}）"
                for p in params if isinstance(p, dict)
            )
        else:
            param_desc = "（无参数）"
        lines.append(f"- domain={domain} ｜ query_type={qt} ｜ {desc} ｜ 参数：{param_desc}")
    lines.append("")
    lines.append("请从用户意图中提取 domain 和 query_type（必须在上述目录中），"
                 "以及 params（按各适配器参数声明填充，数组参数提取全部提及项）。")
    return "\n".join(lines)
