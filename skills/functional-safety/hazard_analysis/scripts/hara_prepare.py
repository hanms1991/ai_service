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
钩子内任何异常由引擎捕获并软降级，不阻断技能执行。
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor

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


def prepare(ctx) -> dict:
    """识别相关项/整车功能，按功能双路精准检索，返回规划层知识注入块。"""
    skill_cfg = ctx.skill_cfg
    exec_cfg = skill_cfg.get("execution") or {}
    prepare_cfg = exec_cfg.get("prepare") or {}
    identify_cfg = prepare_cfg.get("identify") or {}
    if not identify_cfg.get("enabled"):
        return {"kb_block": "", "usage": {}, "identify_info": None}

    info, response = _run_identify_stage(ctx, identify_cfg)
    kb_block = _targeted_layered_retrieval(ctx, skill_cfg, info)
    logger.info(
        "[hara_prepare] 识别层精准检索注入：%d 字符；识别功能=%s",
        len(kb_block), info.get("functions"),
    )
    return {
        "kb_block": kb_block,
        "usage": ctx.extract_usage(response),
        "identify_info": info,
    }


# ── Stage 0：识别相关项与整车功能清单 ──────────────────────────────

def _run_identify_stage(ctx, identify_cfg: dict):
    """识别层：只从相关项信息中识别 item 基本信息与整车功能名称清单。

    不注入知识库、不做安全分析，输出用于驱动功能级精准检索。
    functions 为空时抛异常，由引擎统一软降级。
    """
    system_prompt = ctx.build_stage_prompt(identify_cfg)
    user_message = (
        ctx.rendered
        + ctx.doc_block
        + "\n\n本阶段只做识别：列出相关项基本信息与【整车层级功能】名称清单，"
        "不做失效/危害/评级分析；严格只输出约定的紧凑 JSON 对象，不要输出解释。"
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
