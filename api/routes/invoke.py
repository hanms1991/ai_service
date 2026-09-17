"""POST /agent/invoke 同步执行端点。

设计文档 7.1 节：同步执行并返回最终结果，适用于快任务。
M1 范围：scene 直达 + 智能编排 + reference_data 注入（50KB 阈值报错）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from api.deps import get_trace_id, require_api_key
from api.schemas import AgentRequest, InvokeResponse
from api.services import agent_runner

router = APIRouter(prefix="/agent", tags=["invoke"])


@router.post("/invoke", response_model=InvokeResponse)
async def invoke(
    req: AgentRequest,
    request: Request,
    _api_key: str = Depends(require_api_key),
) -> InvokeResponse:
    """同步执行：scene 直达（execute_skill_v2）或智能编排（Planner）。"""
    trace_id = req.trace_id or get_trace_id(request)
    # 同步到 request.state，使错误处理器（api_error_handler）能拿到请求体里的 trace_id
    # 否则 agent_runner 抛 ApiError 时，handler 只能看到中间件生成的 trace_id
    request.state.trace_id = trace_id

    return await agent_runner.run_invoke(
        message=req.message,
        scene=req.scene,
        inputs=req.inputs,
        thread_id=req.thread_id,
        context=req.context,
        reference_data=req.reference_data,
        response_format=req.response_format,
        timeout_seconds=req.timeout_seconds,
        trace_id=trace_id,
    )
