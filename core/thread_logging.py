"""按 trace_id 分文件的日志管理。

每个请求的 Agent 执行日志 + LLM 交互 + 异常堆栈写入
``logs/threads/{trace_id}.log``，与全局 ``kb_retrieval.log`` / ``skill_errors.log`` 并存。

用法（在 agent_runner 请求入口）::

    from core.thread_logging import setup, teardown
    log_path = setup(trace_id)
    try:
        ...  # 执行技能
    finally:
        teardown(trace_id)

启动时调 ``cleanup_old()`` 清理过期文件。
"""
from __future__ import annotations

import logging
import shutil
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
THREAD_LOG_DIR = _PROJECT_ROOT / "logs" / "threads"
DEFAULT_RETENTION_DAYS = 3

# 需要注入 per-thread FileHandler 的 logger 名称（含子 logger 自动继承）
_TARGET_LOGGERS = ("agents", "skills", "core.kb_client")

# trace_id → 已挂载的 handler 列表（teardown 时移除）
_active_handlers: dict[str, logging.FileHandler] = {}


def setup(trace_id: str) -> Path:
    """创建 per-trace_id 日志文件，挂载 FileHandler 到目标 loggers。

    返回日志文件路径。同 trace_id 重复调用时先 teardown 旧 handler 再重建。
    """
    if trace_id in _active_handlers:
        teardown(trace_id)

    THREAD_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = THREAD_LOG_DIR / f"{trace_id}.log"

    # 写入请求头
    with log_path.open("w", encoding="utf-8") as f:
        f.write(
            f"[#trace_id={trace_id} "
            f"started={datetime.now().isoformat(timespec='seconds')}]\n\n"
        )

    handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    for name in _TARGET_LOGGERS:
        logging.getLogger(name).addHandler(handler)
    _active_handlers[trace_id] = handler
    return log_path


def teardown(trace_id: str) -> None:
    """移除 per-trace_id FileHandler（文件保留不删）。"""
    handler = _active_handlers.pop(trace_id, None)
    if handler is None:
        return
    for name in _TARGET_LOGGERS:
        logging.getLogger(name).removeHandler(handler)
    handler.close()


def get_log_path(trace_id: str) -> Path:
    """返回 per-trace_id 日志文件路径（不创建）。"""
    return THREAD_LOG_DIR / f"{trace_id}.log"


def write_to_thread_log(trace_id: str, text: str) -> None:
    """直接追加文本到 per-trace_id 日志文件（不走 logging 模块）。

    供 executor 的 ``_log_skill_error`` 等不便通过 logger 输出的场景使用。
    """
    if not trace_id:
        return
    log_path = THREAD_LOG_DIR / f"{trace_id}.log"
    if not log_path.exists():
        return
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


def cleanup_old(retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """删除超过保留期的 thread 日志文件，返回删除数。"""
    if not THREAD_LOG_DIR.exists():
        return 0
    cutoff_ts = (datetime.now() - timedelta(days=retention_days)).timestamp()
    count = 0
    for entry in THREAD_LOG_DIR.iterdir():
        try:
            if entry.stat().st_mtime < cutoff_ts:
                if entry.is_file():
                    entry.unlink()
                elif entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                count += 1
        except OSError:
            continue
    return count


@contextmanager
def thread_log_scope(trace_id: str):
    """上下文管理器：setup → yield → teardown。

    用法::

        with thread_log_scope(trace_id):
            logger = _make_llm_logger(trace_id)
            ...  # 执行技能 / 图调用
    """
    setup(trace_id)
    try:
        yield
    finally:
        teardown(trace_id)
