"""功能安全域知识库检索适配器：按功能名检索历史失效模式。

两级检索策略：
  ① failure_mode 层精确检索（meta.func 双向包含匹配过滤）；
  ② 0 命中时 fallback 到 hara_event 层，从内容解析失效模式
     （hara_event 内容含「失效模式：ID（描述）」「整车危害：…」字段）。
不依赖 hara_prepare.py，自有检索与解析逻辑。
"""
import re
import logging

logger = logging.getLogger("skills.knowledge_lookup.functional_safety")

# 11 个标准失效词
_WORDS = ["丢失", "非预期", "间歇", "过多", "过少", "过早", "反向", "振荡", "部分", "过晚", "卡滞"]

# hara_event 内容中失效模式行：「失效模式：P_MF_0004_03（滑行能量回收功能丢失）」
_EV_FM_RE = re.compile(r"失效模式[:：]\s*(\S+)\s*[（(](.+?)[）)]")
# hara_event 内容中整车危害行：「整车危害：车辆纵向减速非预期减少」
_EV_VH_RE = re.compile(
    r"整车危害[:：]\s*(.+?)(?=\n运行场景[:：]|\n危害事件描述[:：]|\n\n|\n风险评估[:：]|$)",
    re.S,
)


def query_failure_modes(ctx) -> dict:
    """按功能名检索历史失效模式。"""
    functions = (ctx.inputs or {}).get("functions") or []
    if isinstance(functions, str):
        functions = [functions]
    domain = getattr(ctx, "kb_domain", "") or ""
    retrieve = ctx.retrieve_knowledge

    records = []
    for func_name in functions:
        func_name = str(func_name or "").strip()
        if not func_name:
            continue
        # ① failure_mode 层精确检索
        fms = _retrieve_from_failure_mode(retrieve, domain, func_name)
        if not fms:
            # ② fallback: hara_event 层
            logger.info(
                "[functional_safety] failure_mode 层 0 命中（func=%s），fallback 到 hara_event 层",
                func_name,
            )
            fms = _extract_from_hara_events(retrieve, domain, func_name)
        for fm in fms:
            records.append({
                "功能": func_name,
                "失效词": fm.get("word", ""),
                "失效行为": fm.get("malfunction_behavior", ""),
                "整车危害": fm.get("vehicle_hazard", ""),
            })
    logger.info(
        "[functional_safety] 检索完成：%d 个功能命中 %d 条失效模式",
        len([f for f in functions if f]), len(records),
    )
    return {"records": records, "usage": {}}


def _retrieve_from_failure_mode(retrieve, domain: str, func_name: str) -> list[dict]:
    """failure_mode 层精确检索（meta.func 双向包含匹配过滤）。"""
    if not func_name:
        return []
    try:
        chunks = retrieve(domain, f"{func_name} 失效模式", top_k=20, layer="failure_mode") or []
    except Exception as exc:
        logger.warning("[functional_safety] failure_mode 检索降级（func=%s）：%s", func_name, exc)
        return []
    result = []
    for c in chunks:
        if not isinstance(c, dict):
            continue
        meta = c.get("meta") or {}
        meta_func = str(meta.get("func") or "").strip()
        if meta_func and meta_func not in func_name and func_name not in meta_func:
            continue
        word = str(meta.get("failure_type") or "").strip()
        if not word:
            continue
        # 从 content 提取失效行为和整车危害
        content = str(c.get("content") or "")
        mb, vh = _parse_failure_mode_content(content)
        result.append({"word": word, "malfunction_behavior": mb, "vehicle_hazard": vh})
    return result


def _parse_failure_mode_content(content: str) -> tuple[str, str]:
    """从 failure_mode 分块正文提取失效行为与整车危害。"""
    text = str(content or "")
    mb = re.search(r"功能异常表现[:：](.*?)(?=\n整车危害[:：]|\n\n|$)", text, re.S)
    vh = re.search(r"整车危害[:：](.*?)(?=\n\n|$)", text, re.S)
    return (
        mb.group(1).strip() if mb else "",
        vh.group(1).strip() if vh else "",
    )


def _extract_from_hara_events(retrieve, domain: str, func_name: str, top_k: int = 30) -> list[dict]:
    """hara_event 层 fallback：从事件内容解析失效模式。"""
    if not func_name:
        return []
    try:
        chunks = retrieve(domain, func_name, top_k=top_k, layer="hara_event") or []
    except Exception as exc:
        logger.warning("[functional_safety] hara_event fallback 检索降级（func=%s）：%s", func_name, exc)
        return []
    result = []
    seen_descs = set()
    for c in chunks:
        if not isinstance(c, dict):
            continue
        meta = c.get("meta") or {}
        meta_func = str(meta.get("func") or "").strip()
        if meta_func and meta_func not in func_name and func_name not in meta_func:
            continue
        content = str(c.get("content") or "")
        fm_match = _EV_FM_RE.search(content)
        if not fm_match:
            continue
        fm_desc = fm_match.group(2).strip()
        if not fm_desc or fm_desc in seen_descs:
            continue
        seen_descs.add(fm_desc)
        vh_match = _EV_VH_RE.search(content)
        vehicle_hazard = vh_match.group(1).strip() if vh_match else ""
        word = ""
        for w in _WORDS:
            if w in fm_desc:
                word = w
                break
        result.append({
            "word": word,
            "malfunction_behavior": fm_desc,
            "vehicle_hazard": vehicle_hazard,
        })
    return result
