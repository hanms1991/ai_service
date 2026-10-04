"""uc_generate 技能的 prepare 钩子：向天枢后端查询 UC 模板并注入 prompt。

引擎约定入口：prepare(ctx) -> dict
返回键 doc_supplement 会被引擎紧随参考数据段注入用户消息。

拉取失败时 tianshu_client 内部已降级为内置默认模板，此处不做额外处理。
"""
from __future__ import annotations

from typing import Any


def prepare(ctx: Any) -> dict:
    """查询 UC 模板，返回注入 prompt 的补充段落。"""
    from core.tianshu_client import fetch_uc_template, format_uc_template_block

    reference_data = getattr(ctx, "reference_data", None)
    template = fetch_uc_template(reference_data)
    block = format_uc_template_block(template)
    return {"doc_supplement": block}
