# -*- coding: utf-8 -*-
"""
HARA 工作簿渲染器（团队标准模板版，模板化填充）。

输入：规划层 + 评级层合并后的 JSON（见 skill 契约 output.schema）
输出：与 references/hara_template.xlsx 同构的 11-sheet HARA 工作簿。

职责边界（LLM 出语义，渲染器做确定性工作）：
- 编号：func / MF / hzrd / SG / SG_VH 全部由本脚本按 domain_prefix 生成；
- ASIL：由数字 S/E/C 按 ISO 26262-3 Table 4 矩阵反算，LLM 不输出 ASIL；
- 安全目标：按目标文本确定性合并为整车安全目标，继承最高 ASIL；
- 校验：S/E/C 越界、QM 错挂/显著漏挂安全目标 → 单元格标黄并写入注释列；
- 溯源：每个条目的 source（reused/adapted/new）落到各 sheet 备注列。
"""
from __future__ import annotations

import json
from collections import OrderedDict, defaultdict
from copy import copy
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

_TEMPLATE = Path(__file__).resolve().parent.parent / "references" / "hara_template.xlsx"

# ── 11 个标准失效词（与 malfunctions.md / 模板列顺序一致） ──
WORDS = ["丢失", "非预期", "间歇", "过多", "过少", "过早", "反向", "振荡", "部分", "过晚", "卡滞"]

# ── ISO 26262-3 Table 4（S,E）→ {C: ASIL}；S=0/E=0/C=0 一律 QM ──
_ASIL_TABLE = {
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
_ASIL_RANK = {"QM": 0, "A": 1, "B": 2, "C": 3, "D": 4}

_FONT = Font(name="微软雅黑", size=10)
_FONT_BOLD = Font(name="微软雅黑", size=10, bold=True)
_FONT_TITLE = Font(name="微软雅黑", size=14, bold=True)
_THIN = Side(style="thin", color="999999")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_WRAP = Alignment(horizontal="left", vertical="center", wrap_text=True)
_WRAP_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
_FILL_WARN = PatternFill("solid", fgColor="FFF2CC")


# ════════════════════════════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════════════════════════════

def _style(cell, *, center=False, bold=False, fill=None):
    cell.font = _FONT_BOLD if bold else _FONT
    cell.alignment = _WRAP_CENTER if center else _WRAP
    cell.border = _BORDER
    if fill is not None:
        cell.fill = fill


def _source_remark(src) -> str:
    """source 对象 → 备注列文案；缺失时按新增处理。"""
    if not isinstance(src, dict):
        return "【新增】"
    kind = str(src.get("type") or "new").strip().lower()
    project = str(src.get("project") or "").strip()
    ref_id = str(src.get("ref_id") or "").strip()
    label = {"reused": "【沿用】", "adapted": "【改编】", "new": "【新增】"}.get(kind, "【新增】")
    tail = project
    if ref_id:
        tail = f"{project}#{ref_id}" if project else ref_id
    return f"{label}{tail}" if tail else label


def _asil_of(s: int, e: int, c: int) -> str | None:
    """数字 S/E/C → ASIL；越界返回 None。"""
    if not (0 <= s <= 3 and 0 <= e <= 4 and 0 <= c <= 3):
        return None
    if s == 0 or e == 0 or c == 0:
        return "QM"
    return _ASIL_TABLE[(s, e)][c]


def _to_int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _merge(ws, rng: str):
    ws.merge_cells(rng)


# ════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════

def generate(in_path: str, out_path: str) -> dict:
    data = json.loads(Path(in_path).read_text(encoding="utf-8"))
    wb = load_workbook(_TEMPLATE)

    item = data.get("item") or {}
    pfx = str(item.get("domain_prefix") or "XX").strip() or "XX"
    functions = data.get("functions") or []
    matrix = {m.get("fid"): m for m in (data.get("malfunction_matrix") or [])}
    hazop = data.get("hazop_items") or []

    # fid → 功能序号/信息；hazop 按功能顺序分组
    func_info: dict[str, dict] = {}
    for i, f in enumerate(functions, 1):
        func_info[f.get("fid")] = {"seq": i, "data": f}
    items_by_func: dict[str, list] = OrderedDict()
    for f in functions:
        items_by_func[f.get("fid")] = []
    for it in hazop:
        fid = it.get("fid")
        if fid in items_by_func:
            items_by_func[fid].append(it)

    # ── 预编号：MF / hzrd / 中间 SG（编号结果随事件结构一起传给各 sheet 写入器） ──
    # event_records: 扁平化的全部评级事件（携带编号与归属）
    event_records = []
    mf_id_of_item: dict[int, str] = {}
    for fid, items in items_by_func.items():
        fseq = func_info[fid]["seq"]
        for j, unit in enumerate(items, 1):
            mf_id = f"{pfx}_MF_{fseq:04d}_{j:02d}"
            mf_id_of_item[id(unit)] = mf_id
        ev_k = 0
        for unit in items:
            for ev in unit.get("events") or []:
                ev_k += 1
                event_records.append({
                    "fid": fid, "fseq": fseq,
                    "func_name": func_info[fid]["data"].get("vehicle_function", ""),
                    "unit": unit, "mf_id": mf_id_of_item[id(unit)],
                    "event": ev, "ev_k": ev_k,
                    "hzrd_id": f"{pfx}_hzrd_{fseq:02d}{ev_k:03d}",
                })

    stats = _write_version(wb, item)
    _write_functions(wb, pfx, functions, func_info)
    _write_malfunctions(wb, pfx, functions, func_info, matrix)
    _write_hazop(wb, pfx, functions, func_info, items_by_func, mf_id_of_item, event_records)
    sg_left, vh_goals = _write_hara(wb, pfx, functions, func_info, items_by_func, event_records)
    _write_safety_goals(wb, sg_left, vh_goals)

    # 统计
    stats.update({
        "functions": len(functions),
        "sc_malfunctions": len(hazop),
        "hara_events": len(event_records),
        "events_qm": sum(1 for r in event_records if r["asil"] == "QM"),
        "events_asil": sum(1 for r in event_records if r["asil"] != "QM"),
        "safety_goals_vh": len(vh_goals),
        "reused": 0, "adapted": 0, "new": 0,
    })
    for r in event_records:
        kind = str((r["event"].get("source") or {}).get("type") or "new").lower()
        stats[kind if kind in ("reused", "adapted", "new") else "new"] += 1

    wb.save(out_path)
    return stats


# ════════════════════════════════════════════════════════════════
# Sheet 写入器
# ════════════════════════════════════════════════════════════════

def _write_version(wb, item) -> dict:
    # 新模板仅保留 R1 表头，版本记录从 R2 起逐行写（不触碰表头格式）
    ws = wb["版本管理"]
    row = ["V1.0", datetime.now().strftime("%Y-%m-%d"), "AI", "全文",
           f"HARA 初版（AI 辅助生成，待评审）；相关项：{item.get('name', '')}"]
    for c, v in enumerate(row, 1):  # A..E
        cell = ws.cell(row=2, column=c, value=v)
        _style(cell)
    return {}


def _write_functions(wb, pfx, functions, func_info):
    ws = wb["相关项功能清单"]
    r = 2
    for f in functions:
        fseq = func_info[f.get("fid")]["seq"]
        func_id = f"{pfx}_func_{fseq:04d}"
        features = f.get("features") or [{}]
        start = r
        remark = _source_remark(f.get("source"))
        doc_ref = f.get("doc_ref") or ""
        for k, feat in enumerate(features):
            do_hara = str(feat.get("do_hara") or "是").strip()
            ws.cell(row=r, column=3, value=feat.get("feature_list_id") or "")
            ws.cell(row=r, column=4, value=feat.get("description") or "")
            ws.cell(row=r, column=5, value=do_hara)
            ws.cell(row=r, column=6, value=feat.get("doc_ref") or doc_ref)
            # 备注列：do_hara=否 的 feature 必须写明不进行 HARA 分析的理由；
            # 首行无排除理由时回落为功能级来源标注
            reason = str(feat.get("no_hara_reason") or "").strip()
            if do_hara == "否" and reason:
                g_val = reason
            elif k == 0:
                g_val = remark
            else:
                g_val = ""
            ws.cell(row=r, column=7, value=g_val)
            for c in range(3, 8):
                _style(ws.cell(row=r, column=c))
            r += 1
        if r - start >= 2:
            _merge(ws, f"A{start}:A{r - 1}")
            _merge(ws, f"B{start}:B{r - 1}")
        ws.cell(row=start, column=1, value=func_id)
        ws.cell(row=start, column=2, value=f.get("vehicle_function") or "")
        _style(ws.cell(row=start, column=1), center=True, bold=True)
        _style(ws.cell(row=start, column=2), bold=True)


def _write_malfunctions(wb, pfx, functions, func_info, matrix):
    ws = wb["失效模式"]
    # 新模板表头为两行分组（R1:R2，含合并），数据从 R3 起；不触碰表头。
    # O 列「备注」由渲染器补齐（模板只到 N=选择理由）：表头样式复制 N1。
    if ws.max_column < 15:
        _merge(ws, "O1:O2")
        hdr = ws.cell(row=1, column=15, value="备注")
        src_hdr = ws.cell(row=1, column=14)
        hdr.font = copy(src_hdr.font)
        hdr.fill = copy(src_hdr.fill)
        hdr.border = copy(src_hdr.border)
        hdr.alignment = copy(src_hdr.alignment)
        ws.column_dimensions["O"].width = 30.0

    r = 3
    for f in functions:
        fid = f.get("fid")
        m = matrix.get(fid) or {}
        fseq = func_info[fid]["seq"]
        ws.cell(row=r, column=1, value=f"{pfx}_func_{fseq:04d}")
        ws.cell(row=r, column=2, value=f.get("vehicle_function") or "")
        selections = m.get("selections") or {}
        for k, word in enumerate(WORDS):
            ws.cell(row=r, column=3 + k, value="√" if selections.get(word) else "")
        ws.cell(row=r, column=14, value=m.get("rationale") or "")
        # O 列备注：逐词溯源说明（LLM 输出 source_note），缺失时回退条目级 source
        ws.cell(row=r, column=15, value=(m.get("source_note") or "").strip()
                or _source_remark(m.get("source")))
        for c in range(1, 16):
            _style(ws.cell(row=r, column=c), center=(c in (1,) or 3 <= c <= 13),
                   bold=(c <= 2))
        r += 1

    # 词表注释已取消（覆盖关系由模型在选择理由中自行分析，不写编号注释行）。
    # 图片统一放在数据内容下方一行，显式 OneCellAnchor ext，并按原始尺寸的 70%
    # 缩放（不显式给 ext 时图片会显示为极小尺寸）。
    img_row = r  # r 已是最后一条数据的下一行
    try:
        from openpyxl.drawing.spreadsheet_drawing import OneCellAnchor, AnchorMarker
        from openpyxl.drawing.xdr import XDRPositiveSize2D
        from openpyxl.utils.units import pixels_to_EMU

        _IMG_SCALE = 0.70
        for img in getattr(ws, "_images", []):
            w = int(getattr(img, "width", 0) or 640)
            h = int(getattr(img, "height", 0) or 360)
            cx = pixels_to_EMU(max(1, int(round(w * _IMG_SCALE))))
            cy = pixels_to_EMU(max(1, int(round(h * _IMG_SCALE))))
            img.anchor = OneCellAnchor(
                _from=AnchorMarker(col=0, colOff=0, row=img_row - 1, rowOff=0),
                ext=XDRPositiveSize2D(cx, cy),
            )
    except Exception:
        pass  # 锚点调整失败不影响出表


def _write_hazop(wb, pfx, functions, func_info, items_by_func, mf_id_of_item, event_records):
    ws = wb["HAZOP 分析"]
    # 表头/标题（R1 合并标题、R2 列名）模板已自带并格式化，数据从 R3 起，不触碰表头。

    # 每个失效单元的事件 ID 区间
    range_of_item: dict[int, str] = {}
    grouped: dict[int, list[str]] = defaultdict(list)
    for rec in event_records:
        grouped[id(rec["unit"])].append(rec["hzrd_id"])
    for key, ids in grouped.items():
        ids_sorted = sorted(ids)
        range_of_item[key] = ids_sorted[0] if len(ids_sorted) == 1 else f"{ids_sorted[0]}~{ids_sorted[-1]}"

    r = 3
    for fid, items in items_by_func.items():
        if not items:
            continue
        fseq = func_info[fid]["seq"]
        fname = func_info[fid]["data"].get("vehicle_function") or ""
        start = r
        for unit in items:
            ws.cell(row=r, column=2, value=fname)
            ws.cell(row=r, column=3, value=unit.get("word") or "")
            ws.cell(row=r, column=4, value=unit.get("malfunction_behavior") or "")
            ws.cell(row=r, column=5, value=mf_id_of_item[id(unit)])
            ws.cell(row=r, column=6, value=unit.get("vehicle_hazard") or "")
            ws.cell(row=r, column=7, value=range_of_item.get(id(unit), ""))
            # H 列备注：成分级溯源说明（LLM 输出 source_note），缺失时回退条目级 source
            ws.cell(row=r, column=8, value=(unit.get("source_note") or "").strip()
                    or _source_remark(unit.get("source")))
            for c in range(2, 9):
                _style(ws.cell(row=r, column=c), center=(c in (3, 5, 7)))
            r += 1
        ws.cell(row=start, column=1, value=f"{pfx}_func_{fseq:04d}")
        _style(ws.cell(row=start, column=1), center=True, bold=True)
        if r - start >= 2:
            _merge(ws, f"A{start}:A{r - 1}")
            _merge(ws, f"B{start}:B{r - 1}")


def _write_hara(wb, pfx, functions, func_info, items_by_func, event_records):
    ws = wb["HARA 分析"]
    # 两级表头（A1:A2/B1:B2/C1:C2/D1:E1/F1:G1/H1:O1/P1:S1/T1:T2）模板已自带并
    # 格式化，数据从 R3 起；严禁在此重建合并（旧 P1:T1 会与新模板 P1:S1+T1:T2 冲突）。

    # 每功能 SG 独立序号
    sg_counter: dict[str, int] = defaultdict(int)
    sg_left = []   # 中间安全目标行（仅 ASIL≥A）
    r = 3
    for fid, items in items_by_func.items():
        fseq = func_info[fid]["seq"]
        fname = func_info[fid]["data"].get("vehicle_function") or ""
        func_records = [x for x in event_records if x["fid"] == fid]
        if not func_records:
            continue
        # 不再写功能横幅合并行：连续数据行（功能名在 B 列、失效 ID 在 C 列），
        # 以保证 Excel 自动筛选可用。

        for rec in func_records:
            ev = rec["event"]
            s = _to_int(ev.get("S"))
            e = _to_int(ev.get("E"))
            c = _to_int(ev.get("C"))
            asil = _asil_of(s, e, c) if s is not None and e is not None and c is not None else None
            rec["asil"] = asil or "QM"
            warnings = []
            if asil is None:
                asil = "CHECK"
                warnings.append("S/E/C 越界，请复核")

            sg_text = str(ev.get("sg_text") or "").strip()
            safe_state = str(ev.get("safe_state") or "").strip()
            ftti = str(ev.get("ftti") or "").strip()
            sg_id = ""
            if asil not in ("QM", "CHECK"):
                sg_counter[fid] += 1
                sg_id = f"{pfx}_SG_{fseq:02d}{sg_counter[fid]:04d}"
                sg_left.append({
                    "sg_id": sg_id, "text": sg_text, "asil": asil,
                    "safe_state": safe_state, "ftti": ftti,
                    "source": ev.get("source"),
                })
                if not sg_text:
                    warnings.append("显著事件缺少安全目标")
            elif sg_text:
                warnings.append("QM 事件不应挂安全目标")

            values = [
                rec["hzrd_id"], fname, rec["mf_id"],
                rec["unit"].get("malfunction_behavior") or "",
                rec["unit"].get("vehicle_hazard") or "",
                ev.get("scenario_text") or "",
                ev.get("event_description") or "",
                s if s is not None else ev.get("S"),
                ev.get("s_reason") or "",
                e if e is not None else ev.get("E"),
                ev.get("e_reason") or "",
                c if c is not None else ev.get("C"),
                ev.get("c_basis") or "",
                ev.get("c_reason") or "",
                asil,
                sg_id, sg_text, safe_state, ftti,
                _source_remark(ev.get("source")) + ("；" + "；".join(warnings) if warnings else ""),
            ]
            for col, v in enumerate(values, 1):
                cell = ws.cell(row=r, column=col, value=v)
                center_cols = (1, 2, 3, 8, 10, 12, 15, 16)
                _style(cell, center=(col in center_cols))
            if warnings:
                for col in (8, 10, 12, 15, 17, 20):
                    ws.cell(row=r, column=col).fill = _FILL_WARN
            r += 1

    # 整车安全目标合并（按目标文本全局合并，继承最高 ASIL）
    vh_goals_map: dict[str, dict] = OrderedDict()
    for row in sg_left:
        text = row["text"]
        if text not in vh_goals_map:
            vh_goals_map[text] = {"text": text, "members": []}
        vh_goals_map[text]["members"].append(row)
    vh_goals = []
    for n, (text, g) in enumerate(vh_goals_map.items(), 1):
        members = g["members"]
        top = max(members, key=lambda m: _ASIL_RANK.get(m["asil"], 0))
        projects = sorted({
            str((m.get("source") or {}).get("project") or "").strip()
            for m in members if str((m.get("source") or {}).get("project") or "").strip()
        })
        remark = ("【沿用】" + "；".join(projects)) if projects else "/"
        if any(str((m.get("source") or {}).get("type") or "") in ("new", "adapted") for m in members):
            tags = []
            if projects:
                tags.append("【沿用】" + "；".join(projects))
            kinds = {str((m.get("source") or {}).get("type") or "new") for m in members}
            if "adapted" in kinds:
                tags.append("含【改编】条目")
            if "new" in kinds:
                tags.append("含【新增】条目")
            remark = "；".join(tags)
        vh_goals.append({
            "vh_id": f"{pfx}_SG_VH_{n:04d}",
            "text": text, "asil": top["asil"],
            "safe_state": top["safe_state"], "ftti": top["ftti"],
            "remark": remark,
        })
    return sg_left, vh_goals


def _write_safety_goals(wb, sg_left, vh_goals):
    """整车安全目标：直接在模板 sheet 上填充（保留用户格式化的 R1:R2 表头）。

    新模板布局（11 列，R1 分组 / R2 列名）：
      左区「安全目标（整理合并前）」A-E：安全目标ID/ASIL/安全目标/安全状态/FTTI
      右区「整车安全目标」          F-J：整车安全目标ID/ASIL/整车安全目标/安全状态/FTTI
      K=备注（服务右区整车目标的来源标注）
    数据从 R3 起。
    """
    ws = wb["整车安全目标"]

    # 防御性清空 R3 以下残留（模板本身为空），不触碰 R1:R2 与其合并
    if ws.max_row >= 3:
        for row in ws.iter_rows(min_row=3, max_col=11):
            for cell in row:
                cell.value = None

    r = 3
    for row in sg_left:
        vals = [row["sg_id"], row["asil"], row["text"], row["safe_state"], row["ftti"]]
        for c, v in enumerate(vals, 1):
            _style(ws.cell(row=r, column=c, value=v), center=(c in (1, 2, 5)))
        r += 1

    r = 3
    for g in vh_goals:
        vals = [g["vh_id"], g["asil"], g["text"], g["safe_state"], g["ftti"], g["remark"]]
        for c, v in enumerate(vals, 6):  # F..K
            _style(ws.cell(row=r, column=c, value=v), center=(c in (6, 7, 10)))
        r += 1


# ════════════════════════════════════════════════════════════════
# 交付摘要（引擎可选入口 summarize；HARA 专属展示逻辑随渲染器放在技能侧）
# ════════════════════════════════════════════════════════════════

def _extract_vehicle_goals(artifact_path: str, limit: int = 15) -> list[dict]:
    """读取成品工作簿「整车安全目标」表右区合并后的整车安全目标（尽力而为）。

    模板布局：R1 分组表头 / R2 列名，数据从 R3 起。
    右区 F 整车安全目标ID / G ASIL / H 整车安全目标 / I 安全状态 / J FTTI / K 备注。
    """
    try:
        wb = load_workbook(artifact_path, read_only=True, data_only=False)
        if "整车安全目标" not in wb.sheetnames:
            return []
        ws = wb["整车安全目标"]
        goals: list[dict] = []
        for row in ws.iter_rows(min_row=3, values_only=True):
            vh_id = row[5] if len(row) > 5 else None  # F 列
            if not vh_id or not str(vh_id).strip():
                continue
            goals.append({
                "sg_id": vh_id,
                "asil": row[6] if len(row) > 6 else "",   # G 列
                "goal": row[7] if len(row) > 7 else "",   # H 列
            })
        wb.close()
        goals.sort(
            key=lambda g: _ASIL_RANK.get(str(g["asil"]).strip(), -1), reverse=True
        )
        return goals[:limit]
    except Exception:
        return []


def summarize(in_json_path: str, artifact_path: str) -> str:
    """引擎 output.renderer.summary_entrypoint：返回附加 markdown 段落。"""
    goals = _extract_vehicle_goals(artifact_path)
    if not goals:
        return ""
    lines = [
        f"**整车安全目标清单（按最高 ASIL 排序，前 {len(goals)} 条，全量见工作簿）**：",
        "",
        "| 整车安全目标 ID | ASIL | 安全目标 |",
        "|---|---|---|",
    ]
    for g in goals:
        goal_text = str(g["goal"]).replace("|", "／").replace("\n", " ")
        lines.append(f"| {g['sg_id']} | {g['asil']} | {goal_text} |")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print("Usage: python generate_hara.py <in.json> <out.xlsx>")
        sys.exit(1)
    stats = generate(sys.argv[1], sys.argv[2])
    print(json.dumps(stats, ensure_ascii=False, indent=2))
