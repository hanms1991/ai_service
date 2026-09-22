"""文件上传/下载端点（文档类技能的输入通道）。

端点：
    POST   /agent/files/upload            上传文档（multipart/form-data）
    GET    /agent/files                   列出已上传文件
    GET    /agent/files/{file_id}         查询文件元数据
    GET    /agent/files/{file_id}/download 下载原始文件
    DELETE /agent/files/{file_id}         删除文件

典型链路：
    1. 调用方 POST upload 拿到 file_id；
    2. 在 /agent/invoke 或 /agent/tasks 的 message 中带上 file_id；
    3. Agent 通过 read_document 工具读取文档内容。

所有端点均需 API Key 鉴权；文件统一落在 core.file_sandbox 管理的沙箱目录。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import FileResponse

from api.deps import require_api_key
from api.errors import file_not_found, file_too_large, file_type_not_allowed
from core.file_sandbox import (
    FileSizeExceeded,
    FileTypeNotAllowed,
    UploadedFileNotFound,
    allowed_extensions,
    delete_file,
    get_meta,
    list_files,
    max_upload_bytes,
    resolve_stored_path,
    save_upload,
)

router = APIRouter(prefix="/agent/files", tags=["files"])


def _file_info(meta) -> dict:
    """FileMeta → 对外 JSON。"""
    return meta.to_jsonable()


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(..., description="上传的文档文件"),
    _api_key: str = Depends(require_api_key),
) -> dict:
    """上传单个文档，返回 file_id 供后续对话引用。

    限制：扩展名白名单（docx/xlsx/pptx/pdf/txt/md/csv/json/html），
    单文件大小受 MAX_UPLOAD_MB 限制（默认 20MB）。
    """
    content = await file.read()
    try:
        meta = save_upload(
            filename=file.filename or "",
            content=content,
            content_type=file.content_type or "",
        )
    except FileSizeExceeded as e:
        raise file_too_large(e.size, e.limit)
    except FileTypeNotAllowed as e:
        raise file_type_not_allowed(e.ext, allowed_extensions())

    return {"file": _file_info(meta)}


@router.get("")
async def get_files(
    _api_key: str = Depends(require_api_key),
) -> dict:
    """列出已上传文件（按上传时间倒序，最多 100 个）。"""
    metas = list_files()
    return {
        "files": [_file_info(m) for m in metas],
        "limits": {
            "max_upload_mb": max_upload_bytes() // (1024 * 1024),
            "allowed_extensions": allowed_extensions(),
        },
    }


@router.get("/{file_id}")
async def get_file_meta(
    file_id: str,
    _api_key: str = Depends(require_api_key),
) -> dict:
    """查询单个文件的元数据。"""
    try:
        meta = get_meta(file_id)
    except UploadedFileNotFound:
        raise file_not_found(file_id)
    return {"file": _file_info(meta)}


@router.get("/{file_id}/download")
async def download_file(
    file_id: str,
    _api_key: str = Depends(require_api_key),
) -> FileResponse:
    """下载文件原始字节（Content-Disposition 使用上传时的原始文件名）。"""
    try:
        meta = get_meta(file_id)
        path = resolve_stored_path(file_id)
    except UploadedFileNotFound:
        raise file_not_found(file_id)
    return FileResponse(
        path=str(path),
        media_type=meta.content_type or "application/octet-stream",
        filename=meta.original_name,
    )


@router.delete("/{file_id}")
async def remove_file(
    file_id: str,
    _api_key: str = Depends(require_api_key),
) -> dict:
    """删除已上传的文件。"""
    try:
        delete_file(file_id)
    except UploadedFileNotFound:
        raise file_not_found(file_id)
    return {"deleted": True, "file_id": file_id}
