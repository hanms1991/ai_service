# -*- coding: utf-8 -*-
"""HARA 技能前置准备（prepare 钩子）与评级检索钩子：技能侧历史知识库检索策略。

这是 hazard_analysis 技能专属逻辑，不属于通用技能引擎；通过
execution.prepare.script 由引擎动态加载调用。

入口契约（引擎侧见 agents/capability_registry.py）：
1. prepare(ctx) -> {
       "kb_block": str,      # 注入规划层的历史知识块（空串 → 引擎回退粗召回）
       "usage": {...},       # 本钩子内 LLM 调用的 token 计量
       "identify_info": dict # 诊断用附加信息（引擎不解释）
   }
   流程：识别整车功能 → 功能级精准检索（功能/失效模式/该功能全部历史 HARA 事件/安全目标）
2. retrieve_map(ctx) -> list[dict]   # 评级切片历史 HARA 事件多路召回（原始分块列表）
   每切片按「整车功能+失效词」锚定多路并集，尽量穷尽该失效下全部历史事件；
   引擎在未声明钩子时回退通用单路检索。
3. review_events(ctx) -> {"events": [...], "usage": {...}}
   # 评级结果评审钩子：代码比对全部事件 source 标注与本次召回的历史候选，
   # 疑似项（漏标沿用/虚引历史 ID/评级与所引不一致等）回炉 LLM 二次校验后回填
钩子内任何异常由引擎捕获并软降级，不阻断技能执行。
"""
from __future__ import annotations

import difflib
import importlib.util
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger("skills.hara_prepare")

_IDENTIFY_MAX_FUNCTIONS = 10
_DEFAULT_LAYERS = ["function_list", "failure_mode", "hara_event", "safety_goal"]
# 单次检索服务端硬上限 20 块；多路并集才能覆盖一个失效下约 19 条历史事件
_DEFAULT_EVENT_TOP_K = 20
_DEFAULT_EVENT_CAP = 45
_DEFAULT_RETRIEVE_WORKERS = 8
_DEFAULT_EVENT_FACETS = [
    "高速 直行 道路 行驶",
    "低速 转弯 泊车 停车位",
    "弯道 变道 超车",
    "湿滑 路面 附着",
    "行人 车辆 碰撞 追尾",
    "静止 停车 无事故 原地",
    "颠簸 减速带 紧急避让",
]

# 同目录结构化抽取模块（doc_item_table.py）的惰性加载缓存
_DOC_EXTRACTOR = None
_DOC_EXTRACTOR_NAME = "doc_item_table"


def _load_doc_extractor():
    """惰性加载同目录 doc_item_table 模块（技能脚本以独立模块方式被引擎加载）。"""
    global _DOC_EXTRACTOR
    if _DOC_EXTRACTOR is None:
        path = Path(__file__).resolve().parent / f"{_DOC_EXTRACTOR_NAME}.py"
        spec = importlib.util.spec_from_file_location(
            f"hara_{_DOC_EXTRACTOR_NAME}", path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _DOC_EXTRACTOR = module
    return _DOC_EXTRACTOR


def _build_doc_supplement(ctx) -> str:
    """从上传的原始 DOCX 确定性抽取功能清单，返回注入识别/规划层的权威段落。

    平台 Markdown 转换对合并单元格表格不稳定（vMerge 续行被丢弃导致错列，
    LLM 会把同一功能编号的多行实现要素误枚举为重复 Feature）；此处绕过
    Markdown 直接解析原始 Word。仅处理 file_id 对应的 .docx；无文件/非
    docx/解析失败一律软降级返回空串，不阻断技能执行。
    """
    try:
        source_cfg = ctx.skill_cfg.get("document_source") or {}
        file_id_input = str(source_cfg.get("file_id_input") or "file_id")
        file_id = str((ctx.inputs or {}).get(file_id_input) or "").strip()
        if not file_id:
            return ""
        from core.file_sandbox import get_meta, resolve_stored_path

        meta = get_meta(file_id)
        if (meta.ext or "").lower() != ".docx":
            return ""
        extractor = _load_doc_extractor()
        data = extractor.extract_item_functions(resolve_stored_path(file_id))
        supplement = extractor.render_supplement(data) if data else ""
        if supplement:
            logger.info(
                "[hara_prepare] 功能清单结构化抽取成功：%d 个整车功能，%d 个 Feature",
                len(data.get("functions") or []),
                sum(len(f.get("features") or [])
                    for f in (data.get("functions") or [])),
            )
        return supplement
    except Exception as exc:  # noqa: BLE001 —— 抽取是增强而非前提
        logger.warning("[hara_prepare] 功能清单结构化抽取降级：%s", exc)
        return ""


def prepare(ctx) -> dict:
    """识别相关项/整车功能，按功能双路精准检索，返回规划层知识注入块。"""
    skill_cfg = ctx.skill_cfg
    exec_cfg = skill_cfg.get("execution") or {}
    prepare_cfg = exec_cfg.get("prepare") or {}
    identify_cfg = prepare_cfg.get("identify") or {}
    if not identify_cfg.get("enabled"):
        return {"kb_block": "", "usage": {}, "identify_info": None}

    # 确定性结构化抽取（DOCX 功能清单）：同时供识别层与规划层使用
    doc_supplement = _build_doc_supplement(ctx)

    info, response = _run_identify_stage(ctx, identify_cfg, doc_supplement)
    kb_block = _targeted_layered_retrieval(ctx, skill_cfg, info)
    logger.info(
        "[hara_prepare] 识别层精准检索注入：%d 字符；识别功能=%s",
        len(kb_block), info.get("functions"),
    )
    return {
        "kb_block": kb_block,
        "usage": ctx.extract_usage(response),
        "identify_info": info,
        "doc_supplement": doc_supplement,
    }


# ── Stage 0：识别相关项与整车功能清单 ──────────────────────────────

def _run_identify_stage(ctx, identify_cfg: dict, doc_supplement: str = ""):
    """识别层：只从相关项信息中识别 item 基本信息与整车功能名称清单。

    不注入知识库、不做安全分析，输出用于驱动功能级精准检索。
    functions 为空时抛异常，由引擎统一软降级。
    """
    system_prompt = ctx.build_stage_prompt(identify_cfg)
    supplement_block = f"\n\n{doc_supplement}\n" if doc_supplement else ""
    user_message = (
        ctx.rendered
        + ctx.doc_block
        + supplement_block
        + "\n\n本阶段只做识别：列出相关项基本信息与【整车层级功能】名称清单，"
        "不做失效/危害/评级分析；严格只输出约定的紧凑 JSON 对象，不要输出解释。"
        "若上方提供了《系统结构化抽取：相关项功能清单》，整车功能以该块分组为准。"
    )
    max_tokens = identify_cfg.get("max_tokens")
    parsed, response = ctx.invoke_json_stage(
        ctx.bind_json_model(max_tokens),
        system_prompt,
        user_message,
        stage_name="识别层",
        max_tokens=max_tokens,
        runnable_config=ctx.runnable_config,
    )
    item = parsed.get("item")
    item = item if isinstance(item, dict) else {}
    functions: list[str] = []
    for fn in parsed.get("functions") or []:
        if isinstance(fn, str):
            name = fn.strip()
        elif isinstance(fn, dict):
            name = str(fn.get("vehicle_function") or fn.get("name") or "").strip()
        else:
            name = ""
        if name and name not in functions:
            functions.append(name)
    if not functions:
        raise ValueError("识别层输出 functions 为空，无法做功能级检索")
    info = {
        "item_name": str(item.get("name") or "").strip(),
        "abbr": str(item.get("abbr") or "").strip(),
        "domain_prefix": str(item.get("domain_prefix") or "").strip(),
        "functions": functions[:_IDENTIFY_MAX_FUNCTIONS],
    }
    logger.info(
        "[hara_prepare] 识别层结果：item=%s（%s/%s），整车功能 %d 个：%s",
        info["item_name"], info["abbr"], info["domain_prefix"],
        len(info["functions"]), info["functions"],
    )
    return info, response


# ── 历史 HARA 事件多路并集检索 ─────────────────────────────────────

def _event_query_specs(func_name: str, word: str, desc: str,
                       facets: list[str]) -> list[str]:
    """一个失效的多路 event 召回 query（服务端单次上限 20，靠并集覆盖 ~19 条事件）。"""
    specs = [
        f"{func_name} {word} HARA 危害事件 运行场景",
        f"{func_name} {word} {desc} 整车危害 危害事件".strip(),
    ]
    specs += [f"{func_name} {word} HARA {f}" for f in facets]
    return specs


def _chunk_key(chunk: dict) -> str:
    content = str(chunk.get("content") or "")
    meta = chunk.get("meta") or {}
    return str(meta.get("hzrd_id") or "").strip() or (
        str(chunk.get("id") or chunk.get("chunk_id") or "").strip()
        or content[:120] + f"#{len(content)}"
    )


def _fan_out_retrieve(retrieve, domain: str, queries: list[str], *,
                      top_k: int, threshold, layer: str | None,
                      workers: int) -> list[list]:
    """并发多路检索，保持与 queries 相同的返回顺序；单路异常降级为空列表。"""
    def _one(q: str) -> list:
        try:
            return retrieve(
                domain, q, top_k=top_k, score_threshold=threshold, layer=layer,
            )
        except Exception as exc:  # noqa: BLE001 —— 单路失败不影响并集
            logger.warning("[hara_prepare] event 召回单路降级（q=%r）：%s", q[:40], exc)
            return []

    if len(queries) <= 1:
        return [_one(queries[0])] if queries else []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(_one, queries))


def _retrieve_event_union(retrieve, domain: str, func_name: str, word: str,
                          desc: str, kb_cfg: dict) -> list[dict]:
    """按「功能+失效词」锚定多路召回并集：同功能优先、按 hzrd_id 去重、封顶 cap。"""
    facets = [str(f) for f in (kb_cfg.get("map_event_facets") or _DEFAULT_EVENT_FACETS)]
    top_k = int(kb_cfg.get("map_event_top_k") or _DEFAULT_EVENT_TOP_K)
    cap = int(kb_cfg.get("map_event_cap") or _DEFAULT_EVENT_CAP)
    workers = int(kb_cfg.get("retrieve_workers") or _DEFAULT_RETRIEVE_WORKERS)
    threshold = kb_cfg.get("score_threshold")

    specs = _event_query_specs(func_name, word, desc, facets)
    batches = _fan_out_retrieve(
        retrieve, domain, specs,
        top_k=min(top_k, 20), threshold=threshold,
        layer="hara_event", workers=workers,
    )
    seen: set[str] = set()
    own: list[dict] = []
    other: list[dict] = []
    for batch in batches:
        for c in batch or []:
            if not isinstance(c, dict):
                continue
            key = _chunk_key(c)
            if key in seen:
                continue
            seen.add(key)
            meta = c.get("meta") or {}
            if str(meta.get("func") or "").strip() == func_name:
                own.append(c)
            else:
                other.append(c)
    union = own + other
    if len(union) > cap:
        union = union[:cap]
    logger.info(
        "[hara_prepare] event 并集召回（%s/%s）：%d 路查询，本功能 %d 块，"
        "合计保留 %d 块（cap=%d）",
        func_name, word, len(specs), len(own), len(union), cap,
    )
    return union


def _parse_fm_desc(chunk: dict) -> str:
    """从 failure_mode 分块正文提取「功能异常表现」短名（如"失去转向助力"）。"""
    m = re.search(r"功能异常表现：(.+)", str(chunk.get("content") or ""))
    return (m.group(1).strip() if m else "")[:60]


# ── 功能级精准检索 ─────────────────────────────────────────────────

def _targeted_layered_retrieval(ctx, skill_cfg: dict, identify_info: dict) -> str:
    """按识别出的每个整车功能精准检索，合并去重后四层分组。

    每功能：
      q1 = "<相关项名> <整车功能名>"  综合召回（功能清单/安全目标等）
      q2 = "<整车功能名> 失效模式"    专项召回 failure_mode 层（历史失效清单）
      q3+ = 以 q2 命中的每个历史失效为单位，多路并集召回其全部历史 HARA 事件
            （prepare_event_recall=false 时关闭 q3+）
    """
    kb_cfg = skill_cfg.get("knowledge") or {}
    domain = str(kb_cfg.get("domain") or "").strip()
    if not domain:
        return ""
    layers = [str(x) for x in (kb_cfg.get("layers") or _DEFAULT_LAYERS)]
    func_top_k = int(kb_cfg.get("identify_func_top_k", 10) or 10)
    fm_top_k = int(kb_cfg.get("identify_fm_top_k", 10) or 10)
    event_recall = bool(kb_cfg.get("prepare_event_recall", True))
    item_name = identify_info.get("item_name") or ""

    seen: set[str] = set()
    merged: list[dict] = []

    def _ingest(chunks) -> int:
        if not isinstance(chunks, list):
            return 0
        added = 0
        for c in chunks:
            if not isinstance(c, dict):
                continue
            key = _chunk_key(c)
            if key in seen:
                continue
            seen.add(key)
            merged.append(c)
            added += 1
        return added

    for fname in identify_info["functions"]:
        base_query = f"{item_name} {fname}".strip()
        _ingest(ctx.retrieve_knowledge(
            domain, base_query,
            top_k=func_top_k, score_threshold=kb_cfg.get("score_threshold"),
        ))
        fm_chunks = ctx.retrieve_knowledge(
            domain, f"{fname} 失效模式",
            top_k=fm_top_k, score_threshold=kb_cfg.get("score_threshold"),
            layer="failure_mode",
        )
        _ingest(fm_chunks)

        # 该功能在历史项目中的失效清单（meta.func 精确同名、确有事件分块）
        hist_failures: list[tuple[str, str]] = []
        if event_recall and isinstance(fm_chunks, list):
            fm_seen: set[str] = set()
            for c in fm_chunks:
                meta = c.get("meta") or {}
                fid = str(meta.get("failure_id") or "").strip()
                word = str(meta.get("failure_type") or "").strip()
                if (
                    str(meta.get("func") or "").strip() == fname
                    and meta.get("has_events") and word and fid
                    and fid not in fm_seen
                ):
                    fm_seen.add(fid)
                    hist_failures.append((word, _parse_fm_desc(c)))
            for word, desc in hist_failures:
                _ingest(_retrieve_event_union(
                    ctx.retrieve_knowledge, domain, fname, word, desc, kb_cfg,
                ))
            logger.info(
                "[hara_prepare] %s 历史失效 %d 个：%s",
                fname, len(hist_failures), [w for w, _ in hist_failures],
            )

    layer_counts: dict[str, int] = {}
    for c in merged:
        ln = str((c.get("meta") or {}).get("layer") or "未标注")
        layer_counts[ln] = layer_counts.get(ln, 0) + 1
    logger.info(
        "[hara_prepare] 功能级精准检索：%d 个功能，去重后 %d 块，层分布=%s",
        len(identify_info["functions"]), len(merged), layer_counts,
    )
    if not merged:
        return ""
    # 规划层需承载每功能全部历史 HARA 事件，默认 2 万字篇幅上限会截断，
    # 故由技能契约 knowledge.layered_block_max_chars 覆盖（未配置走 kb_client 默认值）
    fmt_kwargs = {}
    block_max = kb_cfg.get("layered_block_max_chars")
    chunk_max = kb_cfg.get("layered_chunk_max_chars")
    if block_max:
        fmt_kwargs["max_total_chars"] = int(block_max)
    if chunk_max:
        fmt_kwargs["max_chunk_chars"] = int(chunk_max)
    return ctx.format_knowledge_layered_block(merged, layers, domain, **fmt_kwargs)


# ── 评级层（map）每切片历史事件召回钩子 ────────────────────────────

def retrieve_map(ctx) -> list[dict]:
    """评级切片历史 HARA 事件召回：功能名+失效词锚定的多路并集。

    引擎侧 MapSliceContext 提供 unit（当前切片）/plan_parsed（fid→功能名）/
    skill_cfg/retrieve_knowledge；返回原始分块列表（空列表即零命中）。
    """
    unit = ctx.unit if isinstance(ctx.unit, dict) else {}
    kb_cfg = ctx.skill_cfg.get("knowledge") or {}
    domain = str(kb_cfg.get("domain") or "").strip()
    word = str(unit.get("word") or "").strip()
    if not domain or not word:
        return []

    func_name = ""
    fid = str(unit.get("fid") or "").strip()
    for fn in (ctx.plan_parsed or {}).get("functions") or []:
        if isinstance(fn, dict) and str(fn.get("fid") or "").strip() == fid:
            func_name = str(fn.get("vehicle_function") or "").strip()
            break
    desc = str(unit.get("malfunction_behavior") or "").strip()
    chunks = _retrieve_event_union(
        ctx.retrieve_knowledge, domain, func_name, word, desc[:120], kb_cfg,
    )
    logger.info(
        "[hara_prepare] 评级切片（%s/%s）event 钩子召回 %d 块",
        fid or "?", word, len(chunks),
    )
    return chunks


# ── 规划后处理钩子：按 ref_id 回查知识库原文，逐字覆盖沿用条目 ──────

_FM_FIELD_STOP = (
    r"\n失效类型选择理由：|\n未分析失效类型|\n覆盖规则说明|"
    r"\n危害事件统计|\nASIL 分布|\n典型运行场景"
)


def _fm_original_texts(content: str) -> tuple[str, str]:
    """从 failure_mode 分块正文提取「功能异常表现」「整车危害」原文（值可跨行）。"""
    text = str(content or "")
    mb = re.search(
        r"功能异常表现：(.*?)(?=\n整车危害：|\n\n|" + _FM_FIELD_STOP + r"|$)",
        text, re.S,
    )
    vh = re.search(
        r"整车危害：(.*?)(?=\n\n|" + _FM_FIELD_STOP + r"|$)",
        text, re.S,
    )
    return (mb.group(1).strip() if mb else "", vh.group(1).strip() if vh else "")


def _fetch_fm_originals(retrieve, domain: str, ref: str, func_name: str,
                        word: str, cache: dict) -> tuple[str, str] | None:
    """按失效模式 ref_id 回查知识库，返回（功能异常表现, 整车危害）原文。

    先用 ref 直查，未命中再用「功能名 失效词 失效模式」兜底；仍未命中返回 None。
    """
    if ref in cache:
        return cache[ref] or None
    result: tuple[str, str] | None = None
    queries = [ref] + ([f"{func_name} {word} 失效模式".strip()] if func_name else [])
    for q in queries:
        try:
            chunks = retrieve(domain, q, top_k=8, layer="failure_mode") or []
        except Exception as exc:  # noqa: BLE001 —— 回查失败不阻断规划后处理
            logger.warning(
                "[hara_prepare] 失效模式回查降级（ref=%s，q=%r）：%s", ref, q[:40], exc,
            )
            chunks = []
        for c in chunks:
            if not isinstance(c, dict):
                continue
            meta = c.get("meta") or {}
            content = str(c.get("content") or "")
            if str(meta.get("failure_id") or "").strip() == ref or (
                content.startswith(f"【失效模式 {ref}】")
            ):
                texts = _fm_original_texts(content)
                if texts[0] or texts[1]:
                    result = texts
                    break
        if result:
            break
    cache[ref] = result or ("", "")
    return result


def _parse_function_list_chunk(content: str) -> dict:
    """解析功能清单分块正文，提取功能名与 Feature 列表。

    返回 {name, features: [{feature_list_id, description, do_hara}]}。
    解析失败返回空 dict。
    """
    text = str(content or "")
    out: dict = {}
    # 功能名：【相关项功能清单 CS_func_0001 转向助力功能】
    m = re.search(r"【相关项功能清单\s+\S+\s+(.+?)】", text)
    if m:
        out["name"] = m.group(1).strip()
    features: list[dict] = []
    # 进行 HARA 分析的 Feature
    in_hara = False
    for line in text.splitlines():
        s = line.strip()
        if "进行 HARA 分析的 Feature" in s:
            in_hara = True
            continue
        if "不进行 HARA" in s or "未进行 HARA" in s:
            in_hara = False
            continue
        m = re.match(r"[-•]\s*(\S+)\s+(.+)", s)
        if m:
            features.append({
                "feature_list_id": m.group(1).strip(),
                "description": m.group(2).strip(),
                "do_hara": "是" if in_hara else "否",
            })
    if features:
        out["features"] = features
    return out


def _retrieve_function_list(retrieve, domain: str, func_name: str,
                            top_k: int = 10) -> dict | None:
    """按功能名检索知识库功能清单分块，返回解析后的功能名+Feature列表。"""
    if not func_name:
        return None
    try:
        chunks = retrieve(
            domain, f"{func_name} 相关项功能清单",
            top_k=top_k, layer="function_list",
        ) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("[hara_prepare] 功能清单检索降级（func=%s）：%s", func_name, exc)
        return None
    for c in chunks:
        if not isinstance(c, dict):
            continue
        meta = c.get("meta") or {}
        meta_func = str(meta.get("func") or meta.get("name") or "").strip()
        # 双向包含匹配
        if meta_func and meta_func not in func_name and func_name not in meta_func:
            continue
        parsed = _parse_function_list_chunk(str(c.get("content") or ""))
        # 用 content 中的功能名做二次校验
        cname = parsed.get("name", "")
        if cname and cname not in func_name and func_name not in cname:
            continue
        if parsed.get("features"):
            return parsed
    return None


def _retrieve_function_failure_modes(retrieve, domain: str, func_name: str,
                                     top_k: int = 20) -> list[dict]:
    """按功能名检索知识库中该功能的全部历史失效模式（确定性提取）。

    返回列表：[{word, failure_id, malfunction_behavior, vehicle_hazard, chunk}]。
    用于规划后处理阶段直接以知识库原文生成/覆盖 hazop_items，
    避免 LLM 遗漏或改写历史失效模式。
    """
    if not func_name:
        return []
    try:
        chunks = retrieve(
            domain, f"{func_name} 失效模式",
            top_k=top_k, layer="failure_mode",
        ) or []
    except Exception as exc:  # noqa: BLE001 —— 检索失败降级为空
        logger.warning(
            "[hara_prepare] 失效模式检索降级（func=%s）：%s", func_name, exc,
        )
        return []
    result: list[dict] = []
    seen_keys: set[str] = set()
    for c in chunks:
        if not isinstance(c, dict):
            continue
        meta = c.get("meta") or {}
        # 功能名双向包含匹配：LLM 生成的 vehicle_function 可能带"功能(EPB)"等后缀，
        # 知识库 meta.func 通常是纯功能名，故用包含关系而非精确相等
        meta_func = str(meta.get("func") or "").strip()
        if meta_func and meta_func not in func_name and func_name not in meta_func:
            continue
        word = str(meta.get("failure_type") or "").strip()
        if not word:
            continue
        fid = str(meta.get("failure_id") or "").strip()
        texts = _fm_original_texts(str(c.get("content") or ""))
        if not texts[0] and not texts[1]:
            continue
        # 必须按 failure_id 去重：同一失效词在历史项目中可能对应多个失效模式
        # （如 EPB 的"丢失"含「接合驻车制动器功能丧失」「释放驻车制动器功能
        # 丧失」两条，"非预期"含三条）。若按失效词去重，同词条目只剩一条，
        # 后处理会把多个 hazop_item 的表现/危害覆盖成同一份内容。
        key = fid or f"{word}:{str(c.get('content') or '')[:80]}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        result.append({
            "word": word,
            "failure_id": fid,
            "malfunction_behavior": texts[0],
            "vehicle_hazard": texts[1],
            "chunk": c,
        })
    return result


# 备注文本中引用的历史失效模式 ID（如 #CB_MF_0008_01）
_FM_ID_RE = re.compile(r"([A-Za-z]{1,5}_MF_\d+(?:_\d+)?)")


def _name_norm(text: str) -> str:
    """名称归一：去空白与标点并小写，用于无编号 Feature 的重复判定。"""
    return re.sub(
        r"[\s，。、；：,.;:!？?()（）\"'“”‘’\-—_/]+", "", str(text or "")
    ).lower()


def _merge_feature(base: dict, dup: dict) -> None:
    """把重复 Feature（同 feature_list_id 或同名）dup 合并进首条 base。"""
    if not str(base.get("description") or "").strip() and str(
        dup.get("description") or ""
    ).strip():
        base["description"] = dup["description"]
    # do_hara：任一"是"即"是"；否则取首个非空
    if str(dup.get("do_hara") or "").strip() == "是":
        base["do_hara"] = "是"
    elif not str(base.get("do_hara") or "").strip():
        base["do_hara"] = dup.get("do_hara") or ""
    for field in ("no_hara_reason", "doc_ref"):
        bval = str(base.get(field) or "").strip()
        dval = str(dup.get(field) or "").strip()
        if dval and dval not in bval:
            base[field] = "；".join(x for x in (bval, dval) if x)
    if not isinstance(base.get("source"), dict) and isinstance(
        dup.get("source"), dict
    ):
        base["source"] = dup["source"]


def _dedupe_function_features(plan: dict) -> int:
    """规划层 Feature 去重兜底：同功能内按 feature_list_id（无编号按名称）归并。

    Word 模板不固定（纵向合并、一个编号跨多行实现要素）时，LLM 可能按表格
    行枚举重复 Feature；此处是不依赖文档解析与知识库的最后防线。返回合并条数。
    """
    merged = 0
    for fn in plan.get("functions") or []:
        if not isinstance(fn, dict) or not isinstance(fn.get("features"), list):
            continue
        seen: dict[str, int] = {}
        kept: list[dict] = []
        for feat in fn["features"]:
            if not isinstance(feat, dict):
                continue
            fid = str(feat.get("feature_list_id") or "").strip()
            key = f"id:{fid.lower()}" if fid else f"name:{_name_norm(feat.get('description'))}"
            if key in seen:
                _merge_feature(kept[seen[key]], feat)
                merged += 1
                continue
            seen[key] = len(kept)
            kept.append(feat)
        fn["features"] = kept
    return merged


def _match_item_failure(item: dict, fm_index: dict[str, dict], *,
                        retrieve, domain: str, func_name: str,
                        fetch_cache: dict) -> tuple[dict | None, str]:
    """为一个 hazop_item 匹配唯一对应的知识库失效模式。

    同一失效词可能对应多个历史失效模式（如 EPB 的"丢失"有 2 条、"非预期"
    有 3 条），仅凭 word 无法消歧。按可靠性依次尝试：
      1. source.ref_id 显式指向（规划层被要求逐条填写）；
      2. ref_id 不在本次召回集合时，按 ref 直查知识库取原文（合成记录）；
      3. source_note 中恰好引用一个失效模式 ID；
      4. 该失效词在知识库中只有唯一候选；
      5. 同词多候选时，用条目已有文本与候选原文做相似度消歧。
    均不满足返回 (None, "ambiguous"/"missing")，调用方不得强行覆盖。
    """
    src = item.get("source") if isinstance(item.get("source"), dict) else {}
    ref = str(src.get("ref_id") or "").strip()
    word = str(item.get("word") or "").strip()

    # 1. ref_id 直配（fm_index 已按功能隔离，ref 是最权威的逐字信号）
    if ref and ref in fm_index:
        return fm_index[ref], "ref"
    # 2. ref_id 直查兜底（召回 top_k 截断未带入该分块时）
    if ref and ref not in fm_index:
        texts = _fetch_fm_originals(
            retrieve, domain, ref, func_name, word, fetch_cache
        )
        if texts:
            return {
                "word": word,
                "failure_id": ref,
                "malfunction_behavior": texts[0],
                "vehicle_hazard": texts[1],
                "chunk": {},
                "_synthesized": True,
            }, "ref_query"
    # 3. source_note 中恰好引用一个同失效词的 MF ID
    note = str(item.get("source_note") or "")
    note_hits = []
    for mid in dict.fromkeys(_FM_ID_RE.findall(note)):
        cand = fm_index.get(mid)
        if cand is not None and (
            not word or not cand.get("word")
            or str(cand["word"]).strip() == word
        ):
            note_hits.append(cand)
    if len(note_hits) == 1:
        return note_hits[0], "note"
    # 4/5. 按失效词
    cands = [
        fm for fm in fm_index.values()
        if word and str(fm.get("word") or "").strip() == word
    ]
    if len(cands) == 1:
        return cands[0], "word"
    if len(cands) > 1:
        cur = (str(item.get("malfunction_behavior") or "")
               + str(item.get("vehicle_hazard") or "")).strip()
        if cur:
            best, best_score = None, 0.0
            for fm in cands:
                score = max(
                    _scene_sim(cur, fm["malfunction_behavior"]),
                    _scene_sim(cur, fm["vehicle_hazard"]),
                    _scene_sim(cur, fm["malfunction_behavior"] + fm["vehicle_hazard"]),
                )
                if score > best_score:
                    best, best_score = fm, score
            if best is not None and best_score >= 0.45:
                return best, f"text@{best_score:.2f}"
        return None, "ambiguous"
    return None, "missing"


def fix_plan_items(ctx) -> dict:
    """规划后处理钩子（纯代码，不调 LLM）：知识库内容一律以原文为准，LLM 只补差。

    核心原则：沿用（reused）= 100% 全字段从知识库取，LLM 不生成任何内容；
    改编（adapted）= 部分字段从知识库取、部分由 LLM 修改；新增（new）= LLM 全量生成。
    适用于功能清单 / 失效模式 / HARA 事件 / 安全目标 四层数据。

    第-1步 功能清单覆盖：对每个功能检索知识库 function_list 层，按 feature_list_id
    匹配并覆盖 Feature 描述（do_hara 判断保留 LLM 输出，因需依据当前相关项定义）；
    第0步 失效模式确定性提取：对每个功能按功能名检索知识库 failure_mode 层，
    获取该功能全部历史失效模式（malfunction_behavior/vehicle_hazard 原文）；
    第1步 失效模式原文覆盖：LLM 生成的 hazop_items 中，凡知识库已有的失效
    （按 fid+word 匹配），强制用知识库原文逐字覆盖 malfunction_behavior /
    vehicle_hazard，并置 source.type=reused、ref_id=知识库 ID——杜绝 LLM
    改写/编造沿用条目内容；
    第1.5步 失效模式补全：知识库有但 LLM 未生成的失效模式，追加为新的
    hazop_item（原文取自知识库，source=reused）——杜绝 LLM 遗漏历史失效；
    第2步 场景历史骨架：对所有 hazop_items（含追加的）执行场景对齐与历史
    事件补录（宁多勿漏，实质同场景去重）。
    """
    plan = ctx.plan_parsed if isinstance(ctx.plan_parsed, dict) else {}
    items = plan.get("hazop_items")
    if not isinstance(items, list):
        items = []
        plan["hazop_items"] = items
    # 第-2步：Feature 去重兜底（不依赖知识库；在任何 KB 处理前先归并，
    # 防止模板中一个功能编号跨多行实现要素被 LLM 枚举成多个重复 Feature）
    deduped = _dedupe_function_features(plan)
    if deduped:
        logger.info("[hara_prepare] 规划后处理：归并重复 Feature %d 条", deduped)
    stats = {
        "func_covered": 0, "fm_covered": 0, "fm_appended": 0,
        "scene_aligned": 0, "scene_appended": 0, "features_deduped": deduped,
    }
    kb_cfg = ctx.skill_cfg.get("knowledge") or {}
    domain = str(kb_cfg.get("domain") or "").strip()
    retrieve = ctx.retrieve_knowledge
    if not domain or retrieve is None:
        return stats
    fn_list = plan.get("functions") or []
    functions = {
        str(fn.get("fid") or "").strip(): str(fn.get("vehicle_function") or "").strip()
        for fn in fn_list if isinstance(fn, dict)
    }
    matrix_by_fid = {
        str(m.get("fid") or "").strip(): m
        for m in (plan.get("malfunction_matrix") or []) if isinstance(m, dict)
    }

    # 第-1步：功能清单覆盖（Feature 描述从知识库取，do_hara 保留 LLM 判断）
    for fn in fn_list:
        if not isinstance(fn, dict):
            continue
        func_name = str(fn.get("vehicle_function") or "").strip()
        if not func_name:
            continue
        kb_func = _retrieve_function_list(retrieve, domain, func_name)
        if not kb_func:
            continue
        kb_feats = {
            str(f.get("feature_list_id") or "").strip(): f
            for f in (kb_func.get("features") or []) if isinstance(f, dict)
        }
        lfeats = fn.get("features")
        if not isinstance(lfeats, list):
            continue
        covered = 0
        for lf in lfeats:
            if not isinstance(lf, dict):
                continue
            fid2 = str(lf.get("feature_list_id") or "").strip()
            kb_f = kb_feats.get(fid2)
            if kb_f and str(kb_f.get("description") or "").strip():
                lf["description"] = kb_f["description"]
                covered += 1
        if covered:
            stats["func_covered"] += covered
            logger.info(
                "[hara_prepare] 功能清单覆盖：%s 命中 %d 个 Feature 描述",
                func_name, covered,
            )

    # 第0步：对每个功能检索知识库全部历史失效模式，按 failure_id 建索引
    # （同一失效词可能对应多个历史失效模式，绝不能按词归并）
    fm_by_func: dict[str, dict[str, dict]] = {}
    for func_name in functions.values():
        if not func_name:
            continue
        fms = _retrieve_function_failure_modes(retrieve, domain, func_name)
        index: dict[str, dict] = {}
        for i, fm in enumerate(fms):
            index[fm.get("failure_id") or f"__noid_{i}"] = fm
        fm_by_func[func_name] = index
        logger.info(
            "[hara_prepare] 知识库失效模式检索：%s 命中 %d 个失效（按 failure_id 去重）",
            func_name, len(index),
        )

    union_cache: dict[tuple[str, str], list[dict]] = {}
    covered_pairs: set[tuple[str, str]] = set()  # (fid, 知识库失效模式键)
    fm_fetch_cache: dict[str, tuple[str, str] | None] = {}

    # 第1步：失效模式原文覆盖（reused/缺省条目：知识库有则强制用原文，一字不差；
    # adapted=LLM 有意改编，保留其文本不覆盖，与 HARA 事件层 _materialize 的
    # full/adapted 语义对齐）。匹配必须精确到具体历史失效模式：ref_id 优先，
    # 同一失效词对应多个知识库条目时禁止"按词"取一条强行覆盖全部条目。
    for item in items:
        if not isinstance(item, dict):
            continue
        fid = str(item.get("fid") or "").strip()
        word = str(item.get("word") or "").strip()
        func_name = functions.get(fid, "")
        fm_index = fm_by_func.get(func_name, {})
        src = item.get("source") if isinstance(item.get("source"), dict) else {}
        is_adapted = str(src.get("type") or "").strip().lower() == "adapted"

        fm, how = _match_item_failure(
            item, fm_index,
            retrieve=retrieve, domain=domain, func_name=func_name,
            fetch_cache=fm_fetch_cache,
        )
        if is_adapted:
            # 改编条目不覆盖文本，但其参照的历史条目不允许在第1.5步重复追加
            ref_key = str(src.get("ref_id") or "").strip()
            if not ref_key and fm is not None and fm.get("failure_id"):
                ref_key = fm["failure_id"]
            if ref_key:
                covered_pairs.add((fid, ref_key))
            continue
        if fm is None:
            if how == "ambiguous":
                logger.warning(
                    "[hara_prepare] 失效模式消歧失败，保留规划原文（fid=%s，word=%s，"
                    "ref_id=%s）：同失效词存在多个知识库条目且无可靠匹配信号",
                    fid, word, str(src.get("ref_id") or "").strip(),
                )
            continue
        item["malfunction_behavior"] = fm["malfunction_behavior"]
        item["vehicle_hazard"] = fm["vehicle_hazard"]
        src["type"] = "reused"
        if fm.get("failure_id"):
            src["ref_id"] = fm["failure_id"]
        if not str(src.get("project") or "").strip() and not fm.get("_synthesized"):
            src_file = str(
                (((fm["chunk"].get("meta") or {}).get("source") or {}).get("file"))
                or ""
            ).strip()
            if src_file:
                src["project"] = src_file
        item["source"] = src
        # 同步失效矩阵：知识库命中的失效必须标记为选中
        m = matrix_by_fid.get(fid)
        if m is not None:
            sel = m.get("selections") if isinstance(m.get("selections"), dict) else {}
            sel[str(fm.get("word") or word)] = True
            m["selections"] = sel
        if fm.get("failure_id"):
            covered_pairs.add((fid, fm["failure_id"]))
        stats["fm_covered"] += 1

    # 第1.5步：补全知识库有但 LLM 未生成的失效模式（按 failure_id 逐条判定，宁多勿漏）
    for fid, func_name in functions.items():
        for key, fm in fm_by_func.get(func_name, {}).items():
            if (fid, key) in covered_pairs or fm.get("_synthesized"):
                continue
            src_file = str(
                (((fm["chunk"].get("meta") or {}).get("source") or {}).get("file"))
                or ""
            ).strip()
            items.append({
                "fid": fid,
                "word": fm["word"],
                "malfunction_behavior": fm["malfunction_behavior"],
                "vehicle_hazard": fm["vehicle_hazard"],
                "scenarios": [],
                "source": {
                    "type": "reused",
                    "project": src_file,
                    "ref_id": fm["failure_id"],
                },
                "source_note": f"失效模式沿用知识库 {fm['failure_id']}",
            })
            # 同步失效矩阵：补全的失效必须标记为选中
            m = matrix_by_fid.get(fid)
            if m is not None:
                sel = m.get("selections") if isinstance(m.get("selections"), dict) else {}
                sel[fm["word"]] = True
                m["selections"] = sel
            covered_pairs.add((fid, key))
            stats["fm_appended"] += 1

    # 第2步：场景历史骨架（对所有 hazop_items 含追加的，宁多勿漏）
    for item in items:
        if not isinstance(item, dict):
            continue
        fid = str(item.get("fid") or "").strip()
        word = str(item.get("word") or "").strip()
        func_name = functions.get(fid, "")
        if not func_name or not word:
            continue
        key = (func_name, word)
        if key not in union_cache:
            try:
                union_cache[key] = _retrieve_event_union(
                    retrieve, domain, func_name, word, "", kb_cfg,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[hara_prepare] 场景骨架召回降级（%s/%s）：%s",
                    func_name, word, exc,
                )
                union_cache[key] = []
        by_id: dict[str, dict] = {}
        for c in union_cache[key]:
            if not isinstance(c, dict):
                continue
            meta = c.get("meta") or {}
            # 只保留属于当前失效词的历史事件，避免同功能多失效切片重复沿用同一批事件
            c_word = str(meta.get("failure_type") or "").strip()
            if c_word and c_word != word:
                continue
            hid = str(meta.get("hzrd_id") or "").strip()
            if hid:
                by_id[hid] = c
        if not by_id:
            continue
        scenarios = item.get("scenarios")
        if not isinstance(scenarios, list):
            scenarios = []
        existing_norms = {
            _scene_norm(str(sc.get("scene_text") or ""))
            for sc in scenarios if isinstance(sc, dict)
        }
        used: set[str] = set()
        for sc in scenarios:
            if not isinstance(sc, dict):
                continue
            href = str(sc.get("history_ref") or "").strip()
            if not href:
                continue
            meta = (by_id.get(href) or {}).get("meta") or {}
            scene_text = str(meta.get("scene") or "").strip()
            if scene_text:
                sc["scene_text"] = scene_text
                used.add(href)
                stats["scene_aligned"] += 1
            else:
                sc.pop("history_ref", None)  # 引用无效：剥离标记，避免误导评级层
        for hid, c in by_id.items():
            if hid in used:
                continue
            scene_text = str((c.get("meta") or {}).get("scene") or "").strip()
            norm = _scene_norm(scene_text)
            if not scene_text or norm in existing_norms:
                continue
            scenarios.append({"history_ref": hid, "scene_text": scene_text})
            existing_norms.add(norm)
            stats["scene_appended"] += 1
        if scenarios:
            item["scenarios"] = scenarios
    logger.info(
        "[hara_prepare] 规划后处理：功能 Feature 覆盖 %d 条，失效模式原文覆盖 %d 条，"
        "知识库失效补全 %d 条，场景原文对齐 %d 条，历史场景补录 %d 条",
        stats.get("func_covered", 0), stats["fm_covered"], stats.get("fm_appended", 0),
        stats["scene_aligned"], stats["scene_appended"],
    )
    return stats


# ── 评级结果评审钩子：全量复核 source 标注，疑似项回炉 LLM 二次校验 ──

_REVIEW_SCENE_SIM_DEFAULT = 0.70   # 漏标沿用判定：场景相似度阈值
_REVIEW_REF_SCENE_SIM = 0.50       # 已引用场景过低的提示阈值


# ── 事件块原文物化（reused/adapted 事件内容以知识库原文为准，LLM 只补差） ──

def _parse_event_assessment(content: str) -> dict:
    """解析事件分块正文：场景/描述/S/E/C 及理由/结论 ASIL/安全目标组。

    历史留白口径：S=0 时 E/C 不评估（输出 null、理由空串）；
    ASIL=QM 或值为"-"/"无…"时安全目标/安全状态/FTTI 置空串。
    """
    text = str(content or "")
    out = {
        "scene": "", "desc": "",
        "S": None, "s_reason": "", "E": None, "e_reason": "",
        "C": None, "c_reason": "",
        "asil": "", "sg_text": "", "safe_state": "", "ftti": "",
    }
    m = re.search(r"运行场景：(.*)", text)
    if m:
        out["scene"] = m.group(1).strip()
    m = re.search(r"危害事件描述：(.*)", text)
    if m:
        out["desc"] = m.group(1).strip()
    seg_m = re.search(r"- 严重度 S=.*?(?=\n\n结论：|\Z)", text, re.S)
    if seg_m:
        for raw in seg_m.group(0).splitlines():
            line = raw.strip().lstrip("-").strip()
            m = re.match(r"严重度\s*S=(\d*)[:：]?(.*)", line)
            if m:
                out["S"] = int(m.group(1)) if m.group(1) else None
                out["s_reason"] = m.group(2).strip()
                continue
            m = re.match(r"暴露概率\s*E=(\d*)[:：]?(.*)", line)
            if m:
                out["E"] = int(m.group(1)) if m.group(1) else None
                out["e_reason"] = m.group(2).strip()
                continue
            m = re.match(r"可控性\s*C=(\d*)[:：]?(.*)", line)
            if m:
                out["C"] = int(m.group(1)) if m.group(1) else None
                out["c_reason"] = m.group(2).strip()
                continue
            m = re.match(r"(S\d+[:：].+)", line)
            if m and out["S"] is not None:
                out["s_reason"] = "；".join(
                    x for x in (out["s_reason"], m.group(1).strip()) if x
                )
                continue
            m = re.match(r"(E\d+[:：].+)", line)
            if m and out["E"] is not None:
                out["e_reason"] = "；".join(
                    x for x in (out["e_reason"], m.group(1).strip()) if x
                )
                continue
            m = re.match(r"(C\d+[:：].+)", line)
            if m and out["C"] is not None:
                out["c_reason"] = "；".join(
                    x for x in (out["c_reason"], m.group(1).strip()) if x
                )
                continue
    m = re.search(r"结论：\s*ASIL\s*=\s*(\S+)", text)
    if m:
        out["asil"] = m.group(1).strip().strip("。")
    m = re.search(r"安全目标：(.*)", text)
    sg = m.group(1).strip() if m else ""
    m = re.search(r"安全状态：(.*)", text)
    safe = m.group(1).strip() if m else ""
    m = re.search(r"FTTI：(.*)", text)
    ftti = m.group(1).strip() if m else ""
    is_qm = not out["asil"] or out["asil"].upper() == "QM"
    out["sg_text"] = "" if is_qm or sg == "-" or sg.startswith("无") else sg
    out["safe_state"] = "" if is_qm or safe == "-" else safe
    out["ftti"] = "" if is_qm or ftti == "-" else ftti
    return out


def _apply_materialize(ev: dict, chunk: dict, scope: str) -> bool:
    """把知识库事件块原文物化到事件 dict；成功 True，解析失败 False（保留原输出）。

    沿用（reused）= 100% 全字段覆盖：场景/描述/S/E/C/理由/安全目标组全部按库原文回填；
    改编（adapted）= 仅覆盖场景原文与安全目标组，保留 LLM 的 S/E/C/理由/描述
    （改编的正是评级，沿用部分仍以库为准）。

    S/E/C 优先从 meta 取（可靠），content 解析作为回退；理由从 content 解析。
    """
    meta = chunk.get("meta") or {}
    parsed = _parse_event_assessment(str(chunk.get("content") or ""))
    # S/E/C：meta 优先，解析回退（历史留白 S=0→E/C 为 None 时仍需覆盖）
    sec = {
        "S": _sec_norm(meta.get("S")) if _sec_norm(meta.get("S")) is not None else parsed["S"],
        "E": _sec_norm(meta.get("E")) if _sec_norm(meta.get("E")) is not None else parsed["E"],
        "C": _sec_norm(meta.get("C")) if _sec_norm(meta.get("C")) is not None else parsed["C"],
    }
    if scope == "full" and sec["S"] is None and _sec_norm(meta.get("S")) is None and parsed["S"] is None:
        return False  # meta 与解析均无 S：视为解析失败，回退 LLM 原输出
    scene = str(meta.get("scene") or "").strip() or parsed["scene"]
    updates: dict = {}
    if scene:
        updates["scenario_text"] = scene
    if scope == "full":
        if parsed["desc"]:
            updates["event_description"] = parsed["desc"]
        updates["S"] = sec["S"]
        updates["s_reason"] = parsed["s_reason"]
        updates["E"] = sec["E"]
        updates["e_reason"] = parsed["e_reason"]
        updates["C"] = sec["C"]
        updates["c_reason"] = parsed["c_reason"]
    updates["sg_text"] = parsed["sg_text"]
    updates["safe_state"] = parsed["safe_state"]
    updates["ftti"] = parsed["ftti"]
    ev.update(updates)
    src = ev.get("source") if isinstance(ev.get("source"), dict) else {}
    if src and not str(src.get("project") or "").strip():
        src_file = str(((meta.get("source") or {}).get("file")) or "").strip()
        if src_file:
            src["project"] = src_file
    return True


def _sec_norm(value) -> int | None:
    """S/E/C 归一为 int|None：历史留白（空串）与输出 null 视为等价"未评估"。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ISO 26262-3 Table 4 确定性 ASIL 反算。
# 注意：必须与 scripts/generate_hara.py 的 _ASIL_TABLE/_asil_of 保持一致，
# 评审钩子据此预判"显著事件（ASIL≥A）必须挂安全目标"。
_REVIEW_ASIL_TABLE = {
    (1, 1): {1: "QM", 2: "QM", 3: "QM"},
    (1, 2): {1: "QM", 2: "QM", 3: "QM"},
    (1, 3): {1: "QM", 2: "QM", 3: "A"},
    (1, 4): {1: "QM", 2: "A", 3: "B"},
    (2, 1): {1: "QM", 2: "QM", 3: "QM"},
    (2, 2): {1: "QM", 2: "QM", 3: "A"},
    (2, 3): {1: "QM", 2: "A", 3: "B"},
    (2, 4): {1: "A", 2: "B", 3: "C"},
    (3, 1): {1: "QM", 2: "QM", 3: "A"},
    (3, 2): {1: "QM", 2: "A", 3: "B"},
    (3, 3): {1: "A", 2: "B", 3: "C"},
    (3, 4): {1: "B", 2: "C", 3: "D"},
}


def _event_asil(ev: dict) -> str | None:
    """按渲染器同一规则由 S/E/C 反算事件 ASIL。

    返回 "QM"/"A"/"B"/"C"/"D"；S/E/C 缺失或越界（渲染器标 CHECK）时返回 None。
    """
    s = _sec_norm(ev.get("S"))
    e = _sec_norm(ev.get("E"))
    c = _sec_norm(ev.get("C"))
    # 经验做法：S=0 直接 QM（E/C 可不评估）；S>0 且 E=0 直接 QM（C 可不评估）
    if s == 0 or (s is not None and e == 0):
        return "QM"
    if s is None or e is None or c is None:
        return None
    if not (0 <= s <= 3 and 0 <= e <= 4 and 0 <= c <= 3):
        return None
    if c == 0:
        return "QM"
    return _REVIEW_ASIL_TABLE[(s, e)][c]


def _scene_norm(text: str) -> str:
    return re.sub(
        r"[\s，。、；：,.;:!？?()（）\"'“”‘’\-—]+", "", str(text or "")
    ).lower()


def _scene_sim(a, b) -> float:
    na, nb = _scene_norm(a), _scene_norm(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _cand_summary(meta: dict, sim: float | None = None) -> dict:
    info = {
        "ref_id": str(meta.get("hzrd_id") or ""),
        "scene": str(meta.get("scene") or ""),
        "S": _sec_norm(meta.get("S")),
        "E": _sec_norm(meta.get("E")),
        "C": _sec_norm(meta.get("C")),
        "asil": str(meta.get("asil") or ""),
    }
    if sim is not None:
        info["similarity"] = round(sim, 2)
    return info


def _review_one(ev: dict, candidates: dict[str, dict],
                threshold: float) -> tuple[list[str], dict | None]:
    """单事件比对：返回（疑点列表, 命中的候选历史事件摘要或 None）。

    candidates：{hzrd_id: chunk}（chunk 含 meta+content，供物化与比对共用）。
    """
    src = ev.get("source")
    if not isinstance(src, dict):
        return ["事件缺少 source 字段"], None
    stype = str(src.get("type") or "").strip().lower()
    ref = str(src.get("ref_id") or "").strip()

    if stype == "new":
        ev_s = _sec_norm(ev.get("S"))
        if ev_s is None:
            return [], None
        ev_e, ev_c = _sec_norm(ev.get("E")), _sec_norm(ev.get("C"))
        best_id, best_meta, best_sim = "", None, 0.0
        for hid, chunk in candidates.items():
            meta = chunk.get("meta") or {}
            if _sec_norm(meta.get("S")) != ev_s:
                continue
            if _sec_norm(meta.get("E")) != ev_e or _sec_norm(meta.get("C")) != ev_c:
                continue
            sim = _scene_sim(meta.get("scene"), ev.get("scenario_text"))
            if sim > best_sim:
                best_id, best_meta, best_sim = hid, meta, sim
        if best_meta is not None and best_sim >= threshold:
            his_s = _sec_norm(best_meta.get("S"))
            his_e = _sec_norm(best_meta.get("E"))
            his_c = _sec_norm(best_meta.get("C"))
            issue = (
                f"疑似漏标沿用：与历史事件 {best_id} 实质一致（场景相似度 "
                f"{best_sim:.2f}，S/E/C 一致，历史 S{his_s}/E{his_e}/C{his_c}）。"
                f"应改为 reused 并抄录 ref_id={best_id}；除非你能指出实质性场景差异"
                "（碰撞对象/速度区间/附着条件等），此时应为 adapted 并在理由中说明"
            )
            return [issue], _cand_summary(best_meta, best_sim)
        return [], None

    if stype in ("reused", "adapted"):
        if not ref:
            return [
                f"source.type={stype} 但 ref_id 为空：应补历史事件 ID；"
                "确无匹配应改判 new"
            ], None
        chunk = candidates.get(ref)
        if chunk is None:
            return [
                f"ref_id={ref} 不在本次召回的历史事件中：请核实该 ID 是否真实存在；"
                "不存在应改用正确 ID 或改判 new（禁止编造来源）"
            ], None
        meta = chunk.get("meta") or {}
        issues: list[str] = []
        his_s = _sec_norm(meta.get("S"))
        his_e = _sec_norm(meta.get("E"))
        his_c = _sec_norm(meta.get("C"))
        diff = [
            label for label, his, mine in
            (("S", his_s, _sec_norm(ev.get("S"))),
             ("E", his_e, _sec_norm(ev.get("E"))),
             ("C", his_c, _sec_norm(ev.get("C"))))
            if his != mine
        ]
        if diff:
            issues.append(
                f"所引历史 {ref} 的 {'/'.join(diff)} 与你的输出不一致"
                f"（历史 S{his_s}/E{his_e}/C{his_c}）：无实质场景差异应改回历史"
                "评级并保持 reused；有实质差异应为 adapted 并在理由中说明"
            )
        sim = _scene_sim(meta.get("scene"), ev.get("scenario_text"))
        if sim < _REVIEW_REF_SCENE_SIM:
            scene_head = str(meta.get("scene") or "")[:40]
            issues.append(
                f"场景与所引历史 {ref}（「{scene_head}」）相似度仅 {sim:.2f}，"
                "请核实引用是否正确"
            )
        return issues, (_cand_summary(meta, sim) if issues else None)

    return [f"未知 source.type={stype!r}"], None


def review_events(ctx) -> dict:
    """评审钩子：物化 → 疑点检测（source 标注 + 安全目标缺口）→ 确定性传播/
    复核 LLM → 应用 → 再物化/再传播。

    第一步物化（纯代码，仅在有历史召回时）：reused 事件全字段按知识库事件块
    原文回填、adapted 事件覆盖场景原文与安全目标组——沿用内容以库原文为准，
    LLM 只补差；
    第二步代码级检测两类疑点：
      A. source 标注疑点（漏标沿用/虚引 ID/评级与所引不一致/场景与所引不符），
         仅在本切片有历史召回时检测；
      B. 安全目标缺口（与历史召回无关，始终检测）：按 S/E/C 矩阵确定性反算
         ASIL≥A 的显著事件，若 sg_text/safe_state/ftti 缺失，**先在同一失效
         单元内确定性传播**——同单元显著事件本应共用同一安全目标，兄弟事件
         已有完整三件套时直接继承，零 LLM 开销；仅整个单元都无安全目标时，
         才连同邻近事件回炉 LLM 补全一次；
    改不改、怎么改由复核 LLM 决定。sg_gap 类只允许白名单回填安全目标三字段，
    防止复核顺带改动 S/E/C；source 类保持整事件合并。物化/复核失败均软降级
    保留原结果；补全/传播后仍缺失的，由渲染器标黄并在交付摘要中提示人工补全。
    """
    events = ctx.events if isinstance(ctx.events, list) else []
    if not events:
        return {"events": events, "usage": {}}

    unit = ctx.unit if isinstance(ctx.unit, dict) else {}
    fid = str(unit.get("fid") or "").strip()
    func_name = ""
    for fn in (ctx.plan_parsed or {}).get("functions") or []:
        if isinstance(fn, dict) and str(fn.get("fid") or "").strip() == fid:
            func_name = str(fn.get("vehicle_function") or "").strip()
            break
    word = str(unit.get("word") or "").strip()

    candidates: dict[str, dict] = {}  # hzrd_id -> chunk（meta+content 共用）
    for c in ctx.chunks or []:
        if not isinstance(c, dict):
            continue
        meta = c.get("meta") or {}
        hid = str(meta.get("hzrd_id") or "").strip()
        if not hid:
            continue
        if func_name and str(meta.get("func") or "").strip() != func_name:
            continue
        if word and str(meta.get("failure_type") or "").strip() != word:
            continue
        candidates[hid] = c

    map_cfg = (ctx.skill_cfg.get("execution") or {}).get("map") or {}
    review_cfg = map_cfg.get("review") or {}
    threshold = float(
        review_cfg.get("scene_sim_threshold") or _REVIEW_SCENE_SIM_DEFAULT
    )

    def _materialize_all() -> int:
        """物化回填：reused 全字段 / adapted 场景+安全目标组；返回成功条数。"""
        count = 0
        for ev in events:
            if not isinstance(ev, dict):
                continue
            src = ev.get("source") if isinstance(ev.get("source"), dict) else {}
            stype = str(src.get("type") or "").strip().lower()
            ref = str(src.get("ref_id") or "").strip()
            if stype not in ("reused", "adapted") or not ref:
                continue
            chunk = candidates.get(ref)
            if chunk is None:
                continue
            try:
                if _apply_materialize(
                    ev, chunk, "full" if stype == "reused" else "adapted"
                ):
                    count += 1
            except Exception as exc:  # noqa: BLE001 —— 物化失败保留 LLM 原输出
                ctx.logger.warning(
                    "[hara_prepare] 事件物化失败（ref=%s，保留原输出）：%s", ref, exc,
                )
        return count

    mat_count = _materialize_all()
    ctx.logger.info(
        "[hara_prepare] 评级评审（%s/%s）：%d 条事件，历史候选 %d 块，"
        "物化回填 %d 条（reused 全字段/adapted 场景+安全目标组）",
        fid or "?", word, len(events), len(candidates), mat_count,
    )

    suspects: list[dict] = []
    # A 类：source 标注疑点（无历史召回时不检测，避免把"查无候选"误判为虚引）
    if candidates:
        for i, ev in enumerate(events):
            if not isinstance(ev, dict):
                continue
            issues, matched = _review_one(ev, candidates, threshold)
            if issues:
                suspects.append({
                    "index": i, "kind": "source_review",
                    "issues": issues, "matched_history": matched,
                })

    # B 类：安全目标缺口（ASIL≥A 的显著事件缺 sg_text/safe_state/ftti）
    _SG_FIELDS = ("sg_text", "safe_state", "ftti")
    _SG_LABEL = {"sg_text": "安全目标", "safe_state": "安全状态", "ftti": "FTTI"}

    def _significant(ev: dict) -> bool:
        return isinstance(ev, dict) and _event_asil(ev) not in (None, "QM")

    def _propagate_sg() -> int:
        """同失效单元内确定性传播安全目标三件套，返回补齐的事件数。

        评级切片即一个 hazop_item（同一功能异常表现/整车危害），其下所有
        ASIL≥A 事件按方法论必须共用**完全相同**的安全目标（渲染器也按目标
        文字合并整车安全目标）。因此单元内任一显著事件已挂接完整三件套时，
        其余缺口无需再问 LLM，直接继承；只填空字段，不覆盖已有内容。
        """
        trio = None
        for ev in events:
            if not _significant(ev):
                continue
            vals = tuple(str(ev.get(k) or "").strip() for k in _SG_FIELDS)
            if all(vals):
                trio = vals
                break
        if trio is None:
            return 0
        filled = 0
        for ev in events:
            if not _significant(ev):
                continue
            changed = False
            for k, v in zip(_SG_FIELDS, trio):
                if v and not str(ev.get(k) or "").strip():
                    ev[k] = v
                    changed = True
            if changed:
                filled += 1
        return filled

    # 先确定性传播：同单元兄弟事件已有完整安全目标时直接补齐，不消耗 LLM 调用
    sg_propagated = _propagate_sg()

    # 传播后仍缺的缺口才回炉 LLM（典型为整个单元都没挂安全目标）
    sg_gap_indexes: set[int] = set()
    for i, ev in enumerate(events):
        if not _significant(ev):
            continue
        missing = [
            _SG_LABEL[k] for k in _SG_FIELDS
            if not str(ev.get(k) or "").strip()
        ]
        if not missing:
            continue
        sg_gap_indexes.add(i)
        asil = _event_asil(ev)
        suspects.append({
            "index": i, "kind": "sg_gap",
            "issues": [
                f"安全目标缺口：系统按 ISO 26262 矩阵由该事件 S/E/C 判定 "
                f"ASIL={asil}（显著事件，必须建立安全目标），但"
                f"{'/'.join(missing)}为空，疑似评级层遗漏。请补全 "
                f"sg_text/safe_state/ftti 三项：sg_text 用「防止……」句式，"
                "并与 context_events 中同危害机理事件的安全目标文字保持完全"
                "一致；safe_state 给出可达到的安全状态；ftti 给时间要求"
                "（无数据时带 TBD）。禁止修改 S/E/C 与其他字段；系统以矩阵"
                "判定为准，宁可补齐由系统/人工复核，也不要留空。"
            ],
            "matched_history": None,
            "computed_asil": asil,
        })

    ctx.logger.info(
        "[hara_prepare] 评级评审（%s/%s）：%d 条事件，source 标注疑点 %d 条，"
        "安全目标缺口 %d 条（单元内确定性传播补齐 %d 条，待回炉 %d 条）",
        fid or "?", word, len(events),
        len(suspects) - len(sg_gap_indexes),
        sg_propagated + len(sg_gap_indexes), sg_propagated, len(sg_gap_indexes),
    )
    if not suspects:
        return {"events": events, "usage": {}}

    # 同切片邻近事件（同一功能失效单元）作为补全上下文：同危害机理事件应共用
    # 完全相同的安全目标文字，供复核 LLM 对齐
    context_events = []
    for i, ev in enumerate(events):
        if not isinstance(ev, dict) or i in sg_gap_indexes:
            continue
        context_events.append({
            "index": i,
            "scenario_text": str(ev.get("scenario_text") or "")[:200],
            "S": _sec_norm(ev.get("S")),
            "E": _sec_norm(ev.get("E")),
            "C": _sec_norm(ev.get("C")),
            "asil": _event_asil(ev),
            "sg_text": str(ev.get("sg_text") or ""),
            "safe_state": str(ev.get("safe_state") or ""),
            "ftti": str(ev.get("ftti") or ""),
        })
    system_prompt = ctx.build_stage_prompt(review_cfg)
    payload = {
        "unit": {
            "fid": fid,
            "word": word,
            "malfunction_behavior": str(unit.get("malfunction_behavior") or ""),
            "vehicle_hazard": str(unit.get("vehicle_hazard") or ""),
        },
        "suspects": suspects,
        "current_events": [events[s["index"]] for s in suspects],
        "context_events": context_events,
    }
    user_message = (
        "以下 HARA 事件经系统代码级校验存在疑点（suspects 中 kind 标明类型："
        "source_review=source 标注疑点，sg_gap=显著事件缺少安全目标）。"
        "请严格按系统提示词逐条复核，只对疑点事件输出修正后的完整事件"
        "（index 必须与 suspects 一致，不得新增或遗漏）。\n"
        + json.dumps(payload, ensure_ascii=False, indent=1)
    )
    max_tokens = review_cfg.get("max_tokens")
    parsed, response = ctx.invoke_json_stage(
        ctx.bind_json_model(max_tokens),
        system_prompt,
        user_message,
        stage_name=f"评级复核(切片{ctx.index + 1})",
        max_tokens=max_tokens,
    )
    usage = ctx.extract_usage(response)

    fixed = parsed.get("events")
    applied = 0
    sg_filled = 0
    if isinstance(fixed, list):
        by_index: dict[int, dict] = {}
        for fe in fixed:
            if isinstance(fe, dict) and isinstance(fe.get("index"), int):
                by_index[fe["index"]] = fe
        for s in suspects:
            i = s["index"]
            fe = by_index.get(i)
            if not isinstance(fe, dict):
                continue
            if s.get("kind") == "sg_gap":
                # 白名单回填：只接受非空安全目标三字段，杜绝复核改动 S/E/C
                changed = False
                for k in _SG_FIELDS:
                    v = fe.get(k)
                    if isinstance(v, str) and v.strip():
                        events[i][k] = v.strip()
                        changed = True
                if changed:
                    sg_filled += 1
                    applied += 1
                continue
            merged = dict(events[i])          # 原事件兜底，防复核输出缺字段
            for k, v in fe.items():
                if k != "index":
                    merged[k] = v
            events[i] = merged
            applied += 1

    mat_count2 = _materialize_all()  # 复核改判后按库原文再校正一遍
    # LLM 补出的安全目标（或再物化从历史回填的）在单元内再传播一轮，
    # 覆盖未被模型逐条回填的同单元缺口
    sg_propagated2 = _propagate_sg()
    # 复核+传播+再物化后仍缺安全目标的显著事件：留给渲染器标黄与摘要人工提示
    sg_still = sum(
        1 for ev in events
        if _significant(ev) and any(
            not str(ev.get(k) or "").strip() for k in _SG_FIELDS
        )
    )
    ctx.logger.info(
        "[hara_prepare] 评级复核（%s/%s）：疑似 %d 条（安全目标缺口 %d），"
        "模型回填 %d 条（其中安全目标 %d 条），回炉后单元内再传播 %d 条，"
        "仍缺 %d 条，复核后再物化 %d 条",
        fid or "?", word, len(suspects), len(sg_gap_indexes),
        applied, sg_filled, sg_propagated2, sg_still, mat_count2,
    )
    return {"events": events, "usage": usage}
