# -*- coding: utf-8 -*-
"""相关项定义 DOCX → 功能清单结构化抽取（模板无关的确定性解析）。

为什么需要它：平台 Markdown 转换对 Word 合并单元格表格不稳定（纵向合并
续行被丢弃导致错列），且各项目《相关项定义》模板不固定——列顺序、列名、
是否带"是否 HARA/参考章节/备注"列、一个功能编号是否跨多行展开实现要素
都不一致。纯靠 LLM 读错列的 Markdown 会把同一功能编号的多行实现要素误
枚举成多个重复 Feature。

本模块绕过 Markdown，直接用底层 XML 网格（core.docx_tables 已修正
vMerge/gridSpan），按**表头语义**识别列角色（列位任意、列名宽松匹配），
再按"整车功能（相关项分组）→ Feature（按功能编号归并）"两级输出：

    {
      "source": "table" | "headings",
      "functions": [
        {"id": "CS_func_0001", "name": "转向助力功能", "description": "...",
         "features": [
           {"id": "S-201-01", "name": "基础助力",
            "elements": ["基础助力", "转角监测", "扭矩监测"],
            "do_hara": "", "doc_refs": [], "remarks": []}
         ]}
      ]
    }

任何一步识别不到都软返回 None，由调用方回退到原始文档 Markdown，
不阻断技能执行。可命令行独立运行：python doc_item_table.py <docx>
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 技能脚本在引擎中以独立模块加载，core 包可直接导入；
# 命令行独立运行时把仓库根目录补进 sys.path。
try:
    from core.docx_tables import iter_blocks
except ImportError:  # pragma: no cover - 仅独立运行场景
    for parent in Path(__file__).resolve().parents:
        if (parent / "core" / "docx_tables.py").is_file():
            sys.path.insert(0, str(parent))
            break
    from core.docx_tables import iter_blocks


# ── 表头列角色（顺序即优先级：越具体越靠前） ─────────────────────────
# (role, 包含关键词, 精确匹配词)。短表头（如"相关项""功能"）只允许精确
# 匹配，避免"相关项"被反向包含进"相关项中的子功能"、"功能"误吞
# "相关项描述/功能描述"等宽松误判。
_COLUMN_RULES: list[tuple[str, list[str], set[str]]] = [
    ("item_id",
     ["相关项id", "相关项编号", "相关项编码", "itemid", "item编号",
      "系统id", "系统编号", "总成id", "功能域id"],
     set()),
    ("feature_id",
     ["子功能id", "featurelistid", "featureid", "功能编号",
      "子功能编号", "feature编号", "功能id"],
     set()),
    ("do_hara",
     ["是否进行hara", "是否hara", "hara分析", "是否分析",
      "是否安全相关", "安全相关", "是否纳入", "analyz"],
     set()),
    ("feature_group",
     ["相关项中的子功能", "功能组成", "子功能组", "功能组",
      "组成要素", "子要素", "实现要素"],
     set()),
    ("feature_name",
     ["子功能名称", "子功能名", "子功能", "featurename",
      "功能名称", "功能名", "功能项", "feature"],
     {"功能"}),
    ("item_name",
     ["相关项名称", "系统名称", "总成名称", "itemname", "item名称"],
     {"相关项", "系统", "总成", "item"}),
    ("doc_ref",
     ["参考章节", "关联章节", "文档章节", "来源章节", "章节号",
      "章节", "参考编号", "引用编号", "文档参考", "reference", "ref"],
     set()),
    ("remark",
     ["不进行hara理由", "不分析理由", "排除理由", "备注", "注释",
      "remark", "note", "理由", "说明"],
     set()),
    ("item_desc",
     ["相关项描述", "系统描述", "总成描述", "功能描述", "描述", "定义"],
     set()),
]

_REQUIRED_ROLES = ("feature_name",)  # feature_id 可缺（无编号模板按名称归并）
_HEADING_HINT = ("功能清单", "功能列表", "featurelist", "feature list")
_FUNC_DESC_HINT = ("功能描述", "功能定义", "功能说明")

_HARA_TRUE = ("是", "y", "yes", "true", "√", "✓", "1", "纳入", "进行")
_HARA_FALSE = ("否", "n", "no", "false", "×", "x", "0", "不纳入", "不进行")


def _norm_cell(s: str) -> str:
    return re.sub(r"[\s　*:：|丨\[\]（）()【】]+", "", str(s or "")).lower()


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").replace("|", "丨")).strip()


def _looks_like_id(s: str) -> bool:
    s = str(s or "").strip()
    return bool(s) and len(s) <= 40 and bool(re.search(r"\d", s)) and (
        " " not in s and not re.search(r"[一-鿿]", s)
    )


def _norm_key(s: str) -> str:
    return re.sub(r"[\s　，。、；：,.;:!？?()（）\"'“”‘’\-—_/]+", "",
                  str(s or "")).lower()


def _parse_do_hara(s: str) -> str:
    v = _clean(s).lower()
    if not v:
        return ""
    if any(v == t or v.startswith(t) for t in _HARA_TRUE):
        return "是"
    if any(v == t or v.startswith(t) for t in _HARA_FALSE):
        return "否"
    if "不" in v and ("hara" in v or "分析" in v):
        return "否"
    if "hara" in v or "分析" in v or "安全" in v:
        return "是"
    return ""


# ── 表头识别与选表 ──────────────────────────────────────────────────

def _map_columns(header: list[str]) -> dict[str, int]:
    """按表头语义把列角色映射到列索引；同一角色取最靠左的命中列。"""
    roles: dict[str, int] = {}
    norms = [_norm_cell(c) for c in header]
    for ci, cell in enumerate(norms):
        if not cell:
            continue
        for role, substr_kws, exact_kws in _COLUMN_RULES:
            if role in roles:
                continue
            if cell in exact_kws or any(kw in cell for kw in substr_kws):
                roles[role] = ci
                break
    return roles


def _score_header(roles: dict[str, int], header: list[str]) -> int:
    score = len(roles)
    if "feature_id" in roles:
        score += 2
    if "item_name" in roles or "item_id" in roles:
        score += 1
    if "do_hara" in roles:
        score += 1
    return score


def _locate_table(blocks: list[dict]) -> tuple[list[list[str]], dict[str, int],
                                               list[str], str] | None:
    """在全部表格中选最像"功能清单"的一张，返回(网格, 列角色, 表头行, 来源提示)。"""
    best = None
    recent_headings: list[str] = []
    for blk in blocks:
        if blk["kind"] == "heading":
            recent_headings.append(blk["text"])
            recent_headings = recent_headings[-6:]
            continue
        rows = blk["rows"]
        if len(rows) < 2:
            continue
        context_hit = any(
            any(h in _norm_cell(htext) for h in _HEADING_HINT)
            for htext in recent_headings
        )
        candidate = None
        for ri in range(min(4, len(rows) - 1)):
            roles = _map_columns(rows[ri])
            if not any(r in roles for r in _REQUIRED_ROLES):
                continue
            data_count = 0
            fcol = roles.get("feature_name")
            idcol = roles.get("feature_id")
            for row in rows[ri + 1:]:
                fval = _clean(row[fcol]) if fcol is not None and fcol < len(row) else ""
                idval = _clean(row[idcol]) if idcol is not None and idcol < len(row) else ""
                if fval or idval:
                    data_count += 1
            if data_count < 2:
                continue
            score = _score_header(roles, rows[ri]) + (3 if context_hit else 0)
            if candidate is None or score > candidate[0]:
                candidate = (score, ri, roles, data_count)
        if candidate is None:
            continue
        score, ri, roles, _ = candidate
        if best is None or score > best[0]:
            best = (score, rows, roles, rows[ri],
                    "table" if context_hit else "table")
    if best is None:
        return None
    _, rows, roles, header, source = best
    return rows, roles, header, source


# ── 表格 → 两级结构 ─────────────────────────────────────────────────

def _cell(row: list[str], col: int | None) -> str:
    if col is None or col >= len(row):
        return ""
    return _clean(row[col])


def _build_from_table(rows: list[list[str]], roles: dict[str, int],
                      header: list[str]) -> dict:
    header_norms = {_norm_cell(c) for c in header}
    functions: list[dict] = []
    func_index: dict[tuple[str, str], int] = {}
    feat_index: dict[tuple[str, int], int] = {}

    for row in rows:
        if row is header:
            continue
        item_id = _cell(row, roles.get("item_id"))
        item_name = _cell(row, roles.get("item_name"))
        item_desc = _cell(row, roles.get("item_desc"))
        fid = _cell(row, roles.get("feature_id"))
        if fid and not _looks_like_id(fid):
            # 编号列被标题/说明行污染时不计为编号
            fid = fid if re.search(r"S[-_]?\d|func|FS[-_]?\d|F\d{2,}", fid, re.I) else ""
        fname = _cell(row, roles.get("feature_name"))
        group_name = _cell(row, roles.get("feature_group"))
        do_hara = _parse_do_hara(_cell(row, roles.get("do_hara")))
        doc_ref = _cell(row, roles.get("doc_ref"))
        remark = _cell(row, roles.get("remark"))

        if not fid and not fname:
            continue
        # 跳过表头重复行 / 跨列表题行
        if _norm_cell(fname) in header_norms and not fid:
            continue

        fkey = (item_id, item_name)
        if fkey not in func_index:
            func_index[fkey] = len(functions)
            functions.append({
                "id": item_id, "name": item_name,
                "description": item_desc, "features": [],
            })
        else:
            if item_desc and not functions[func_index[fkey]]["description"]:
                functions[func_index[fkey]]["description"] = item_desc
        fn = functions[func_index[fkey]]

        feat_uid = (id(fn), fid.lower() if fid else f"name:{_norm_key(fname)}")
        if feat_uid not in feat_index:
            feat_index[feat_uid] = len(fn["features"])
            fn["features"].append({
                "id": fid, "name": fname or group_name,
                "elements": [], "do_hara": do_hara,
                "doc_refs": [], "remarks": [],
            })
        feat = fn["features"][feat_index[feat_uid]]

        for element in (group_name, fname):
            element = _clean(element)
            if element and _norm_key(element) not in {
                _norm_key(e) for e in feat["elements"]
            } and (not feat["name"] or element != feat["name"]
                   or not feat["elements"]):
                feat["elements"].append(element)
        # do_hara 跨行合并：任一行为"是"即为"是"；否则取首个非空判定
        if do_hara:
            if do_hara == "是":
                feat["do_hara"] = "是"
            elif not feat["do_hara"]:
                feat["do_hara"] = do_hara
        if doc_ref and doc_ref not in feat["doc_refs"]:
            feat["doc_refs"].append(doc_ref)
        if remark and _norm_key(remark) not in {_norm_key(r) for r in feat["remarks"]}:
            feat["remarks"].append(remark)

    functions = [f for f in functions if f["features"]]
    return {"source": "table", "functions": functions}


# ── 无表格时的章节回退 ───────────────────────────────────────────────

def _build_from_headings(blocks: list[dict]) -> dict | None:
    """从"功能描述"类章节的标题层级推断整车功能与 Feature（编号留空）。"""
    desc_level = None
    funcs: list[dict] = []
    current = None
    for blk in blocks:
        if blk["kind"] != "heading":
            continue
        text = blk["text"]
        level = blk["level"]
        if desc_level is None:
            if any(h in text for h in _FUNC_DESC_HINT) and re.search(r"功能", text):
                desc_level = level
            continue
        if level <= desc_level:
            if any(h in text for h in _FUNC_DESC_HINT):
                desc_level = level
                current = None
                continue
            break
        if level == desc_level + 1:
            name = re.sub(r"^[\d.、\s]+", "", text).strip()
            if not name:
                continue
            current = {"id": "", "name": name, "description": "", "features": []}
            funcs.append(current)
        elif level == desc_level + 2 and current is not None:
            name = re.sub(r"^[\d.、\s]+", "", text).strip()
            if name and name not in {f["name"] for f in current["features"]}:
                current["features"].append({
                    "id": "", "name": name, "elements": [],
                    "do_hara": "", "doc_refs": [], "remarks": [],
                })
    funcs = [f for f in funcs if f["name"]]
    return {"source": "headings", "functions": funcs} if funcs else None


# ── 对外入口与渲染 ──────────────────────────────────────────────────

def extract_item_functions(docx_path: str | Path) -> dict | None:
    """抽取相关项功能清单两级结构；无法识别时返回 None。"""
    blocks = list(iter_blocks(docx_path))
    located = _locate_table(blocks)
    if located is not None:
        rows, roles, header, _ = located
        data = _build_from_table(rows, roles, header)
        if data["functions"]:
            return data
    return _build_from_headings(blocks)


def render_supplement(data: dict) -> str:
    """把抽取结果渲染为注入识别/规划层的权威 Markdown 段落。"""
    if not data or not data.get("functions"):
        return ""
    if data.get("source") == "headings":
        lines = [
            "【系统结构化抽取：相关项功能章节（未识别到功能清单表格，"
            "由章节标题层级推断；功能编号文档未提供，Feature 名称供参照）】"
        ]
        for fi, fn in enumerate(data["functions"], 1):
            lines.append(f"{fi}. 整车功能：{fn['name']}")
            for feat in fn["features"]:
                lines.append(f"   - {feat['name']}")
        return "\n".join(lines)

    lines = [
        "【系统结构化抽取：相关项功能清单（系统直接解析原始 Word 表格，"
        "已修正合并单元格错列并按功能编号归并；整车功能与 Feature 划分以此块为准）】",
        "规则：每个功能编号对应**一个** Feature；同编号下列出的“包含实现要素”"
        "是该 Feature 的内部组成，禁止逐行展开或重复输出多个同编号 Feature；"
        "无编号的 Feature 按名称归一，只保留一条。",
    ]
    for fi, fn in enumerate(data["functions"], 1):
        title = f"{fi}. 整车功能："
        if fn.get("id"):
            title += f"{fn['id']} {fn['name']}"
        else:
            title += fn["name"] or "（文档未命名分组）"
        lines.append(title)
        if fn.get("description"):
            lines.append(f"   描述：{fn['description']}")
        lines.append(f"   Feature 清单（{len(fn['features'])} 项）：")
        for feat in fn["features"]:
            parts = [f"- {feat['id']}｜" if feat["id"] else "- "]
            parts.append(feat["name"] or "（未命名）")
            do_hara = feat.get("do_hara") or "文档未标注（按规划规则判定）"
            parts.append(f"｜是否HARA：{do_hara}")
            elements = [e for e in feat["elements"] if e and e != feat["name"]]
            if elements:
                parts.append(f"｜包含实现要素：{'、'.join(elements)}")
            if feat["doc_refs"]:
                parts.append(f"｜文档参考：{'；'.join(feat['doc_refs'][:5])}")
            if feat["remarks"]:
                parts.append(f"｜备注：{'；'.join(feat['remarks'][:3])}")
            lines.append("".join(parts))
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - 独立调试入口
    if len(sys.argv) != 2:
        print("用法：python doc_item_table.py <相关项定义.docx>")
        raise SystemExit(1)
    result = extract_item_functions(sys.argv[1])
    if result is None:
        print("[未识别到功能清单结构]")
        raise SystemExit(2)
    print(render_supplement(result))
