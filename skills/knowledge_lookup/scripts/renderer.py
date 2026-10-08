"""markdown 表格渲染器。"""

def render_table(columns: list[str], records: list[dict]) -> str:
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
