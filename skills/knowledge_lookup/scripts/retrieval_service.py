"""知识检索服务：声明式适配器引擎的统一入口，供多技能复用。

从 dispatch_lookup.py 提取，剥离 LLM 意图解析和渲染，只保留：
  - retrieve()：走声明式适配器，返回结构化 records + note
  - retrieve_raw()：薄包装 ctx.retrieve_knowledge，统一日志/异常降级

消费方：
  - knowledge_lookup 技能：prepare 钩子调 retrieve() 拿 records 后渲染 markdown
  - hazard_analysis 技能：failure_mode/safety_goal 调 retrieve()，hara_event 多路
    编排单路调 retrieve_raw()
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


def retrieve(domain: str, query_type: str, params: dict, ctx) -> tuple[list, str]:
    """走声明式适配器，返回 (records, note)。

    内部调用 adapter_registry.find() 定位适配器，再走 _invoke_declarative 或
    _invoke_code_adapter。不包含 LLM 意图解析（由消费方完成）和渲染。
    """
    reg = _load_sibling("adapter_registry")
    adapter = reg.find(domain, query_type)
    if adapter is None:
        logger.warning(
            "[retrieval_service] 未匹配适配器：domain=%s query_type=%s",
            domain, query_type,
        )
        return [], ""
    records, _usage, note = _invoke_adapter(adapter, params or {}, ctx)
    return records, note


def retrieve_raw(domain: str, query: str, *, layer: str | None = None,
                 top_k: int = 20, keywords: list[str] | None = None,
                 score_threshold=None, fetch_k: int | None = None,
                 chunk_filter=None, ctx=None) -> list:
    """薄包装 ctx.retrieve_knowledge，统一日志/异常降级。

    供多路编排逻辑（如 hazard_analysis 的 _retrieve_event_union）单路调用。
    """
    if ctx is None or not callable(getattr(ctx, "retrieve_knowledge", None)):
        return []
    retrieve_fn = ctx.retrieve_knowledge
    extra: dict = {}
    if fetch_k is not None:
        extra["fetch_k"] = int(fetch_k)
    if chunk_filter:
        extra["chunk_filter"] = chunk_filter
    if keywords:
        extra["keywords"] = keywords
    if score_threshold is not None:
        extra["score_threshold"] = score_threshold
    try:
        return retrieve_fn(domain, query, top_k=top_k, layer=layer, **extra) or []
    except Exception as exc:
        logger.warning(
            "[retrieval_service] 检索降级（query=%s layer=%s keywords=%s）：%s",
            query, layer, keywords, exc,
        )
        return []


# ── 以下为从 dispatch_lookup.py 迁移的内部实现 ──────────────────────

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
    script_path = _HERE.parent / adapter.get("module", "")
    function_name = str(adapter.get("function") or "").strip()
    if not script_path.is_file() or not function_name:
        logger.warning(
            "[retrieval_service] 适配器模块或函数缺失：%s/%s",
            script_path, function_name,
        )
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

    sub_ctx = SimpleNamespace(
        inputs=params or {},
        retrieve_knowledge=ctx.retrieve_knowledge,
        kb_domain=str(adapter.get("kb_domain") or "").strip(),
        logger=ctx.logger if hasattr(ctx, "logger") else logger,
    )
    result = fn(sub_ctx)
    if not isinstance(result, dict):
        return [], {}
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
    keywords_tpl = retrieval_cfg.get("keywords_template")
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
            # 迭代模式下 keywords 从模板生成，确保精确匹配本轮功能名
            kw: list[str] | None = None
            if keywords_tpl:
                kw = [keywords_tpl.replace("{value}", val)]
            chunks = _safe_retrieve(retrieve, kb_domain, query, retrieval_cfg, keywords=kw)
            for c in chunks:
                if isinstance(c, dict):
                    c.setdefault("_iterate_value", val)
            all_chunks.extend(chunks)
    else:
        query = retrieval_cfg.get("query", "")
        # 非迭代模式：keywords_template 无 {value} 占位时按静态关键词使用
        kw: list[str] | None = None
        if keywords_tpl and "{value}" not in keywords_tpl:
            kw = [keywords_tpl]
        all_chunks = _safe_retrieve(retrieve, kb_domain, query, retrieval_cfg, keywords=kw)

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
        # 迭代检索时按轮次分别匹配：本轮召回的块只用本轮迭代值过滤，避免
        # A 功能的块因文本含 B 功能名而穿过 B 轮过滤（跨迭代污染）；
        # 显式声明 match_value_from 时保持并集语义不变。
        per_iteration = bool(iterate_param) and not filter_cfg.get("match_value_from")
        filtered = []
        for c in all_chunks:
            if not isinstance(c, dict):
                continue
            meta = c.get("meta") or {}
            content = str(c.get("content") or "")
            chunk_values = match_values
            if per_iteration:
                it_val = str(c.get("_iterate_value") or "").strip()
                if it_val:
                    chunk_values = [it_val]
            kept = False
            for match_val in chunk_values:
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
            # 无去重键的记录不参与去重：避免空键互相吞掉（如 meta 缺 failure_id 的旧数据）
            if not val:
                deduped.append(r)
                continue
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


def _safe_retrieve(retrieve, domain: str, query: str, retrieval_cfg: dict,
                   *, keywords: list[str] | None = None) -> list:
    """安全检索，异常时降级返回空。"""
    layer = retrieval_cfg.get("layer")
    top_k = retrieval_cfg.get("top_k", 20)
    fetch_k = retrieval_cfg.get("fetch_k")
    chunk_filter = retrieval_cfg.get("chunk_filter")
    extra: dict = {}
    if fetch_k is not None:
        extra["fetch_k"] = int(fetch_k)
    if chunk_filter:
        extra["chunk_filter"] = chunk_filter
    if keywords:
        extra["keywords"] = keywords
    try:
        return retrieve(domain, query, top_k=top_k, layer=layer, **extra) or []
    except Exception as exc:
        logger.warning("[retrieval_service] 检索降级（query=%s layer=%s keywords=%s）：%s",
                       query, layer, keywords, exc)
        return []
