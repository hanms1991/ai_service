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
import json
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
    seen_words: set[str] = set()
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
        if not word or word in seen_words:
            continue
        fid = str(meta.get("failure_id") or "").strip()
        texts = _fm_original_texts(str(c.get("content") or ""))
        if not texts[0] and not texts[1]:
            continue
        seen_words.add(word)
        result.append({
            "word": word,
            "failure_id": fid,
            "malfunction_behavior": texts[0],
            "vehicle_hazard": texts[1],
            "chunk": c,
        })
    return result


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
    stats = {
        "func_covered": 0, "fm_covered": 0, "fm_appended": 0,
        "scene_aligned": 0, "scene_appended": 0,
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

    # 第0步：对每个功能检索知识库全部历史失效模式（一次检索覆盖11失效词）
    fm_by_func: dict[str, dict[str, dict]] = {}
    for func_name in functions.values():
        if not func_name:
            continue
        fms = _retrieve_function_failure_modes(retrieve, domain, func_name)
        fm_by_func[func_name] = {fm["word"]: fm for fm in fms}
        logger.info(
            "[hara_prepare] 知识库失效模式检索：%s 命中 %d 个失效",
            func_name, len(fms),
        )

    union_cache: dict[tuple[str, str], list[dict]] = {}
    covered_pairs: set[tuple[str, str]] = set()

    # 第1步：失效模式原文覆盖（reused/缺省条目：知识库有则强制用原文，一字不差；
    # adapted=LLM 有意改编，保留其文本不覆盖，与 HARA 事件层 _materialize 的
    # full/adapted 语义对齐；改编条目同样计入 covered_pairs，第1.5步不再重复追加）
    for item in items:
        if not isinstance(item, dict):
            continue
        fid = str(item.get("fid") or "").strip()
        word = str(item.get("word") or "").strip()
        func_name = functions.get(fid, "")
        fm = fm_by_func.get(func_name, {}).get(word)
        if not fm:
            continue
        src = item.get("source") if isinstance(item.get("source"), dict) else {}
        if str(src.get("type") or "").strip().lower() == "adapted":
            covered_pairs.add((fid, word))
            continue
        item["malfunction_behavior"] = fm["malfunction_behavior"]
        item["vehicle_hazard"] = fm["vehicle_hazard"]
        src["type"] = "reused"
        src["ref_id"] = fm["failure_id"]
        if not str(src.get("project") or "").strip():
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
            sel[word] = True
            m["selections"] = sel
        covered_pairs.add((fid, word))
        stats["fm_covered"] += 1

    # 第1.5步：补全知识库有但 LLM 未生成的失效模式（宁多勿漏）
    for fid, func_name in functions.items():
        fms = fm_by_func.get(func_name, {})
        for word, fm in fms.items():
            if (fid, word) in covered_pairs:
                continue
            src_file = str(
                (((fm["chunk"].get("meta") or {}).get("source") or {}).get("file"))
                or ""
            ).strip()
            items.append({
                "fid": fid,
                "word": word,
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
                sel[word] = True
                m["selections"] = sel
            covered_pairs.add((fid, word))
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
    """评审钩子：物化 → 疑点检测 → 复核 LLM → 应用 → 再物化。

    第一步物化（纯代码）：reused 事件全字段按知识库事件块原文回填、adapted
    事件覆盖场景原文与安全目标组——沿用内容以库原文为准，LLM 只补差；
    第二步代码比对找疑点（漏标沿用/虚引 ID/评级与所引不一致/场景与所引不符），
    改不改、怎么改由复核 LLM 决定；复核应用后再物化一遍（改判 reused/adapted
    的以库原文校正）。物化/复核失败均软降级保留原结果。
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
    if not candidates:
        return {"events": events, "usage": {}}

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
        "[hara_prepare] 评级评审（%s/%s）：%d 条事件，物化回填 %d 条"
        "（reused 全字段/adapted 场景+安全目标组）",
        fid or "?", word, len(events), mat_count,
    )

    suspects: list[dict] = []
    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            continue
        issues, matched = _review_one(ev, candidates, threshold)
        if issues:
            suspects.append(
                {"index": i, "issues": issues, "matched_history": matched}
            )
    ctx.logger.info(
        "[hara_prepare] 评级评审（%s/%s）：%d 条事件，疑似标注问题 %d 条",
        fid or "?", word, len(events), len(suspects),
    )
    if not suspects:
        return {"events": events, "usage": {}}

    system_prompt = ctx.build_stage_prompt(review_cfg)
    payload = {
        "suspects": suspects,
        "current_events": [events[s["index"]] for s in suspects],
    }
    user_message = (
        "以下事件的 source 标注经系统比对存在疑点，请按系统提示词逐条复核，"
        "输出修正后的完整事件（index 必须与 suspects 一致）。\n"
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
    if isinstance(fixed, list):
        by_index: dict[int, dict] = {}
        for fe in fixed:
            if isinstance(fe, dict) and isinstance(fe.get("index"), int):
                by_index[fe["index"]] = fe
        for s in suspects:
            i = s["index"]
            fe = by_index.get(i)
            if isinstance(fe, dict):
                merged = dict(events[i])          # 原事件兜底，防复核输出缺字段
                for k, v in fe.items():
                    if k != "index":
                        merged[k] = v
                events[i] = merged
                applied += 1

    mat_count2 = _materialize_all()  # 复核改判后按库原文再校正一遍
    ctx.logger.info(
        "[hara_prepare] 评级复核（%s/%s）：疑似 %d 条，模型修正回填 %d 条，"
        "复核后再物化 %d 条",
        fid or "?", word, len(suspects), applied, mat_count2,
    )
    return {"events": events, "usage": usage}
