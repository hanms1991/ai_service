"""知识库检索主入口：编排 adapter_registry → intent_parser → adapter → renderer。

prepare 钩子内完成全部工作，返回 {"final_text": <markdown 表格>} 短路。
"""
import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace

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
    """编排：加载适配器目录 → LLM 解析意图 → 路由适配器 → 渲染表格。"""
    reg = _load_sibling("adapter_registry")
    ip = _load_sibling("intent_parser")
    rnd = _load_sibling("renderer")

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

    # ③ 调用适配器
    records, adapter_usage = _invoke_adapter(adapter, params, ctx)
    if isinstance(adapter_usage, dict):
        for k in adapter_usage:
            usage[k] = int(usage.get(k, 0) or 0) + int(adapter_usage.get(k, 0) or 0)

    # ④ 渲染表格
    table = rnd.render_table(adapter.get("output_columns") or [], records)
    return {"final_text": table, "usage": usage}


def _invoke_adapter(adapter: dict, params: dict, ctx) -> tuple[list, dict]:
    """加载适配器模块并调用入口函数。"""
    script_path = Path(__file__).resolve().parent.parent / adapter.get("module", "")
    function_name = str(adapter.get("function") or "").strip()
    if not script_path.is_file() or not function_name:
        logger.warning("[knowledge_lookup] 适配器模块或函数缺失：%s/%s", script_path, function_name)
        return [], {}

    spec = importlib.util.spec_from_file_location(
        f"adapter_{adapter.get('domain','?')}_{adapter.get('query_type','?')}",
        script_path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn = getattr(module, function_name, None)
    if not callable(fn):
        raise AttributeError(f"适配器 {script_path} 不存在入口函数 {function_name!r}")

    # 构造适配器子上下文
    sub_ctx = SimpleNamespace(
        inputs=params or {},
        retrieve_knowledge=ctx.retrieve_knowledge,
        kb_domain=str(adapter.get("kb_domain") or "").strip(),
        logger=ctx.logger,
    )
    result = fn(sub_ctx)
    if not isinstance(result, dict):
        return [], {}
    records = result.get("records") or []
    a_usage = result.get("usage") or {}
    if not isinstance(records, list):
        records = []
    if not isinstance(a_usage, dict):
        a_usage = {}
    return records, a_usage
