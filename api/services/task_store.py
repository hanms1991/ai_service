"""任务状态存储（设计文档 11.2 节）。

一期实现：内存字典 + asyncio.Lock，接口抽象为
  create/get/update/set_result/set_interrupt/fail/timeout/cancel

后续可替换为 Redis/SQL 实现（保持接口签名不变）。

并发：所有写操作持锁；读操作无锁（dict 读写原子性足够）。

TTL：终态任务（completed/failed/timeout/cancelled/rejected）保留 24h，
     启动期后台清理协程每 60s 扫描一次过期任务删除。

局限（设计文档 11.2 明示）：进程崩溃时"运行中"任务丢失，
    已完成结果可凭 checkpointer 恢复快照（不在本类职责）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from api.errors import task_already_finished, task_not_found
from api.schemas import TERMINAL_STATES, TaskStatus

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """UTC 时间（ISO 8601 输出友好，跨时区一致）。"""
    return datetime.now(timezone.utc)


@dataclass
class TaskRecord:
    """任务记录（内部状态，route 层负责转 TaskDetailResponse）。"""
    task_id: str
    thread_id: str
    status: TaskStatus
    trace_id: str = ""
    mode: str | None = None             # skill_direct | orchestrated
    scene: str | None = None
    progress: dict | None = None
    plan: list[dict] | None = None
    output: str | None = None
    structured: dict | list | None = None
    interrupt: dict | None = None       # {"type","step_id","tool","skill","message"}
    error: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    callback_url: str | None = None
    idempotency_key: str | None = None
    timeout_seconds: int | None = None  # 整体任务时限（用于 wait_for）
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    finished_at: datetime | None = None  # 进入终态时间戳，用于 TTL 清理
    # 运行时句柄（不序列化，不暴露给响应）
    async_task: asyncio.Task | None = field(default=None, repr=False, compare=False)
    # 预先解析好的执行参数（避免在 routes 层耦合 agent_runner）
    request_snapshot: dict = field(default_factory=dict, repr=False)

    def to_detail_dict(self) -> dict[str, Any]:
        """转对外响应字典（对齐 TaskDetailResponse 字段，过滤内部字段）。"""
        return {
            "task_id": self.task_id,
            "thread_id": self.thread_id,
            "status": self.status.value,
            "mode": self.mode,
            "scene": self.scene,
            "progress": self.progress,
            "plan": self.plan,
            "output": self.output,
            "structured": self.structured,
            "interrupt": self.interrupt,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "usage": self.usage,
            "trace_id": self.trace_id,
        }


class TaskStore:
    """内存任务存储（单进程，可替换为 Redis）。"""

    def __init__(self, ttl_seconds: int = 24 * 3600, cleaner_interval: int = 60):
        self._tasks: dict[str, TaskRecord] = {}
        # 幂等键索引：idempotency_key -> task_id（仅终态保留期内有效）
        self._idem_index: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._ttl_seconds = ttl_seconds
        self._cleaner_interval = cleaner_interval
        self._cleaner_task: asyncio.Task | None = None

    # ── 基本接口 ──────────────────────────────────────────────

    async def create(self, record: TaskRecord) -> TaskRecord:
        """新建任务记录（pending 状态）。"""
        async with self._lock:
            # 幂等：相同 idempotency_key 已存在则返回旧任务
            if record.idempotency_key:
                existing_id = self._idem_index.get(record.idempotency_key)
                if existing_id and existing_id in self._tasks:
                    return self._tasks[existing_id]
            self._tasks[record.task_id] = record
            if record.idempotency_key:
                self._idem_index[record.idempotency_key] = record.task_id
        logger.debug(
            "[TaskStore] create task=%s thread=%s status=%s",
            record.task_id, record.thread_id, record.status.value,
        )
        return record

    def get(self, task_id: str) -> TaskRecord:
        """同步读（无锁，dict 读原子）。

        不存在直接抛 task_not_found（route 层捕获转 404）。
        """
        rec = self._tasks.get(task_id)
        if rec is None:
            raise task_not_found(task_id)
        return rec

    async def update(
        self,
        task_id: str,
        *,
        status: TaskStatus | None = None,
        progress: dict | None = None,
        plan: list[dict] | None = None,
        output: str | None = None,
        structured: dict | list | None = None,
        interrupt: dict | None = None,
        mode: str | None = None,
        scene: str | None = None,
        usage: dict[str, int] | None = None,
    ) -> TaskRecord:
        """局部更新任务字段，自动刷新 updated_at。"""
        async with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                raise task_not_found(task_id)
            if status is not None and status != rec.status:
                # 终态后不允许更新（防止后台协程误改）
                if rec.status in TERMINAL_STATES:
                    raise task_already_finished(task_id, rec.status.value)
                rec.status = status
                if status in TERMINAL_STATES:
                    rec.finished_at = _utcnow()
            if progress is not None:
                rec.progress = progress
            if plan is not None:
                rec.plan = plan
            if output is not None:
                rec.output = output
            if structured is not None:
                rec.structured = structured
            if interrupt is not None:
                rec.interrupt = interrupt
            if mode is not None:
                rec.mode = mode
            if scene is not None:
                rec.scene = scene
            if usage is not None:
                rec.usage = usage
            rec.updated_at = _utcnow()
            return rec

    async def set_result(
        self,
        task_id: str,
        *,
        output: str | None = None,
        structured: dict | list | None = None,
        plan: list[dict] | None = None,
        usage: dict[str, int] | None = None,
        mode: str | None = None,
    ) -> TaskRecord:
        """成功终态：status=completed，写结果。"""
        async with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                raise task_not_found(task_id)
            rec.status = TaskStatus.COMPLETED
            rec.finished_at = _utcnow()
            rec.updated_at = _utcnow()
            if output is not None:
                rec.output = output
            if structured is not None:
                rec.structured = structured
            if plan is not None:
                rec.plan = plan
            if usage is not None:
                rec.usage = usage
            if mode is not None:
                rec.mode = mode
            # 清除中断字段
            rec.interrupt = None
            rec.error = None
            return rec

    async def set_interrupt(
        self,
        task_id: str,
        interrupt: dict,
    ) -> TaskRecord:
        """人机中断：status=waiting_human，写 interrupt 载荷。"""
        async with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                raise task_not_found(task_id)
            rec.status = TaskStatus.WAITING_HUMAN
            rec.interrupt = interrupt
            rec.updated_at = _utcnow()
            return rec

    async def fail(self, task_id: str, error_message: str) -> TaskRecord:
        """失败终态：status=failed。"""
        async with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                raise task_not_found(task_id)
            rec.status = TaskStatus.FAILED
            rec.error = error_message
            rec.finished_at = _utcnow()
            rec.updated_at = _utcnow()
            rec.interrupt = None
            return rec

    async def mark_timeout(self, task_id: str) -> TaskRecord:
        """超时终态：status=timeout。"""
        async with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                raise task_not_found(task_id)
            rec.status = TaskStatus.TIMEOUT
            rec.error = f"任务整体时限 {rec.timeout_seconds}s 超时"
            rec.finished_at = _utcnow()
            rec.updated_at = _utcnow()
            return rec

    async def cancel(self, task_id: str) -> TaskRecord:
        """取消终态：status=cancelled；尽力取消后台 async_task。

        已是终态返回 task_already_finished（route 层转 409）。
        """
        async with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                raise task_not_found(task_id)
            if rec.status in TERMINAL_STATES:
                raise task_already_finished(task_id, rec.status.value)
            # 取消后台协程（不抛 CancelledError 到上层）
            if rec.async_task is not None and not rec.async_task.done():
                rec.async_task.cancel()
            rec.status = TaskStatus.CANCELLED
            rec.finished_at = _utcnow()
            rec.updated_at = _utcnow()
            return rec

    async def find_by_idempotency_key(self, key: str) -> TaskRecord | None:
        """幂等查询。"""
        async with self._lock:
            task_id = self._idem_index.get(key)
            if task_id is None:
                return None
            return self._tasks.get(task_id)

    # ── 后台清理 ──────────────────────────────────────────────

    async def start_cleanup_loop(self) -> None:
        """启动后台 TTL 清理协程（lifespan 启动时调用）。"""
        if self._cleaner_task is not None and not self._cleaner_task.done():
            return
        self._cleaner_task = asyncio.create_task(self._cleanup_loop())

    async def stop_cleanup_loop(self) -> None:
        """停止清理协程（lifespan 关闭时调用）。"""
        if self._cleaner_task is not None and not self._cleaner_task.done():
            self._cleaner_task.cancel()
            try:
                await self._cleaner_task
            except asyncio.CancelledError:
                pass
            self._cleaner_task = None

    async def _cleanup_loop(self) -> None:
        """周期扫描，删除过期终态任务。"""
        try:
            while True:
                await asyncio.sleep(self._cleaner_interval)
                await self._sweep()
        except asyncio.CancelledError:
            logger.debug("[TaskStore] cleanup loop 已停止")
            raise

    async def _sweep(self) -> None:
        """执行一次扫描清理。"""
        import time
        now_ts = time.time()
        async with self._lock:
            to_delete: list[str] = []
            for task_id, rec in self._tasks.items():
                if rec.status not in TERMINAL_STATES:
                    continue
                if rec.finished_at is None:
                    continue
                age = now_ts - rec.finished_at.timestamp()
                if age > self._ttl_seconds:
                    to_delete.append(task_id)
            for task_id in to_delete:
                rec = self._tasks.pop(task_id, None)
                if rec and rec.idempotency_key:
                    self._idem_index.pop(rec.idempotency_key, None)
            if to_delete:
                logger.info("[TaskStore] TTL 清理 %d 条终态任务", len(to_delete))


# ════════════════════════════════════════════════════════════════
# 单例句柄（lifespan 启动时 set_task_store 注入）
# ════════════════════════════════════════════════════════════════

_store: TaskStore | None = None


def set_task_store(store: TaskStore) -> None:
    global _store
    _store = store


def get_task_store() -> TaskStore:
    if _store is None:
        raise RuntimeError("TaskStore 尚未初始化，请检查 lifespan 是否已启动")
    return _store
