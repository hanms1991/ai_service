"""read_document 工具：读取用户上传到沙箱的文档并转为 Markdown。

用途：
    汽车研发场景以文档交付为主（相关项定义 docx、需求清单 xlsx、标准 pdf 等）。
    用户先通过 POST /agent/files/upload 上传文件获得 file_id，
    在对话中把 file_id 告诉 Agent；Agent 调用本工具取得文档的 Markdown 文本，
    再依据所加载技能（如 hazard_analysis）完成分析。

支持格式由 core.file_sandbox.ALLOWED_EXTENSIONS 决定：
    docx / doc / xlsx / pptx / pdf / txt / md / csv / json / html

注意：
    - .docx 等基于 markitdown，本地执行，不外发文件；
    - .doc（旧版二进制 OLE2）基于 olefile 直接提取正文文本；
    - 返回文本按 READ_DOC_MAX_CHARS（默认 6 万字符）截断，避免撑爆上下文；
    - 工具不抛异常给 LLM，错误以结构化字符串返回，便于 Agent 自行纠偏。
"""
from __future__ import annotations

import logging
import os
import tempfile

from langchain_core.tools import tool

from core.file_sandbox import (
    SandboxError,
    allowed_extensions,
    get_meta,
    resolve_stored_path,
)

logger = logging.getLogger("tools.read_document")


def _max_chars() -> int:
    return int(os.getenv("READ_DOC_MAX_CHARS", "60000"))


def _doc_to_text(path: str) -> str:
    """从旧版二进制 .doc（OLE2）提取正文文本。

    基于 olefile 读取 WordDocument 流，用 FIB（文件信息块）定位正文：
      - fcMin（偏移 0x18）：正文在 WordDocument 流中的起始位置；
      - ccpText（偏移 0x4C）：正文字符数；
    Word 97+ 的 .doc 正文为 UTF-16LE（2 字节/字符）。若解码异常则回退为
    扫描可打印的 UTF-16LE 文本段，保证尽可能拿到内容。
    """
    import re
    import struct

    import olefile

    ole = olefile.OleFileIO(path)
    try:
        word = ole.openstream("WordDocument").read()
    finally:
        ole.close()

    if len(word) < 0x50:
        return ""

    fc_min = struct.unpack_from("<I", word, 0x18)[0]
    ccp_text = struct.unpack_from("<I", word, 0x4C)[0]

    text = ""
    if fc_min < len(word) and ccp_text > 0:
        # UTF-16LE：2 字节/字符
        end = min(fc_min + ccp_text * 2, len(word))
        raw = word[fc_min:end]
        try:
            text = raw.decode("utf-16-le", errors="ignore")
        except Exception:  # noqa: BLE001
            text = ""

    # 兜底：FIB 取值异常或解码后几乎全是乱码时，扫描可打印 UTF-16LE 文本段
    if not text or sum(c.isprintable() for c in text) < max(len(text) * 0.3, 1):
        # 把整个流按 UTF-16LE 解码，过滤控制字符后取非空行
        try:
            full = word.decode("utf-16-le", errors="ignore")
        except Exception:  # noqa: BLE001
            full = ""
        full = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", full)
        lines = [ln.strip() for ln in full.splitlines()
                 if any(c.isprintable() for c in ln)]
        text = "\n".join(lines)

    # 清理控制字符，保留换行/制表
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _prepare_convert_path(path: str, ext: str) -> tuple[str, str | None]:
    """转换前预处理，返回（实际转换路径, 临时文件路径或 None）。

    docx：先把纵向合并单元格（vMerge）实物化再交给 markitdown。
    markitdown 的 mammoth 后端会丢弃 vMerge 续行单元格，导致续行整体
    左移错列（功能清单类层级表会因此被误读为重复行）。预处理失败时
    软降级为原文件，绝不阻断读取。
    """
    if ext.lower() != ".docx":
        return path, None
    try:
        from core.docx_tables import fill_vertical_merges

        tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
        tmp.close()
        fill_vertical_merges(path, tmp.name)
        return tmp.name, tmp.name
    except Exception as exc:  # noqa: BLE001 —— 预处理是增强而非前提
        logger.warning("[read_document] docx 合并单元格预处理失败，按原文件转换：%s", exc)
        return path, None


@tool("read_document")
def read_document(file_id: str) -> str:
    """读取用户已上传的文档，返回转换后的 Markdown 文本。

    使用前提：用户已通过文件上传接口（POST /agent/files/upload）上传文档，
    并在消息中提供了返回的 file_id（32 位十六进制串）。

    典型用法：
        用户消息："请基于我上传的相关项文档 1f2c... 做 HARA 分析"
        → 先调用 read_document(file_id="1f2c...") 获取文档内容
        → 再调用 load_skill("hazard_analysis") 按技能模板分析

    Args:
        file_id: 文件上传成功后服务端返回的文件 ID。

    Returns:
        文档的 Markdown 文本（超长时截断并标注）；失败时返回以
        "[读取失败]" 开头的说明。
    """
    file_id = (file_id or "").strip()
    if not file_id:
        return "[读取失败] 未提供 file_id。请先上传文件并在消息中给出 file_id。"

    try:
        meta = get_meta(file_id)
        path = resolve_stored_path(file_id)
    except SandboxError as e:
        return (
            f"[读取失败] {e}。请确认 file_id 是否正确、文件是否仍在保留期内。"
        )
    except Exception as e:  # noqa: BLE001 - 工具层兜底，避免异常冒泡打断 Agent 循环
        return f"[读取失败] 文件定位异常：{e}"

    # .doc（旧版二进制）走 olefile 解析，不依赖 markitdown
    if meta.ext.lower() == ".doc":
        try:
            text = _doc_to_text(str(path)).strip()
        except Exception as e:  # noqa: BLE001
            return (
                f"[读取失败] 文件 {meta.original_name!r}（{meta.ext}）解析失败：{e}。"
                "该 .doc 文件可能已损坏或加密；建议另存为 .docx 后重新上传。"
            )
    else:
        # markitdown 为可选重依赖，惰性导入，缺失时给出可操作提示
        try:
            from markitdown import MarkItDown
        except ImportError:
            return (
                "[读取失败] 文档解析依赖 markitdown 未安装，"
                "请执行 pip install 'markitdown[docx,xlsx,pptx,pdf]' 后重启服务。"
            )

        try:
            convert_path, tmp_path = _prepare_convert_path(str(path), meta.ext)
            try:
                result = MarkItDown().convert(convert_path)
                text = (getattr(result, "text_content", None) or "").strip()
            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
        except Exception as e:  # noqa: BLE001
            return (
                f"[读取失败] 文件 {meta.original_name!r}（{meta.ext}）解析失败：{e}。"
                "该文件可能已损坏、加密，或内容为扫描件图片（暂不支持 OCR）。"
            )

    if not text:
        return (
            f"[读取成功但内容为空] 文件 {meta.original_name!r} 未提取到文本，"
            "可能是纯图片/扫描件。"
        )

    limit = _max_chars()
    header = (
        f"# 文档内容：{meta.original_name}\n"
        f"（file_id={file_id}，类型={meta.ext}，大小={meta.size} 字节，"
        f"提取字符数={len(text)}）\n\n---\n\n"
    )
    if len(text) <= limit:
        return header + text

    truncated = text[:limit]
    omitted = len(text) - limit
    return (
        header
        + truncated
        + f"\n\n---\n[系统提示] 文档过长，已截断前 {limit} 字符，"
        f"省略约 {omitted} 字符。如需分析文档其余部分，请缩小范围或分段提供。"
    )


# 工具自描述辅助信息（供能力/文档查询）
SUPPORTED_EXTENSIONS = allowed_extensions()
