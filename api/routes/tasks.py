"""POST /agent/tasks 异步任务端点（M2 实现）。

设计文档 7.2-7.5 节：
  - POST   /agent/tasks                提交异步任务（202）
  - GET    /agent/tasks/{task_id}      查询任务状态与结果（200）
  - POST   /agent/tasks/{task_id}/resume   恢复 ask/confirm 中断（200）
  - POST   /agent/tasks/{task_id}/cancel   取消任务（200）

任务状态机（设计文档第 8 节）：
  pending → planning → running → completed/waiting_human/failed/timeout/cancelled
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from api.deps import get_trace_id, require_api_key
from api.schemas import (
    AgentRequest,
    CancelResponse,
    ResumeRequest,
    SubmitTaskResponse,
    TaskDetailResponse,
    TaskStatus,
)
from api.services import agent_runner
from api.services.task_store import get_task_store

router = APIRouter(prefix="/agent", tags=["tasks"])


@router.post("/tasks", response_model=SubmitTaskResponse, status_code=202)
async def submit_task(
    req: AgentRequest,
    request: Request,
    _api_key: str = Depends(require_api_key),
) -> SubmitTaskResponse:
    """提交异步任务：202 立即返回 task_id，后台执行。

    校验失败（scene 不存在、必填输入缺失、reference_data 超限等）返回 4xx，
    不消耗 LLM。LLM 调用全部在后台 _run_task_async 内进行。
    """
    trace_id = req.trace_id or get_trace_id(request)
    request.state.trace_id = trace_id

    record = await agent_runner.submit_task(req, trace_id)
    return SubmitTaskResponse(
        task_id=record.task_id,
        thread_id=record.thread_id,
        status=record.status,
    )


@router.get("/tasks/{task_id}", response_model=TaskDetailResponse)
async def get_task(
    task_id: str,
    request: Request,
    _api_key: str = Depends(require_api_key),
) -> TaskDetailResponse:
    """查询任务状态与结果（设计文档 7.3）。

    不存在抛 task_not_found（route 层无显式处理，由 main.py 异常处理器转 404）。
    """
    store = get_task_store()
    rec = store.get(task_id)  # 抛 task_not_found
    return TaskDetailResponse(**rec.to_detail_dict())


@router.post("/tasks/{task_id}/resume", response_model=TaskDetailResponse)
async def resume_task(
    task_id: str,
    body: ResumeRequest,
    request: Request,
    _api_key: str = Depends(require_api_key),
) -> TaskDetailResponse:
    """恢复 ask/confirm 中断（设计文档 7.4）。

    仅 status=waiting_human 的任务可 resume；终态抛 task_already_finished（409）。
    底层映射 graph.ainvoke(Command(resume=reply), config=...) 续跑。
    """
    # 恢复任务的 trace_id 沿用原任务，便于全链路追踪
    store = get_task_store()
    rec = store.get(task_id)
    request.state.trace_id = rec.trace_id

    updated = await agent_runner.resume_task(task_id, body.reply)
    return TaskDetailResponse(**updated.to_detail_dict())


@router.post("/tasks/{task_id}/cancel", response_model=CancelResponse)
async def cancel_task(
    task_id: str,
    request: Request,
    _api_key: str = Depends(require_api_key),
) -> CancelResponse:
    """取消任务（设计文档 7.5）。

    - pending/planning/running/waiting_human → cancelled
    - 已是终态 → task_already_finished（409）
    取消语义：尽力取消后台 LLM 调用（asyncio.Task.cancel），不回滚已写数据。
    """
    store = get_task_store()
    rec = store.get(task_id)  # 抛 task_not_found
    request.state.trace_id = rec.trace_id

    updated = await store.cancel(task_id)
    # 触发回调（cancelled 也是终态）
    from api.services.callbacks import get_callback_client
    await get_callback_client().notify_if_needed(updated)

    return CancelResponse(
        task_id=updated.task_id,
        status=updated.status,
        message=f"任务已取消（原状态：{rec.status.value}）",
    )
