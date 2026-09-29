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


# ── 评级结果评审钩子：全量复核 source 标注，疑似项回炉 LLM 二次校验 ──

_REVIEW_SCENE_SIM_DEFAULT = 0.70   # 漏标沿用判定：场景相似度阈值
_REVIEW_REF_SCENE_SIM = 0.50       # 已引用场景过低的提示阈值


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
    """单事件比对：返回（疑点列表, 命中的候选历史事件摘要或 None）。"""
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
        for hid, meta in candidates.items():
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
        meta = candidates.get(ref)
        if meta is None:
            return [
                f"ref_id={ref} 不在本次召回的历史事件中：请核实该 ID 是否真实存在；"
                "不存在应改用正确 ID 或改判 new（禁止编造来源）"
            ], None
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
    """评审钩子：代码复核本切片全部事件的 source 标注，疑似项回炉 LLM 二次校验。

    代码只负责"找疑点"（漏标沿用/虚引 ID/评级与所引不一致/场景与所引不符），
    改不改、怎么改由复核 LLM 决定；复核失败软降级保留原结果。
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

    candidates: dict[str, dict] = {}
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
        candidates[hid] = meta
    if not candidates:
        return {"events": events, "usage": {}}

    map_cfg = (ctx.skill_cfg.get("execution") or {}).get("map") or {}
    review_cfg = map_cfg.get("review") or {}
    threshold = float(
        review_cfg.get("scene_sim_threshold") or _REVIEW_SCENE_SIM_DEFAULT
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
    ctx.logger.info(
        "[hara_prepare] 评级复核（%s/%s）：疑似 %d 条，模型修正回填 %d 条",
        fid or "?", word, len(suspects), applied,
    )
    return {"events": events, "usage": usage}
