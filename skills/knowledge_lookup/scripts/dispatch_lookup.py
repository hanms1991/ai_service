"""知识库检索主入口：编排 adapter_registry → intent_parser → retrieval_service → renderer。

prepare 钩子内完成全部工作，返回 {"final_text": <markdown 表格>} 短路。

检索引擎（声明式适配器解释执行 + 代码兜底）已提取到 retrieval_service.py，
供 knowledge_lookup（本文件渲染）和 hazard_analysis（HARA 专有后处理）共用。
"""
import importlib.util
import logging
from pathlib import Path

logger = logging.getLogger("skills.knowledge_lookup")

_HERE = Path(__file__).resolve().parent
_MODULES = {}


def _load_sibling(name: str):
    """惰性加载同目录模块（技能脚本以独立模块被引擎 importlib 加载，同目录
    模块不在 sys.path 上，需用 importlib 按绝对路径加载，与 hara_prepare.py
    的 _load_doc_extractor 模式一致）。"""
    if name not in _MODULES:
        path = _HERE / f"{name}.py"
        spec = importlib.util.spec_from_file_location(
            f"knowledge_lookup_{name}", path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MODULES[name] = module
    return _MODULES[name]


def prepare(ctx) -> dict:
    """编排：加载适配器目录 → LLM 解析意图 → 调检索服务 → 渲染表格。"""
    reg = _load_sibling("adapter_registry")
    ip = _load_sibling("intent_parser")
    rnd = _load_sibling("renderer")
    svc = _load_sibling("retrieval_service")

    adapters = reg.list_catalog()
    catalog_text = reg.format_catalog_text(adapters)

    # ① LLM 解析意图
    parsed, usage = ip.parse(ctx, catalog_text)
    domain = parsed["domain"]
    query_type = parsed["query_type"]
    params = parsed["params"]

    # ② 路由适配器
    adapter = reg.find(domain, query_type)
    if adapter is None:
        logger.warning("[knowledge_lookup] 未匹配适配器：domain=%s query_type=%s", domain, query_type)
        return {
            "final_text": (
                f"未找到匹配的检索能力（domain={domain or '空'}，"
                f"query_type={query_type or '空'}）。\n\n可用检索能力：\n\n{catalog_text}"
            ),
            "usage": usage,
        }

    # ③ 调用检索服务（声明式引擎在 retrieval_service 中，返回结构化 records）
    records, note = svc.retrieve(domain, query_type, params, ctx)

    # ④ 渲染（按适配器声明的 render_format，默认 table；tiered 用 summary_columns）
    fmt = str(adapter.get("render_format") or "table").strip()
    summary_cols = adapter.get("summary_columns") or adapter.get("output_columns") or []
    output_cols = adapter.get("output_columns") or []
    table = rnd.render(output_cols, records, fmt=fmt, summary_columns=summary_cols)
    if note:
        table = f"{note}\n\n{table}"
    return {"final_text": table, "usage": usage}
