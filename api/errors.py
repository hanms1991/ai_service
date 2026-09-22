"""统一错误码与异常。

设计文档第 14 节错误码表：所有错误统一形如
  { "error": { "code": "...", "message": "...", "task_id": "...", "trace_id": "..." } }

HTTP 状态码 → code 映射：
  401 UNAUTHORIZED / 403 FORBIDDEN / 400 REQUEST_INVALID 等
"""
from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """对外 API 统一异常。

    由 routes 层主动抛出，由 main.py 的全局异常处理器归一化为响应体。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 400,
        task_id: str | None = None,
        trace_id: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        self.code = code
        self.message = message
        self.http_status = http_status
        self.task_id = task_id
        self.trace_id = trace_id
        self.details = details or {}
        super().__init__(f"[{code}] {message}")


# ── 预定义错误工厂（routes 层直接调用，避免散落字符串） ──

def unauthorized(message: str = "缺少或无效的 API Key") -> ApiError:
    return ApiError("UNAUTHORIZED", message, http_status=401)


def forbidden(message: str = "无权访问") -> ApiError:
    return ApiError("FORBIDDEN", message, http_status=403)


def request_invalid(message: str) -> ApiError:
    return ApiError("REQUEST_INVALID", message, http_status=400)


def scene_not_found(scene: str) -> ApiError:
    return ApiError(
        "SCENE_NOT_FOUND",
        f"场景码 {scene!r} 不存在于 scenes.yaml",
        http_status=400,
    )


def skill_not_found(skill: str, agent: str | None = None) -> ApiError:
    extra = f"（agent={agent}）" if agent else ""
    return ApiError(
        "SKILL_NOT_FOUND",
        f"技能 {skill!r} 未在注册表绑定{extra}",
        http_status=400,
    )


def agent_not_found(agent: str) -> ApiError:
    return ApiError(
        "AGENT_NOT_FOUND",
        f"Agent {agent!r} 不在注册表",
        http_status=400,
    )


def skill_input_missing(missing: list[str], skill: str) -> ApiError:
    return ApiError(
        "SKILL_INPUT_MISSING",
        f"技能 {skill} 缺少必填输入：{', '.join(missing)}",
        http_status=400,
    )


def reference_data_too_large(current_bytes: int, limit_bytes: int) -> ApiError:
    return ApiError(
        "REFERENCE_DATA_TOO_LARGE",
        f"reference_data 当前 {current_bytes} 字节，超过 {limit_bytes // 1024}KB 硬阈值",
        http_status=413,
    )


def output_format_conflict(scene: str, expected: str, got: str) -> ApiError:
    return ApiError(
        "OUTPUT_FORMAT_CONFLICT",
        f"场景 {scene} 要求 response_format={expected}，请求传了 {got}",
        http_status=409,
    )


def skill_output_invalid(message: str) -> ApiError:
    return ApiError("SKILL_OUTPUT_INVALID", message, http_status=422)


def llm_upstream_error(message: str) -> ApiError:
    return ApiError("LLM_UPSTREAM_ERROR", message, http_status=502)


def invoke_timeout(task_id: str, timeout_seconds: int) -> ApiError:
    return ApiError(
        "INVOKE_TIMEOUT",
        f"同步执行超过 {timeout_seconds}s，已转异步任务 {task_id}，"
        f"可用 GET /agent/tasks/{task_id} 继续跟踪",
        http_status=408,
        task_id=task_id,
    )


def internal_error(message: str = "服务内部错误") -> ApiError:
    return ApiError("INTERNAL_ERROR", message, http_status=500)


# ── M2 新增：任务相关错误码（设计文档第 14 节） ──

def task_not_found(task_id: str) -> ApiError:
    return ApiError(
        "TASK_NOT_FOUND",
        f"任务 {task_id!r} 不存在或已过期清理",
        http_status=404,
        task_id=task_id,
    )


def thread_not_found(thread_id: str) -> ApiError:
    return ApiError(
        "THREAD_NOT_FOUND",
        f"线程 {thread_id!r} 不存在",
        http_status=404,
    )


def task_already_finished(task_id: str, current_status: str) -> ApiError:
    return ApiError(
        "TASK_ALREADY_FINISHED",
        f"任务 {task_id} 已是终态（{current_status}），不可 resume/cancel",
        http_status=409,
        task_id=task_id,
        details={"current_status": current_status},
    )


def rate_limited(retry_after: int | None = None) -> ApiError:
    details = {"retry_after_seconds": retry_after} if retry_after else None
    return ApiError(
        "RATE_LIMITED",
        f"请求过于频繁，触发限流{f'，建议 {retry_after}s 后重试' if retry_after else ''}",
        http_status=429,
        details=details,
    )


def not_implemented(feature: str) -> ApiError:
    return ApiError(
        "NOT_IMPLEMENTED",
        f"{feature} 暂未实现",
        http_status=501,
    )


def task_timeout(task_id: str, timeout_seconds: int) -> ApiError:
    """异步任务整体超时（任务状态机置 timeout）。"""
    return ApiError(
        "TASK_TIMEOUT",
        f"异步任务 {task_id} 超过 {timeout_seconds}s 整体时限",
        http_status=504,
        task_id=task_id,
    )


# ── 文件上传（文档类技能输入） ──

def file_too_large(current_bytes: int, limit_bytes: int) -> ApiError:
    return ApiError(
        "FILE_TOO_LARGE",
        f"文件大小 {current_bytes} 字节，超过上限 {limit_bytes // (1024 * 1024)}MB",
        http_status=413,
    )


def file_type_not_allowed(ext: str, allowed: list[str]) -> ApiError:
    return ApiError(
        "FILE_TYPE_NOT_ALLOWED",
        f"不支持的文件类型：{ext or '（无扩展名）'}；允许：{', '.join(allowed)}",
        http_status=415,
    )


def file_not_found(file_id: str) -> ApiError:
    return ApiError(
        "FILE_NOT_FOUND",
        f"文件 {file_id!r} 不存在或已被清理",
        http_status=404,
    )
