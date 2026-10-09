"""markdown 渲染器：表格、列表、三层结构三种格式。"""


def render_tiered(summary_columns: list[str], output_columns: list[str], records: list[dict]) -> str:
    """三层渲染：概览表（带编号）→ 详情列表（编号对应）。

    第一层：概览表，只展示 summary_columns 字段，加编号列 [1] [2]...
    第二层：详情列表，按编号对应，展示 output_columns 全部字段，原始内容用引用块
    """
    if not records:
        return "（未检索到匹配记录）"

    s_cols = [str(c) for c in summary_columns] if summary_columns else list(records[0].keys())
    o_cols = [str(c) for c in output_columns] if output_columns else list(records[0].keys())
    # 区分长文本列（"内容"等）
    content_cols = {"内容", "content"}
    short_o = [c for c in o_cols if c not in content_cols]
    long_o = [c for c in o_cols if c in content_cols]

    # 第一层：概览表
    table_header = "| # | " + " | ".join(s_cols) + " |"
    table_sep = "| --- | " + " | ".join("---" for _ in s_cols) + " |"
    table_rows = []
    for i, r in enumerate(records, 1):
        if not isinstance(r, dict):
            r = {}
        cells = [str(i)]
        for c in s_cols:
            val = str(r.get(c, "") or "").replace("\n", " ").replace("|", "\\|")
            cells.append(val)
        table_rows.append("| " + " | ".join(cells) + " |")
    table = "\n".join([table_header, table_sep] + table_rows)

    # 第二层：详情列表
    details = []
    for i, r in enumerate(records, 1):
        if not isinstance(r, dict):
            r = {}
        lines = [f"**[{i}]**"]
        for c in short_o:
            val = str(r.get(c, "") or "").strip()
            if val:
                lines.append(f"- **{c}**：{val}")
        for c in long_o:
            val = str(r.get(c, "") or "").strip()
            if val:
                lines.append("")
                for ln in val.split("\n"):
                    lines.append(f"> {ln}")
        details.append("\n".join(lines))

    detail_text = "\n\n---\n\n".join(details)
    return f"{table}\n\n---\n\n{detail_text}"


def render_table(columns: list[str], records: list[dict]) -> str:
    """渲染为 markdown 表格（适合短字段、无长文本的场景）。"""
    if not records:
        return "（未检索到匹配记录）"
    cols = [str(c) for c in columns] or list(records[0].keys())
    header = "| " + " | ".join(cols) + " |"
    separator = "| " + " | ".join("---" for _ in cols) + " |"
    rows = []
    for r in records:
        if not isinstance(r, dict):
            r = {}
        cells = []
        for c in cols:
            val = str(r.get(c, "") or "").replace("\n", " ").replace("|", "\\|")
            cells.append(val)
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join([header, separator] + rows)


def render_list(columns: list[str], records: list[dict]) -> str:
    """渲染为 markdown 列表（适合含长文本/原始内容的场景）。

    每条记录：字段名加粗逐行列出，原始内容（content_field）作为代码块呈现。
    """
    if not records:
        return "（未检索到匹配记录）"
    cols = [str(c) for c in columns] or list(records[0].keys())
    # 区分"内容"列（长文本）和普通短字段
    content_cols = {"内容", "content"}
    short_cols = [c for c in cols if c not in content_cols]
    long_cols = [c for c in cols if c in content_cols]

    parts = []
    for r in records:
        if not isinstance(r, dict):
            r = {}
        lines = []
        # 短字段逐行加粗
        for c in short_cols:
            val = str(r.get(c, "") or "").strip()
            if val:
                lines.append(f"**{c}**：{val}")
        # 长文本字段用引用块呈现
        for c in long_cols:
            val = str(r.get(c, "") or "").strip()
            if val:
                lines.append("")
                lines.append(f"> **{c}**：")
                for ln in val.split("\n"):
                    lines.append(f"> {ln}")
        parts.append("\n".join(lines))
    return "\n\n---\n\n".join(parts)


def render(columns: list[str], records: list[dict], fmt: str = "table", summary_columns: list[str] = None) -> str:
    """按指定格式渲染。fmt=table / list / tiered。

    fmt=tiered 时使用 summary_columns 渲染概览表、columns 渲染详情列表。
    """
    if fmt == "tiered":
        return render_tiered(summary_columns or columns, columns, records)
    if fmt == "list":
        return render_list(columns, records)
    return render_table(columns, records)
