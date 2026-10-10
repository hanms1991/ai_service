# -*- coding: utf-8 -*-
"""HARA 线性管道（pipeline 执行模式）：检索 → LLM 判断 → 物化存 JSON → … → 渲染。

这是 hazard_analysis 技能专属逻辑，通过 execution.pipeline.script 由引擎动态
加载，单一 run(ctx) 入口；技能脚本自行驱动 LLM 调用与知识库检索
（引擎侧契约见 agents/capability_registry.py 的 PipelineContext）。

6 步线性管道（docs/hara_pipeline_redesign.md）：
  Step 1 identify        LLM 识别相关项与整车功能清单（stage_identify.md）
  Step 2 function_match  KB 功能清单候选 → LLM 判断沿用/改编/新增 → 物化（02 的 KB 功能名）
  Step 3 fm_match        KB 失效模式候选 × 11 失效词 → LLM 判断 → 物化
  Step 4 event_match     KB 事件候选 → LLM 判断（reused 只给 ref_id）→ 物化（切片并行）
  Step 5 sg_consolidate  脚本提取全部事件安全目标 + KB 参考 → LLM 合并 → 整车安全目标
  Step 6 merge           合并 01~05 JSON 为最终结构化输出（引擎做 schema 校验 + 渲染 xlsx）

设计原则：
  - 级联检索：每步检索参数来自上一步 LLM 判断/物化输出（KB 功能名），
    不是最初的识别名；
  - 三态物化：reused=脚本按 kb_id 从 KB 原块逐字复制（LLM 只给 id，不输出内容，
    reused 事件的 S/E/C/安全目标全部脚本填）；adapted=KB 原块 + LLM overrides 覆盖；
    new=LLM 输出；
  - 分步存盘：每步产物存临时 JSON（01~05），可调试、可断点续跑；
  - 解析/检索/物化辅助函数复用同目录 hara_prepare.py（importlib 按路径加载）。
"""
from __future__ import annotations

import importlib.util
import json
import logging
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger("skills.hara_pipeline")

_DOMAIN = "functional_safety"

# ── 11 个标准失效词（与渲染器 generate_hara.py WORDS / malfunctions.md 一致） ──
_WORDS = ["丢失", "非预期", "间歇", "过多", "过少", "过早", "反向", "振荡", "部分", "过晚", "卡滞"]

# 差异化召回（方案第七节）：功能清单 10、失效模式 20/功能、事件 50/功能、安全目标随适配器
_TOPK_FUNCTION_LIST = 10
_TOPK_FAILURE_MODE = 20
_TOPK_EVENT_PER_SLICE = 50
_TOPK_REF_QUERY = 8

# 事件候选注入 LLM 的封顶数（KB 原块物化不受此限，仅限制上下文长度）
_CAND_PROMPT_CAP = 45


# ── 同目录 hara_prepare.py 工具模块（解析/检索/物化辅助） ────────────

_PREPARE = None


def _prep():
    global _PREPARE
    if _PREPARE is None:
        path = Path(__file__).resolve().parent / "hara_prepare.py"
        spec = importlib.util.spec_from_file_location("hara_pipeline_hara_prepare", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _PREPARE = module
    return _PREPARE


def _svc():
    return _prep()._load_retrieval_service()


# ── 通用小工具 ──────────────────────────────────────────────────────

def _pipeline_cfg(ctx) -> dict:
    return ((ctx.skill_cfg.get("execution") or {}).get("pipeline")) or {}


def _save_stage_json(ctx, stage_name: str, data) -> str:
    """分步存盘：阶段产物写入系统临时目录 hara_pipeline/<task_id>/，可调试、可断点续跑。"""
    task_id = str(getattr(ctx, "task_id", "") or "default")
    stage_dir = Path(tempfile.gettempdir()) / "hara_pipeline" / task_id
    stage_dir.mkdir(parents=True, exist_ok=True)
    path = stage_dir / f"{stage_name}.json"
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("[hara_pipeline] 阶段产物已保存: %s", path)
    return str(path)


def _usage_add(total: dict, usage: dict | None) -> None:
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        total[k] = int(total.get(k, 0) or 0) + int((usage or {}).get(k, 0) or 0)


def _norm_features(raw) -> list[dict]:
    """Feature 列表规范化：LLM 输出/KB 解析统一为 {feature_list_id, description, do_hara}。"""
    feats: list[dict] = []
    for f in (raw if isinstance(raw, list) else []):
        if isinstance(f, dict):
            desc = str(f.get("description") or "").strip()
            if not desc:
                continue
            dh = str(f.get("do_hara") or "是").strip()
            feats.append({
                "feature_list_id": str(f.get("feature_list_id") or "").strip(),
                "description": desc,
                "do_hara": "否" if dh in ("否", "no", "N", "n", "false") else "是",
            })
        elif isinstance(f, str) and f.strip():
            feats.append({"feature_list_id": "", "description": f.strip(), "do_hara": "是"})
    return feats


# ════════════════════════════════════════════════════════════════════
# Step 1: identify —— 识别相关项与整车功能清单（复用 hara_prepare 识别层）
# ════════════════════════════════════════════════════════════════════

def _run_identify(ctx) -> tuple[dict, dict]:
    cfg = _pipeline_cfg(ctx).get("identify") or {}
    prep = _prep()
    # DOCX 功能清单确定性抽取（合并单元格错列修正），供识别层做权威分组
    doc_supplement = prep._build_doc_supplement(ctx)
    info, response = prep._run_identify_stage(ctx, cfg, doc_supplement)
    identify = {
        "item": {
            "name": info.get("item_name") or "",
            "abbr": info.get("abbr") or "",
            "domain_prefix": info.get("domain_prefix") or "",
            "doc_ref": "",
        },
        "identified_functions": info.get("functions") or [],
    }
    _save_stage_json(ctx, "01_identify", identify)
    return identify, ctx.extract_usage(response)


# ════════════════════════════════════════════════════════════════════
# Step 2: function_match —— KB 功能候选 → LLM 判断 → 物化
# ════════════════════════════════════════════════════════════════════

def _run_function_match(ctx, identify: dict) -> tuple[dict, dict]:
    cfg = _pipeline_cfg(ctx).get("function_match") or {}
    identified = [str(x or "").strip() for x in identify.get("identified_functions") or [] if str(x or "").strip()]
    svc = _svc()
    prep = _prep()

    cand_funcs = prep._retrieve_function_candidates(
        svc, {"functions": identified}, ctx
    )
    kb_index = {c["ref_id"]: c for c in cand_funcs if c.get("ref_id")}

    decisions: list = []
    usage: dict = {}
    if cand_funcs:
        if ctx.emit_progress:
            ctx.emit_progress("正在对照历史功能清单判断沿用/新增…")
        system_prompt = ctx.build_stage_prompt(cfg)
        user_message = (
            ctx.rendered
            + "\n\n【识别出的整车功能清单】\n"
            + json.dumps(identified, ensure_ascii=False, indent=1)
            + "\n\n【历史项目功能清单候选（KB，ref_id 为唯一标识）】\n"
            + json.dumps(cand_funcs, ensure_ascii=False, indent=1)
            + "\n\n请按系统提示词对识别清单中每个功能逐条判断，只输出 JSON 对象。"
        )
        max_tokens = cfg.get("max_tokens")
        parsed, response = ctx.invoke_json_stage(
            ctx.bind_json_model(max_tokens),
            system_prompt,
            user_message,
            stage_name="功能匹配(function_match)",
            max_tokens=max_tokens,
            runnable_config=ctx.runnable_config,
        )
        usage = ctx.extract_usage(response)
        raw = parsed.get("functions")
        decisions = raw if isinstance(raw, list) else []
    else:
        logger.info("[hara_pipeline] 功能清单候选零命中，全部按新增处理")

    functions_out, matched_names = _materialize_functions(decisions, identified, kb_index)
    step2 = {
        "functions": functions_out,
        "matched_kb_function_names": matched_names,
        "candidates": len(cand_funcs),
    }
    _save_stage_json(ctx, "02_functions", step2)
    return step2, usage


def _materialize_functions(decisions: list, identified: list[str],
                           kb_index: dict) -> tuple[list, list[str]]:
    """功能三态物化：reused/adapted 取 KB 原块（features 全取 KB），new 用 LLM 输出。

    reused 物化后的 vehicle_function 即 KB 功能名，后续检索级联使用；
    LLM 漏判的识别功能兜底为 new（保持覆盖完整）。
    """
    functions_out: list[dict] = []
    matched: list[str] = []
    claimed: set[str] = set()
    seq = 0
    for d in decisions:
        if not isinstance(d, dict):
            continue
        source = str(d.get("source") or "new").strip().lower()
        kb = kb_index.get(str(d.get("kb_id") or "").strip()) \
            if source in ("reused", "adapted") else None
        if source in ("reused", "adapted") and kb is None:
            logger.warning(
                "[hara_pipeline] 功能物化：kb_id=%s 不在候选中，降级为新增",
                d.get("kb_id"),
            )
            source = "new"
        seq += 1
        fid = f"F{seq:03d}"
        identified_name = str(d.get("identified") or "").strip()
        if source == "reused":
            functions_out.append({
                "fid": fid,
                "vehicle_function": str(kb["func"] or "").strip(),
                "features": _norm_features(kb.get("features")),
                "source": {"type": "reused", "ref_id": kb["ref_id"], "project": ""},
            })
            matched.append(str(kb["func"] or "").strip())
            claimed.add(identified_name)
        elif source == "adapted":
            fn = {
                "fid": fid,
                "vehicle_function": str(kb["func"] or "").strip(),
                "features": _norm_features(kb.get("features")),
                "source": {"type": "adapted", "ref_id": kb["ref_id"], "project": ""},
            }
            for k, v in (d.get("overrides") or {}).items():
                if k == "features":
                    fn["features"] = _norm_features(v)
                elif k == "vehicle_function" and str(v or "").strip():
                    fn[k] = str(v).strip()
            functions_out.append(fn)
            matched.append(fn["vehicle_function"])
            claimed.add(identified_name)
        else:
            functions_out.append({
                "fid": fid,
                "vehicle_function": str(
                    d.get("vehicle_function") or identified_name
                ).strip(),
                "features": _norm_features(d.get("features")),
                "source": {"type": "new"},
            })
            claimed.add(identified_name)
    # 兜底：LLM 漏判的识别功能补为 new
    for name in identified:
        if name and name not in claimed:
            seq += 1
            logger.warning("[hara_pipeline] 功能匹配漏判 %r，兜底为新增", name)
            functions_out.append({
                "fid": f"F{seq:03d}",
                "vehicle_function": name,
                "features": [],
                "source": {"type": "new"},
            })
    logger.info(
        "[hara_pipeline] 功能匹配完成：%d 个功能（KB 级联名 %d 个）：%s",
        len(functions_out), len(matched),
        [(f["fid"], f["vehicle_function"]) for f in functions_out],
    )
    return functions_out, matched


# ════════════════════════════════════════════════════════════════════
# Step 3: fm_match —— KB 失效模式候选 × 11 失效词 → LLM 判断 → 物化
# ════════════════════════════════════════════════════════════════════

def _fm_records(svc, func_name: str, ctx) -> list[dict]:
    """检索某功能的失效模式候选（完整原文 mb/vh），返回 [{ref_id, func, word, mb, vh}]。"""
    prep = _prep()
    try:
        records, _ = svc.retrieve(
            domain=_DOMAIN, query_type="failure_mode",
            params={"functions": [func_name]}, ctx=ctx,
        )
    except Exception as exc:  # noqa: BLE001 —— 检索失败降级为空
        logger.warning(
            "[hara_pipeline] 失效模式候选检索降级（func=%s）：%s", func_name, exc,
        )
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for r in records:
        word = str(r.get("失效词") or "").strip()
        if not word:
            continue
        ref_id = str(r.get("失效模式编号") or "").strip()
        content = str(r.get("内容") or "")
        mb, vh = prep._fm_original_texts(content)
        if not (mb or vh):
            continue
        key = ref_id or f"{word}:{content[:80]}"
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "ref_id": ref_id,
            "func": str(r.get("功能") or ""),
            "word": word,
            "mb": mb,
            "vh": vh,
        })
    return out


def _run_fm_match(ctx, functions: list[dict]) -> tuple[dict, dict]:
    cfg = _pipeline_cfg(ctx).get("failure_mode_match") or {}

    fm_cands: dict[str, list] = {}
    for fn in functions:
        fm_cands[fn["fid"]] = _fm_records(_svc(), fn["vehicle_function"], ctx)
    total_cands = sum(len(v) for v in fm_cands.values())
    logger.info(
        "[hara_pipeline] 失效模式候选：%d 条（%d 个功能）", total_cands, len(functions),
    )

    decisions: list = []
    usage: dict = {}
    if total_cands:
        if ctx.emit_progress:
            ctx.emit_progress("正在对照历史失效模式判断沿用/新增…")
        payload = [
            {
                "fid": fn["fid"],
                "vehicle_function": fn["vehicle_function"],
                "candidates": fm_cands[fn["fid"]],
            }
            for fn in functions
        ]
        system_prompt = ctx.build_stage_prompt(cfg)
        user_message = (
            ctx.rendered
            + "\n\n【整车功能清单】\n"
            + json.dumps(
                [{"fid": fn["fid"], "vehicle_function": fn["vehicle_function"]}
                 for fn in functions],
                ensure_ascii=False, indent=1,
            )
            + "\n\n【历史项目失效模式候选（按功能分组，ref_id 为唯一标识）】\n"
            + json.dumps(payload, ensure_ascii=False, indent=1)
            + "\n\n请按系统提示词对每个功能的 11 个失效词逐条判断，只输出 JSON 对象。"
        )
        max_tokens = cfg.get("max_tokens")
        parsed, response = ctx.invoke_json_stage(
            ctx.bind_json_model(max_tokens),
            system_prompt,
            user_message,
            stage_name="失效模式匹配(fm_match)",
            max_tokens=max_tokens,
            runnable_config=ctx.runnable_config,
        )
        usage = ctx.extract_usage(response)
        raw = parsed.get("items")
        decisions = raw if isinstance(raw, list) else []
    else:
        logger.info("[hara_pipeline] 失效模式候选零命中，全部失效单元按新增处理")

    kb_fm_index = {
        fid: {c["ref_id"]: c for c in cands if c.get("ref_id")}
        for fid, cands in fm_cands.items()
    }
    matrix, hazop_units = _materialize_failure_modes(decisions, functions, kb_fm_index)
    step3 = {"malfunction_matrix": matrix, "hazop_units": hazop_units}
    _save_stage_json(ctx, "03_failure_modes", step3)
    return step3, usage


def _materialize_failure_modes(decisions: list, functions: list[dict],
                               kb_fm_index: dict) -> tuple[list, list]:
    """失效模式三态物化：产出 malfunction_matrix 与 hazop_items 骨架（不含 events）。

    reused/adapted：mb/vh 全取 KB 完整原文（adapted 允许 overrides 覆盖）；
    new：用 LLM 撰写的失效行为/整车危害。
    """
    by_fid: dict[str, list] = {}
    for d in decisions:
        if isinstance(d, dict):
            by_fid.setdefault(str(d.get("fid") or "").strip(), []).append(d)

    matrix: list[dict] = []
    hazop_items: list[dict] = []
    item_seq = 0
    for fn in functions:
        fid = fn["fid"]
        kb_index = kb_fm_index.get(fid) or {}
        selections: dict = {}
        for d in by_fid.get(fid, []):
            word = str(d.get("word") or "").strip()
            if word not in _WORDS:
                logger.warning(
                    "[hara_pipeline] 失效词 %r 不在 11 个标准失效词内，跳过", word,
                )
                continue
            source = str(d.get("source") or "new").strip().lower()
            kb = kb_index.get(str(d.get("kb_id") or "").strip()) \
                if source in ("reused", "adapted") else None
            if source in ("reused", "adapted") and kb is None:
                logger.warning(
                    "[hara_pipeline] 失效模式物化：kb_id=%s 不在候选中，降级为新增",
                    d.get("kb_id"),
                )
                source = "new"
            if source == "reused":
                mb, vh = kb["mb"], kb["vh"]
                src_obj: dict = {"type": "reused", "ref_id": kb["ref_id"], "project": ""}
            elif source == "adapted":
                mb, vh = kb["mb"], kb["vh"]
                for k, v in (d.get("overrides") or {}).items():
                    if k == "malfunction_behavior" and str(v or "").strip():
                        mb = str(v).strip()
                    elif k == "vehicle_hazard" and str(v or "").strip():
                        vh = str(v).strip()
                src_obj = {"type": "adapted", "ref_id": kb["ref_id"], "project": ""}
            else:
                mb = str(d.get("malfunction_behavior") or "").strip()
                vh = str(d.get("vehicle_hazard") or "").strip()
                if not mb:
                    logger.warning(
                        "[hara_pipeline] 失效单元（%s/%s）新增条目缺失效行为，跳过",
                        fid, word,
                    )
                    continue
                src_obj = {"type": "new"}
            selections[word] = {"malfunction_behavior": mb, "vehicle_hazard": vh}
            item_seq += 1
            hazop_items.append({
                "item_id": f"H{item_seq:03d}",
                "fid": fid,
                "word": word,
                "malfunction_behavior": mb,
                "vehicle_hazard": vh,
            })
        matrix.append({"fid": fid, "selections": selections})
    logger.info(
        "[hara_pipeline] 失效模式匹配完成：%d 个安全关键失效单元",
        len(hazop_items),
    )
    return matrix, hazop_items


# ════════════════════════════════════════════════════════════════════
# Step 4: event_match —— KB 事件候选 → LLM 判断 → 物化（按切片并行）
# ════════════════════════════════════════════════════════════════════

def _fetch_event_content(ctx, ref_id: str) -> str | None:
    """ref 不在本切片候选时按 ref 直查 KB 单块（兜底），返回 content 或 None。"""
    if not ref_id:
        return None
    try:
        chunks = ctx.retrieve_knowledge(
            _DOMAIN, ref_id, top_k=_TOPK_REF_QUERY, layer="hara_event"
        ) or []
    except Exception as exc:  # noqa: BLE001 —— 直查失败视为 ref 失效
        logger.warning("[hara_pipeline] 事件 ref 直查降级（ref=%s）：%s", ref_id, exc)
        return None
    for c in chunks:
        if not isinstance(c, dict):
            continue
        meta = c.get("meta") or {}
        content = str(c.get("content") or "")
        if str(meta.get("hzrd_id") or "").strip() == ref_id or ref_id in content[:120]:
            return content
    return None


def _slice_candidates(prep, kb_idx: dict, word: str) -> list[dict]:
    """从功能事件 index 过滤出本失效词的候选（结构化摘要，供 LLM 判断）。"""
    candidates: list[dict] = []
    for ref_id, r in kb_idx.items():
        content = str(r.get("内容") or "")
        m = prep._EVENT_WORD_RE.search(content)
        c_word = m.group(1).strip() if m else ""
        # 失效词匹配（适配器按功能名检索，需二次过滤失效词）；无失效词标注的旧数据保留
        if c_word and c_word != word:
            continue
        ev = prep._parse_event_assessment(content)
        candidates.append({
            "ref_id": ref_id,
            "scene": ev["scene"],
            "S": ev["S"], "E": ev["E"], "C": ev["C"],
            "asil": ev["asil"],
            "sg_text": ev["sg_text"],
        })
        if len(candidates) >= _CAND_PROMPT_CAP:
            break
    return candidates


def _materialize_events(prep, ctx, unit: dict, events: list,
                        kb_idx: dict) -> tuple[list, list[dict]]:
    """事件三态物化（单切片）。返回 (events_final, gaps 待回炉事件)。

    reused：从 KB 原块逐字复制 scenario/desc/S/E/C/理由/安全目标组；
    adapted：KB 场景+安全目标组覆盖，保留 LLM 的 S/E/C/理由；
    new：保留 LLM 输出（S/E/C 必须齐全）。
    ref 失效（候选与直查均无）或 KB 块解析不出评级 → sec_gap 回炉。
    """
    final: list = []
    gaps: list[dict] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        src = ev.get("source") if isinstance(ev.get("source"), dict) else {}
        stype = str(src.get("type") or "new").strip().lower()
        ref = str(src.get("ref_id") or "").strip()
        if stype in ("reused", "adapted") and ref:
            record = kb_idx.get(ref)
            content = str(record.get("内容") or "") if record else ""
            if not content:
                fetched = _fetch_event_content(ctx, ref)
                if fetched:
                    record = {"来源": ""}
                    content = fetched
            if content:
                ok = prep._apply_materialize(
                    ev, {"content": content, "meta": {}},
                    "full" if stype == "reused" else "adapted",
                )
                if ok and stype == "reused":
                    if record and not str(src.get("project") or "").strip():
                        src["project"] = str(record.get("来源") or "").strip()
                if not ok:
                    # KB 块解析不出评级（meta 与正文均无 S）：回炉评级；
                    # 事件内容将来自 LLM，来源改标 new（避免沿用备注误导）
                    logger.warning(
                        "[hara_pipeline] 事件 ref=%s 知识库块解析不出评级，回炉评级", ref,
                    )
                    ev["source"] = {"type": "new"}
                    gaps.append(ev)
                    continue
            else:
                logger.warning(
                    "[hara_pipeline] 事件 ref=%s 未命中知识库（候选与直查均无），回炉评级",
                    ref,
                )
                ev["source"] = {"type": "new"}
                gaps.append(ev)
                continue
        if stype != "reused" and prep._sec_norm(ev.get("S")) is None:
            # adapted/new 事件缺 S：评级不完整，回炉
            gaps.append(ev)
            continue
        final.append(ev)
    return final, gaps


_REWORK_FIELDS = ("S", "s_reason", "E", "e_reason", "C", "c_basis", "c_reason",
                  "sg_text", "safe_state", "ftti")


def _rework_gaps(prep, ctx, unit: dict, gaps: list[dict], system_prompt: str,
                 max_tokens) -> tuple[list[dict], dict]:
    """sec_gap 回炉：物化失败/评级缺失的事件回炉 LLM 评级一次（白名单回填评级字段）。"""
    if not gaps:
        return [], {}
    payload_events = [
        {
            "index": i,
            "scenario_text": str(ev.get("scenario_text") or ""),
        }
        for i, ev in enumerate(gaps)
    ]
    payload = {
        "unit": {
            "word": unit.get("word") or "",
            "malfunction_behavior": unit.get("malfunction_behavior") or "",
            "vehicle_hazard": unit.get("vehicle_hazard") or "",
        },
        "events": payload_events,
    }
    user_message = (
        "以下危害事件的评级缺失（来源引用失效或评级不完整）。请按系统提示词的"
        " S/E/C 评级规则与安全目标挂接规则，对每条事件完整评级：index 对应关系"
        "保持不变，不得新增或遗漏；只输出 JSON 对象 {\"events\": [...]}\n"
        + json.dumps(payload, ensure_ascii=False, indent=1)
    )
    try:
        parsed, response = ctx.invoke_json_stage(
            ctx.bind_json_model(max_tokens),
            system_prompt,
            user_message,
            stage_name=f"事件评级回炉({unit.get('fid')}/{unit.get('word')})",
            max_tokens=max_tokens,
        )
    except Exception as exc:  # noqa: BLE001 —— 回炉失败保留事件原文（渲染器标 CHECK/黄）
        logger.warning(
            "[hara_pipeline] 评级回炉失败（%s/%s），保留事件原文：%s",
            unit.get("fid"), unit.get("word"), exc,
        )
        return [], {}
    usage = ctx.extract_usage(response)
    fixed = parsed.get("events")
    if not isinstance(fixed, list):
        return [], usage
    by_index: dict[int, dict] = {}
    for fe in fixed:
        if isinstance(fe, dict) and isinstance(fe.get("index"), int):
            by_index[fe["index"]] = fe
    filled = 0
    for i, ev in enumerate(gaps):
        fe = by_index.get(i)
        if not isinstance(fe, dict):
            continue
        for k in _REWORK_FIELDS:
            v = fe.get(k)
            if isinstance(v, (int, str)) and str(v).strip() != "":
                ev[k] = v
        if prep._sec_norm(ev.get("S")) is not None:
            filled += 1
    logger.info(
        "[hara_pipeline] 评级回炉（%s/%s）：%d 条待补，%d 条补齐",
        unit.get("fid"), unit.get("word"), len(gaps), filled,
    )
    return gaps, usage


def _run_event_match(ctx, functions: list[dict], hazop_units: list[dict],
                     identify: dict) -> tuple[list, dict]:
    cfg = _pipeline_cfg(ctx).get("event_match") or {}
    prep = _prep()
    svc = _svc()
    system_prompt = ctx.build_stage_prompt(cfg)
    max_tokens = cfg.get("max_tokens")
    max_workers = max(1, int(cfg.get("max_workers") or 3))
    user_instruction = str(cfg.get("user_instruction") or "").strip() or (
        "以下是一个「功能失效单元」及其历史 HARA 事件候选清单。"
        "请按系统提示词输出该失效单元的完整危害事件集合（候选沿用/改编/新增），只输出 JSON 对象。"
    )
    item = identify.get("item") or {}
    item_header = (
        f"相关项：{item.get('name') or ''}（{item.get('abbr') or ''} / "
        f"{item.get('domain_prefix') or ''}）"
    )

    # 每功能事件候选 index（ref_id → record）：级联用 Step 2 物化后的 KB 功能名
    event_index: dict[str, dict] = {}
    for fn in functions:
        event_index[fn["fid"]] = prep._build_event_index(
            svc, _DOMAIN, fn["vehicle_function"], ctx
        )
    fn_name_by_fid = {fn["fid"]: fn["vehicle_function"] for fn in functions}

    logger.info(
        "[hara_pipeline] 事件评级开始：%d 个切片，并发 %d，max_tokens=%s",
        len(hazop_units), max_workers, max_tokens,
    )
    if ctx.emit_progress:
        ctx.emit_progress(
            f"正在逐项评估危害事件（共 {len(hazop_units)} 项，可并行）…"
        )

    def _one(index: int, unit: dict) -> tuple[int, list, dict]:
        fid = str(unit.get("fid") or "")
        word = str(unit.get("word") or "")
        func_name = fn_name_by_fid.get(fid, "")
        kb_idx = event_index.get(fid) or {}
        candidates = _slice_candidates(prep, kb_idx, word)

        payload = {
            "unit": {
                "fid": fid,
                "word": word,
                "vehicle_function": func_name,
                "malfunction_behavior": unit.get("malfunction_behavior") or "",
                "vehicle_hazard": unit.get("vehicle_hazard") or "",
            },
            "candidates": candidates,
        }
        user_message = (
            f"{item_header}\n\n{user_instruction}\n\n"
            f"```json\n{json.dumps(payload, ensure_ascii=False, indent=1)}\n```\n"
            + ctx.schema_example_block
        )

        def _attempt() -> tuple[list, dict]:
            parsed, response = ctx.invoke_json_stage(
                ctx.bind_json_model(max_tokens),
                system_prompt,
                user_message,
                stage_name=f"事件评级(切片{index + 1})[{func_name}/{word}]",
                max_tokens=max_tokens,
                # 线程池内不传 runnable_config：避免日志回调跨线程并发写同一文件
            )
            events = parsed.get("events")
            if not isinstance(events, list):
                raise ValueError("事件评级输出缺少列表字段 events")
            events_final, gaps = _materialize_events(prep, ctx, unit, events, kb_idx)
            reworked, rw_usage = _rework_gaps(
                prep, ctx, unit, gaps, system_prompt, max_tokens
            )
            for ev in reworked:
                if prep._sec_norm(ev.get("S")) is None:
                    # 回炉后仍缺评级：丢弃，避免 null S 进入 schema 校验
                    logger.warning(
                        "[hara_pipeline] 事件（%s/%s）回炉后仍缺评级，丢弃：%s",
                        unit.get("fid"), unit.get("word"),
                        str(ev.get("scenario_text") or "")[:40],
                    )
                    continue
                events_final.append(ev)
            usage = ctx.extract_usage(response)
            _usage_add(usage, rw_usage)
            logger.info(
                "[hara_pipeline] 事件评级切片 %d（%s/%s）：候选 %d 条，"
                "输出事件 %d 条（回炉 %d 条）",
                index + 1, fid, word, len(candidates),
                len(events_final), len(gaps),
            )
            return events_final, usage

        try:
            events, usage = _attempt()
            return index, events, usage
        except Exception as first_exc:  # noqa: BLE001 —— 单切片统一重试 1 次
            logger.warning(
                "[hara_pipeline] 事件评级切片 %d 首次失败，重试一次：%s",
                index + 1, first_exc,
            )
            try:
                events, usage = _attempt()
                return index, events, usage
            except Exception as second_exc:
                raise ValueError(
                    f"事件评级切片 {index + 1}（{fid}/{word}）重试后仍失败："
                    f"{second_exc}。请缩小一次分析的功能/场景范围后重试。"
                ) from second_exc

    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    # 跨切片去重：同一历史事件（非空 ref_id）只允许被沿用一次
    consumed_refs: set[str] = set()
    hazop_items = [dict(u) for u in hazop_units]
    errors: list[BaseException] = []
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_one, i, u): i for i, u in enumerate(hazop_items)}
        for future in as_completed(futures):
            done += 1
            if ctx.emit_progress:
                ctx.emit_progress(
                    f"正在逐项评估危害事件（已完成 {done}/{len(hazop_items)} 项）…"
                )
            try:
                idx, events, usage = future.result()
            except BaseException as exc:  # noqa: BLE001 —— 收集后统一抛出
                errors.append(exc)
                continue
            deduped: list = []
            dropped = 0
            for ev in events:
                src = ev.get("source") if isinstance(ev, dict) else None
                ref_id = str((src or {}).get("ref_id") or "").strip() \
                    if isinstance(src, dict) else ""
                if ref_id:
                    if ref_id in consumed_refs:
                        dropped += 1
                        continue
                    consumed_refs.add(ref_id)
                deduped.append(ev)
            if dropped:
                logger.warning(
                    "[hara_pipeline] 切片 %d 有 %d 条事件因历史 ref_id 已被其他切片"
                    "沿用而丢弃", idx + 1, dropped,
                )
            hazop_items[idx]["events"] = deduped
            _usage_add(usage_total, usage)
    if errors:
        raise errors[0]

    _save_stage_json(ctx, "04_events", hazop_items)
    return hazop_items, usage_total


# ════════════════════════════════════════════════════════════════════
# Step 5: sg_consolidate —— 事件安全目标提取 + KB 参考 + LLM 合并
# ════════════════════════════════════════════════════════════════════

def _extract_events_sg(prep, hazop_items: list) -> list[dict]:
    """脚本从全部事件中提取安全目标三件套（整理合并前的原始列表）。"""
    events_sg: list[dict] = []
    for unit in hazop_items:
        for j, ev in enumerate(unit.get("events") or [], 1):
            sg = str((ev or {}).get("sg_text") or "").strip()
            if not sg:
                continue
            events_sg.append({
                "event_id": f"{unit.get('fid')}/{unit.get('word')}#{j:02d}",
                "sg_text": sg,
                "safe_state": str(ev.get("safe_state") or "").strip(),
                "ftti": str(ev.get("ftti") or "").strip(),
                "asil": prep._event_asil(ev) or "",
            })
    return events_sg


def _run_sg_consolidate(ctx, functions: list[dict],
                        hazop_items: list) -> tuple[dict, dict]:
    cfg = _pipeline_cfg(ctx).get("sg_consolidate") or {}
    prep = _prep()
    svc = _svc()

    # ① 脚本提取全部事件安全目标（整理合并前）
    events_sg = _extract_events_sg(prep, hazop_items)
    # ② KB safety_goal 候选（合并参考；KB 中整车安全目标条目不多）
    kb_sg = prep._retrieve_safety_goal_candidates(svc, ctx)
    logger.info(
        "[hara_pipeline] 安全目标整理：事件携带 %d 条（KB 参考 %d 条）",
        len(events_sg), len(kb_sg),
    )

    merged_sg: list = []
    usage: dict = {}
    if events_sg:
        if ctx.emit_progress:
            ctx.emit_progress("正在整理合并整车安全目标…")
        system_prompt = ctx.build_stage_prompt(cfg)
        user_message = (
            ctx.rendered
            + "\n\n【全部事件携带的安全目标（整理合并前）】\n"
            + json.dumps(events_sg, ensure_ascii=False, indent=1)
            + "\n\n【历史项目整车安全目标候选（KB，合并参考）】\n"
            + json.dumps(kb_sg, ensure_ascii=False, indent=1)
            + "\n\n请按系统提示词完成合并去重，只输出 JSON 对象。"
        )
        max_tokens = cfg.get("max_tokens")
        parsed, response = ctx.invoke_json_stage(
            ctx.bind_json_model(max_tokens),
            system_prompt,
            user_message,
            stage_name="安全目标整理合并(sg_consolidate)",
            max_tokens=max_tokens,
            runnable_config=ctx.runnable_config,
        )
        usage = ctx.extract_usage(response)
        raw = parsed.get("merged_sg")
        if isinstance(raw, list):
            merged_sg = _norm_merged_sg(raw)
    else:
        logger.info("[hara_pipeline] 无事件安全目标，跳过合并")

    step5 = {"events_sg": events_sg, "merged_sg": merged_sg}
    _save_stage_json(ctx, "05_safety_goals", step5)
    return step5, usage


def _norm_merged_sg(raw: list) -> list[dict]:
    """merged_sg 条目规范化：空白丢弃、source 白名单、merged_from 保留非空。"""
    out: list[dict] = []
    for g in raw:
        if not isinstance(g, dict):
            continue
        sg = str(g.get("sg_text") or "").strip()
        if not sg:
            continue
        src = str(g.get("source") or "merged").strip().lower()
        out.append({
            "sg_text": sg,
            "safe_state": str(g.get("safe_state") or "").strip(),
            "ftti": str(g.get("ftti") or "").strip(),
            "asil": str(g.get("asil") or "").strip().upper(),
            "source": src if src in ("kb", "merged") else "merged",
            "kb_id": str(g.get("kb_id") or "").strip(),
            "merged_from": [
                str(x).strip() for x in (g.get("merged_from") or [])
                if str(x).strip()
            ],
        })
    return out


# ════════════════════════════════════════════════════════════════════
# 管道入口：串联 6 步
# ════════════════════════════════════════════════════════════════════

def run(ctx):
    """pipeline 技能入口：串联 6 步线性管道，返回 (structured, usage)。"""
    logger.info("[hara_pipeline] 管道开始（task=%s）", ctx.task_id)

    identify, u1 = _run_identify(ctx)
    step2, u2 = _run_function_match(ctx, identify)
    step3, u3 = _run_fm_match(ctx, step2["functions"])
    hazop_items, u4 = _run_event_match(
        ctx, step2["functions"], step3["hazop_units"], identify
    )
    safety_goals, u5 = _run_sg_consolidate(ctx, step2["functions"], hazop_items)

    # Step 6: merge —— 合并 01~05 为最终结构化输出（引擎做 schema 校验 + 渲染）
    final = {
        "item": identify["item"],
        "functions": step2["functions"],
        "malfunction_matrix": step3["malfunction_matrix"],
        "hazop_items": hazop_items,
        "safety_goals": safety_goals,
    }
    _save_stage_json(ctx, "06_final", final)

    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for u in (u1, u2, u3, u4, u5):
        _usage_add(usage, u)
    usage.update({
        "stage_identify_calls": 1,
        "stage_function_match_calls": 1,
        "stage_fm_match_calls": 1,
        "stage_event_match_calls": len(hazop_items),
        "stage_sg_consolidate_calls": 1,
    })
    logger.info(
        "[hara_pipeline] 管道完成：%d 功能，%d 失效单元，usage=%s",
        len(step2["functions"]), len(hazop_items),
        {k: v for k, v in usage.items() if k.startswith(("prompt", "completion", "total"))},
    )
    return final, usage
