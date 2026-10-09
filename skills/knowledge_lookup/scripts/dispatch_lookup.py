"""知识库检索主入口：编排 adapter_registry → intent_parser → adapter → renderer。

prepare 钩子内完成全部工作，返回 {"final_text": <markdown 表格>} 短路。

适配器两种路径：
  - 路径 A（声明式，默认）：YAML 中含 retrieval+extract，由 _invoke_declarative
    通用引擎解释执行 retrieve→filter→extract→dedup→output，零 Python 代码。
  - 路径 B（代码兜底）：YAML 中含 module+function，加载 Python 适配器模块调用。
    当前 adapters.yaml 无条目使用，留作未来复杂场景兜底。
"""
import importlib.util
import logging
import re
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
    records, adapter_usage, note = _invoke_adapter(adapter, params, ctx)
    if isinstance(adapter_usage, dict):
        for k in adapter_usage:
            usage[k] = int(usage.get(k, 0) or 0) + int(adapter_usage.get(k, 0) or 0)

    # ④ 渲染（按适配器声明的 render_format，默认 table；tiered 用 summary_columns）
    fmt = str(adapter.get("render_format") or "table").strip()
    summary_cols = adapter.get("summary_columns") or adapter.get("output_columns") or []
    output_cols = adapter.get("output_columns") or []
    table = rnd.render(output_cols, records, fmt=fmt, summary_columns=summary_cols)
    if note:
        table = f"{note}\n\n{table}"
    return {"final_text": table, "usage": usage}


def _invoke_adapter(adapter: dict, params: dict, ctx) -> tuple[list, dict, str]:
    """两条路径分发：
      - 路径 A（声明式，默认）：有 retrieval+extract，走 _invoke_declarative 引擎；
      - 路径 B（代码兜底）：有 module+function，走 _invoke_code_adapter 加载 Python 模块。
    """
    if adapter.get("module") and adapter.get("function"):
        return _invoke_code_adapter(adapter, params, ctx)
    return _invoke_declarative(adapter, params, ctx)


def _invoke_code_adapter(adapter: dict, params: dict, ctx) -> tuple[list, dict, str]:
    """加载适配器模块并调用入口函数（代码兜底路径）。"""
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
        return [], {}, ""
    records = result.get("records") or []
    a_usage = result.get("usage") or {}
    note = str(result.get("note") or "").strip()
    if not isinstance(records, list):
        records = []
    if not isinstance(a_usage, dict):
        a_usage = {}
    return records, a_usage, note


def _get_nested(d, key_path):
    """按点号分割递归取嵌套值。source.file → d["source"]["file"]。"""
    if not key_path or not isinstance(d, dict):
        return ""
    val = d
    for part in str(key_path).split("."):
        if isinstance(val, dict):
            val = val.get(part)
        else:
            return ""
    return str(val or "").strip() if val else ""


def _invoke_declarative(adapter: dict, params: dict, ctx) -> tuple[list, dict, str]:
    """声明式引擎：解释 YAML 声明执行 retrieve→filter→extract→dedup→output。

    流程：
      ① 检索：支持 iterate_param 遍历数组参数，每值单独检索并标记 _iterate_value；
      ② 过滤（match_any）：从 match_value_from 或 iterate_param 取匹配值，
         任一字段（meta.func / content / 等）包含任一匹配值即保留；
      ③ 提取：meta 字段支持点号嵌套取值（source.file → meta["source"]["file"]），
         content 字段按正则提取；raw_content_field 声明时把原始 chunk content
         原样挂到指定列，不做正则裁剪；
      ④ 去重：按 dedup_by 字段去重（null/空时跳过）；
      ⑤ 前缀过滤（prefix_map）：按 filter_param 取过滤值，对指定字段做前缀匹配，
         无匹配且 fallback_all=true 时返回全部 + note。
    """
    retrieve = ctx.retrieve_knowledge
    kb_domain = str(adapter.get("kb_domain") or "").strip()
    retrieval_cfg = adapter.get("retrieval") or {}

    # ─── ① 检索 ───────────────────────────────
    all_chunks: list = []

    iterate_param = retrieval_cfg.get("iterate_param")
    if iterate_param:
        values = params.get(iterate_param) or []
        if isinstance(values, str):
            values = [values]
        query_tpl = retrieval_cfg.get("query_template", "{value}")
        for val in values:
            val = str(val or "").strip()
            if not val:
                continue
            query = query_tpl.replace("{value}", val)
            chunks = _safe_retrieve(retrieve, kb_domain, query, retrieval_cfg)
            for c in chunks:
                if isinstance(c, dict):
                    c.setdefault("_iterate_value", val)
            all_chunks.extend(chunks)
    else:
        query = retrieval_cfg.get("query", "")
        all_chunks = _safe_retrieve(retrieve, kb_domain, query, retrieval_cfg)

    # ─── ② 过滤（match_any 机制）─────────────
    filter_cfg = adapter.get("filter") or {}
    match_any = filter_cfg.get("match_any")
    if match_any:
        match_values: list[str] = []
        match_param = filter_cfg.get("match_value_from") or iterate_param
        if match_param:
            vals = params.get(match_param) or []
            if isinstance(vals, str):
                vals = [vals]
            match_values = [str(v).strip() for v in vals if v]
        filtered = []
        for c in all_chunks:
            if not isinstance(c, dict):
                continue
            meta = c.get("meta") or {}
            content = str(c.get("content") or "")
            kept = False
            for match_val in match_values:
                if not match_val:
                    continue
                for field_cfg in match_any:
                    field = field_cfg.get("field", "")
                    if field == "meta.func":
                        field_val = str(meta.get("func") or "")
                    elif field == "content":
                        field_val = content
                    elif field.startswith("meta."):
                        field_val = str(meta.get(field[5:]) or "")
                    else:
                        field_val = content
                    if match_val in field_val:
                        kept = True
                        break
                if kept:
                    break
            if kept:
                filtered.append(c)
        all_chunks = filtered

    # ─── ③ 提取 ───────────────────────────────
    extract_cfg = adapter.get("extract") or {}
    meta_map = extract_cfg.get("meta") or {}
    content_map = extract_cfg.get("content") or {}
    raw_field = adapter.get("raw_content_field")

    records: list[dict] = []
    for c in all_chunks:
        if not isinstance(c, dict):
            continue
        meta = c.get("meta") or {}
        content = str(c.get("content") or "")
        record: dict = {}
        for col, key in meta_map.items():
            record[col] = _get_nested(meta, key)
        for col, pattern in content_map.items():
            m = re.search(pattern, content, re.S)
            record[col] = m.group(1).strip() if m else ""
        # raw_content_field：原始 chunk content 原样挂到指定列，不做正则裁剪
        if raw_field:
            record[raw_field] = content
        attach_to = adapter.get("attach_param_value_to")
        if attach_to:
            record[attach_to] = str(c.get("_iterate_value") or "").strip()
        records.append(record)

    # ─── ④ 去重 ───────────────────────────────
    dedup_field = adapter.get("dedup_by")
    if dedup_field:
        seen: set[str] = set()
        deduped: list[dict] = []
        for r in records:
            val = str(r.get(dedup_field, "") or "")
            if val in seen:
                continue
            seen.add(val)
            deduped.append(r)
        records = deduped

    # ─── ⑤ 前缀过滤（safety_goal 的域名过滤）──
    note = ""
    prefix_map = filter_cfg.get("prefix_map")
    if prefix_map:
        filter_field = filter_cfg.get("field", "")
        filter_param = filter_cfg.get("filter_param", "")
        filter_value = str(params.get(filter_param) or "").strip()
        if filter_value and filter_value in prefix_map:
            prefixes = prefix_map[filter_value]
            filtered = [r for r in records
                        if any(str(r.get(filter_field, "")).startswith(p + "_") for p in prefixes)]
            if filtered:
                records = filtered
            elif filter_cfg.get("fallback_all"):
                note = f"知识库中未找到{filter_value}专属数据，以下为全部 {len(records)} 条"

    return records, {}, note


def _safe_retrieve(retrieve, domain: str, query: str, retrieval_cfg: dict) -> list:
    """安全检索，异常时降级返回空。"""
    layer = retrieval_cfg.get("layer")
    top_k = retrieval_cfg.get("top_k", 20)
    try:
        return retrieve(domain, query, top_k=top_k, layer=layer) or []
    except Exception as exc:
        logger.warning("[knowledge_lookup] 检索降级（query=%s layer=%s）：%s", query, layer, exc)
        return []
