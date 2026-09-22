"""上传文件沙箱：集中管理用户上传文档的存储、校验与定位。

安全设计：
- 所有文件落在 UPLOAD_DIR（默认 data/uploads）下，按 file_id 分子目录隔离；
- file_id 由服务端生成（uuid4 hex），不接受用户传入路径，从根本上杜绝路径穿越；
- 扩展名白名单 + 单文件大小上限（均可经环境变量配置）；
- 显式拒绝 Office 宏格式（.docm/.xlsm/.pptm）与旧版二进制格式（.doc/.xls/.ppt）。

存储布局：
    <UPLOAD_DIR>/<file_id>/
        ├── file<ext>      # 原始字节（扩展名保留，供 markitdown 嗅探类型）
        └── meta.json      # 原始文件名 / 大小 / content_type / 上传时间

环境变量：
    UPLOAD_DIR       上传根目录（默认 <项目根>/data/uploads）
    MAX_UPLOAD_MB    单文件大小上限，MB（默认 20）
"""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 允许上传/读取的文档扩展名（小写、含点）
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({
    ".docx", ".xlsx", ".pptx", ".pdf",
    ".txt", ".md", ".csv", ".json", ".html", ".htm",
})

# 显式拒绝（给出明确报错，而不是落到"不支持的扩展名"）：
# - 宏格式：可能携带恶意 VBA
# - 旧版二进制 Office：markitdown 不支持，避免上传后无法读取
_BLOCKED_EXTENSIONS: frozenset[str] = frozenset({
    ".docm", ".xlsm", ".pptm", ".doc", ".xls", ".ppt",
    ".exe", ".dll", ".bat", ".cmd", ".ps1", ".sh", ".msi",
})

# file_id 仅接受服务端生成的 32 位十六进制
_FILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_META_NAME = "meta.json"


class SandboxError(Exception):
    """沙箱操作异常基类（routes 层翻译为 ApiError）。"""


class FileSizeExceeded(SandboxError):
    def __init__(self, size: int, limit: int):
        self.size = size
        self.limit = limit
        super().__init__(
            f"文件大小 {size} 字节，超过上限 {limit // (1024 * 1024)}MB"
        )


class FileTypeNotAllowed(SandboxError):
    def __init__(self, ext: str):
        self.ext = ext
        super().__init__(f"不支持的文件类型：{ext or '（无扩展名）'}")


class UploadedFileNotFound(SandboxError):
    def __init__(self, file_id: str):
        self.file_id = file_id
        super().__init__(f"文件 {file_id!r} 不存在或已被清理")


@dataclass
class FileMeta:
    """已上传文件/生成交付物的元数据。"""
    file_id: str
    original_name: str
    stored_name: str
    ext: str
    size: int
    content_type: str
    uploaded_at: str
    # upload=用户上传的输入文档；artifact=技能执行链生成的交付物（xlsx/docx…）
    kind: str = "upload"

    def to_jsonable(self) -> dict:
        return asdict(self)


# ────────────────────────────────────────────────────────────────────
# 配置
# ────────────────────────────────────────────────────────────────────

def upload_root() -> Path:
    """上传根目录（启动时与每次调用时都确保存在）。"""
    root = Path(os.getenv("UPLOAD_DIR", str(PROJECT_ROOT / "data" / "uploads")))
    root.mkdir(parents=True, exist_ok=True)
    return root


def max_upload_bytes() -> int:
    """单文件大小上限（字节）。"""
    return int(float(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024)


def allowed_extensions() -> list[str]:
    """允许的扩展名列表（供错误提示与文档化使用）。"""
    return sorted(ALLOWED_EXTENSIONS)


# ────────────────────────────────────────────────────────────────────
# 内部工具
# ────────────────────────────────────────────────────────────────────

def _split_ext(filename: str) -> str:
    """取小写扩展名（含点）；无扩展名返回空串。"""
    return Path(filename).suffix.lower()


def _safe_original_name(filename: str) -> str:
    """剥离路径成分，仅保留文件名本身（防 a/b/../../x.docx）。"""
    name = Path(filename).name.strip()
    # Windows 非法文件名字符与控制字符统一替换
    for ch in '<>:"|?*\x00':
        name = name.replace(ch, "_")
    return name


def _file_dir(file_id: str) -> Path:
    """按 file_id 定位子目录，并校验 file_id 形态 + 根目录归属。"""
    if not _FILE_ID_RE.match(file_id or ""):
        raise UploadedFileNotFound(file_id or "")
    root = upload_root().resolve()
    target = (root / file_id).resolve()
    # 双保险：解析后必须仍在根目录内
    if root not in target.parents and target != root:
        raise UploadedFileNotFound(file_id)
    return target


# ────────────────────────────────────────────────────────────────────
# 对外 API
# ────────────────────────────────────────────────────────────────────

def validate_upload(filename: str, size: int) -> str:
    """校验文件名与大小，返回规范化的小写扩展名；不通过抛 SandboxError。"""
    ext = _split_ext(filename)
    if ext in _BLOCKED_EXTENSIONS:
        raise FileTypeNotAllowed(ext)
    if ext not in ALLOWED_EXTENSIONS:
        raise FileTypeNotAllowed(ext)
    limit = max_upload_bytes()
    if size > limit:
        raise FileSizeExceeded(size, limit)
    return ext


def save_upload(
    filename: str,
    content: bytes,
    content_type: str = "",
) -> FileMeta:
    """落盘一个上传文件，返回 FileMeta。

    - file_id 服务端生成；
    - 内容以 file<ext> 固定名落盘，原始文件名只存 meta.json。
    """
    original_name = _safe_original_name(filename)
    ext = validate_upload(original_name, len(content))

    file_id = uuid.uuid4().hex
    file_dir = upload_root() / file_id
    file_dir.mkdir(parents=True, exist_ok=False)

    stored_name = f"file{ext}"
    meta = FileMeta(
        file_id=file_id,
        original_name=original_name,
        stored_name=stored_name,
        ext=ext,
        size=len(content),
        content_type=(content_type or "").split(";")[0].strip(),
        uploaded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    try:
        (file_dir / stored_name).write_bytes(content)
        (file_dir / _META_NAME).write_text(
            json.dumps(meta.to_jsonable(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        # 落盘失败要清理半成品目录，避免孤儿
        shutil.rmtree(file_dir, ignore_errors=True)
        raise
    return meta


# 交付物允许的扩展名（平台渲染器产出，受控来源，单独白名单）
ALLOWED_ARTIFACT_EXTENSIONS: frozenset[str] = frozenset({
    ".xlsx", ".docx", ".pdf", ".csv", ".md", ".json", ".txt",
})

_ARTIFACT_CONTENT_TYPES: dict[str, str] = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".json": "application/json",
    ".txt": "text/plain",
}


def save_generated(filename: str, content: bytes) -> FileMeta:
    """落盘一个平台生成的交付物（xlsx/docx 等），返回 FileMeta(kind='artifact')。

    与 save_upload 的区别：
    - 文件名由技能渲染器模板生成（仍经 _safe_original_name 净化）；
    - 扩展名走交付物白名单；不做上传大小上限校验（产出受控）。
    """
    original_name = _safe_original_name(filename)
    ext = _split_ext(original_name)
    if ext not in ALLOWED_ARTIFACT_EXTENSIONS:
        raise FileTypeNotAllowed(ext)

    file_id = uuid.uuid4().hex
    file_dir = upload_root() / file_id
    file_dir.mkdir(parents=True, exist_ok=False)

    stored_name = f"artifact{ext}"
    meta = FileMeta(
        file_id=file_id,
        original_name=original_name,
        stored_name=stored_name,
        ext=ext,
        size=len(content),
        content_type=_ARTIFACT_CONTENT_TYPES.get(ext, "application/octet-stream"),
        uploaded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        kind="artifact",
    )
    try:
        (file_dir / stored_name).write_bytes(content)
        (file_dir / _META_NAME).write_text(
            json.dumps(meta.to_jsonable(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(file_dir, ignore_errors=True)
        raise
    return meta


def get_meta(file_id: str) -> FileMeta:
    """读取文件元数据（上传文档或生成交付物）；不存在抛 UploadedFileNotFound。"""
    file_dir = _file_dir(file_id)
    meta_path = file_dir / _META_NAME
    if not meta_path.is_file():
        raise UploadedFileNotFound(file_id)
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    return FileMeta(**data)


def resolve_stored_path(file_id: str) -> Path:
    """定位沙箱内实际文件（供 read_document 等本地工具使用）。"""
    meta = get_meta(file_id)
    path = _file_dir(file_id) / meta.stored_name
    if not path.is_file():
        raise UploadedFileNotFound(file_id)
    return path


def list_files(limit: int = 100) -> list[FileMeta]:
    """列出最近上传的文件（按上传时间倒序）。"""
    root = upload_root()
    metas: list[FileMeta] = []
    for meta_path in root.glob(f"*/{_META_NAME}"):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            metas.append(FileMeta(**data))
        except Exception:
            continue
    metas.sort(key=lambda m: m.uploaded_at, reverse=True)
    return metas[:limit]


def delete_file(file_id: str) -> None:
    """删除文件及其目录；不存在抛 UploadedFileNotFound。"""
    file_dir = _file_dir(file_id)
    if not file_dir.is_dir():
        raise UploadedFileNotFound(file_id)
    shutil.rmtree(file_dir, ignore_errors=True)
