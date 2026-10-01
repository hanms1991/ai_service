# -*- coding: utf-8 -*-
"""DOCX 表格/段落底层解析工具（平台无关，不依赖 markitdown/mammoth）。

存在意义：markitdown（mammoth 后端）转换 Word 表格时，对纵向合并单元格
（w:vMerge）的续行采取"丢弃单元格"策略，导致续行单元格整体左移错列，
合并语义丢失。本模块直接解析 word/document.xml：

- :func:`table_grid`：把单个表格还原为对齐的二维文本网格（纵向合并向下
  填充、横向合并 gridSpan 展开）；
- :func:`iter_blocks`：按文档顺序迭代「标题 / 表格」块，供技能侧做
  模板无关的结构化抽取（列位不固定也能按表头语义定位）；
- :func:`fill_vertical_merges`：生成一份把纵向合并单元格"实物化"的
  新 docx（续行填入与首行相同的段落副本并移除 vMerge 标记），
  供 read_document 交给 markitdown 前预处理，全局修复错列问题。

只处理 word/document.xml 正文部分；页眉页脚/文本框等区域中的表格不在
范围内（相关项定义类文档的功能清单均在正文）。任何解析异常交调用方处理。
"""
from __future__ import annotations

import copy
import zipfile
from collections.abc import Iterator
from pathlib import Path

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def qn(tag: str) -> str:
    return f"{{{W}}}{tag}"


# ── 基础读取 ────────────────────────────────────────────────────────

def _paragraph_texts(tc: etree._Element) -> list[str]:
    """单元格内**直接**段落的文本（嵌套表格段落不计入，避免串格）。"""
    out: list[str] = []
    for p in tc.findall("./" + qn("p")):
        parts: list[str] = []
        for node in p.iter():
            if node.tag == qn("t"):
                parts.append(node.text or "")
            elif node.tag == qn("tab"):
                parts.append("\t")
            elif node.tag in (qn("br"), qn("cr")):
                parts.append("\n")
        out.append("".join(parts).strip())
    return out


def _grid_span(tc: etree._Element) -> int:
    gs = tc.find("./" + qn("tcPr") + "/" + qn("gridSpan"))
    if gs is None:
        return 1
    try:
        return max(1, int(gs.get(qn("val")) or 1))
    except (TypeError, ValueError):
        return 1


def _vmerge_kind(tc: etree._Element) -> str | None:
    """返回 restart / continue / None。

    OOXML 约定：w:vMerge 无 val 属性等价于 continue。
    """
    vm = tc.find("./" + qn("tcPr") + "/" + qn("vMerge"))
    if vm is None:
        return None
    val = (vm.get(qn("val")) or "continue").strip().lower()
    return "restart" if val == "restart" else "continue"


def _grid_col_count(tbl: etree._Element) -> int:
    grid = tbl.find("./" + qn("tblGrid"))
    n = len(grid.findall("./" + qn("gridCol"))) if grid is not None else 0
    if n <= 0:
        rows = tbl.findall("./" + qn("tr"))
        n = max(
            (sum(_grid_span(tc) for tc in tr.findall("./" + qn("tc"))) for tr in rows),
            default=0,
        )
    return max(1, n)


def table_grid(tbl: etree._Element) -> list[list[str]]:
    """把单个 w:tbl 还原为矩形文本网格。

    - 纵向合并（vMerge）：restart 行记录内容，continue 行向下填充；
    - 横向合并（gridSpan）：同一内容展开到所跨列；
    - 每行长度均等于网格列数，彻底消除下游"列左移错列"问题。
    """
    n = _grid_col_count(tbl)
    carried: list[list[str] | None] = [None] * n
    rows: list[list[str]] = []
    for tr in tbl.findall("./" + qn("tr")):
        row = [""] * n
        col = 0
        for tc in tr.findall("./" + qn("tc")):
            span = min(_grid_span(tc), n - col)
            if span <= 0:
                continue
            kind = _vmerge_kind(tc)
            paras = _paragraph_texts(tc)
            text = "\n".join(paras).strip()
            if kind == "continue":
                payload = carried[col] if carried[col] is not None else paras
                fill = "\n".join(payload).strip()
            else:
                # restart 或普通单元格：刷新各列承载内容
                payload = paras
                fill = text
            for k in range(col, col + span):
                row[k] = fill
                carried[k] = payload
            col += span
        rows.append(row)
    return rows


def _heading_level(p: etree._Element) -> int | None:
    pPr = p.find("./" + qn("pPr"))
    if pPr is None:
        return None
    style = pPr.find("./" + qn("pStyle"))
    if style is not None:
        val = (style.get(qn("val")) or "").strip()
        if val.lower().startswith("heading"):
            digits = "".join(ch for ch in val if ch.isdigit())
            if digits:
                return int(digits)
        if val.isdigit():
            return int(val)
    outline = pPr.find("./" + qn("outlineLvl"))
    if outline is not None:
        try:
            return int(outline.get(qn("val"))) + 1  # outlineLvl 从 0 起
        except (TypeError, ValueError):
            return None
    return None


def _paragraph_text(p: etree._Element) -> str:
    return "".join(
        (t.text or "") for t in p.iter(qn("t"))
    ).strip()


def iter_blocks(docx_path: str | Path) -> Iterator[dict]:
    """按正文顺序迭代块：

    - ``{"kind": "heading", "level": int, "text": str}``
    - ``{"kind": "table", "index": int, "rows": [[str, ...], ...]}``

    普通段落不产出（调用方目前只需要标题与表格）。
    """
    with zipfile.ZipFile(str(docx_path)) as zf:
        root = etree.fromstring(zf.read("word/document.xml"))
    body = root.find("./" + qn("body"))
    if body is None:
        return
    table_index = 0
    for child in body:
        if child.tag == qn("p"):
            level = _heading_level(child)
            if level is not None:
                text = _paragraph_text(child)
                if text:
                    yield {"kind": "heading", "level": level, "text": text}
        elif child.tag == qn("tbl"):
            yield {
                "kind": "table",
                "index": table_index,
                "rows": table_grid(child),
            }
            table_index += 1


# ── 纵向合并实物化（供 markitdown 预处理） ───────────────────────────

def _strip_vmerge(tc: etree._Element) -> None:
    tcPr = tc.find("./" + qn("tcPr"))
    if tcPr is None:
        return
    vm = tcPr.find("./" + qn("vMerge"))
    if vm is not None:
        tcPr.remove(vm)


def _fill_table_merges(tbl: etree._Element) -> None:
    """就地把表格的 vMerge 续行单元格填入首行段落副本，并移除全部 vMerge。"""
    n = _grid_col_count(tbl)
    carried: list[list[etree._Element] | None] = [None] * n
    for tr in tbl.findall("./" + qn("tr")):
        col = 0
        for tc in tr.findall("./" + qn("tc")):
            span = min(_grid_span(tc), n - col)
            if span <= 0:
                continue
            kind = _vmerge_kind(tc)
            own_paras = tc.findall("./" + qn("p"))
            if kind == "continue":
                payload = carried[col] if carried[col] is not None else own_paras
                # 仅替换直接段落，保留嵌套表格/图片等其它子节点
                for p in own_paras:
                    tc.remove(p)
                insert_at = 0
                for i, child in enumerate(list(tc)):
                    if child.tag != qn("tcPr"):
                        insert_at = i
                        break
                else:
                    insert_at = len(list(tc))
                for offset, p in enumerate(payload):
                    tc.insert(insert_at + offset, copy.deepcopy(p))
            else:
                clones = [copy.deepcopy(p) for p in own_paras]
                for k in range(col, col + span):
                    carried[k] = clones
            _strip_vmerge(tc)
            col += span


def fill_vertical_merges(src_path: str | Path, dst_path: str | Path) -> None:
    """生成一份纵向合并已实物化的新 docx。

    解析 word/document.xml 中全部表格（含嵌套表），续行单元格填入合并
    首行的段落副本，再移除 vMerge 标记——下游转换器无需理解合并语义，
    每行都会输出完整列数。原文件不修改，其余 zip 条目原样复制。
    """
    src_path, dst_path = Path(src_path), Path(dst_path)
    with zipfile.ZipFile(str(src_path)) as zin:
        infos = zin.infolist()
        payloads = {info.filename: zin.read(info.filename) for info in infos}

    root = etree.fromstring(payloads["word/document.xml"])
    for tbl in root.iter(qn("tbl")):
        _fill_table_merges(tbl)
    payloads["word/document.xml"] = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(dst_path), "w", zipfile.ZIP_DEFLATED) as zout:
        for info in infos:
            zout.writestr(info, payloads[info.filename])
