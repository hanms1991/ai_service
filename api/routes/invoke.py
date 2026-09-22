"""POST /agent/invoke 同步执行端点。

- POST /agent/invoke 同步执行端点。
- POST /agent/invoke/stream 流式执行端点。

设计文档 7.1 节：同步执行并返回最终结果，适用于快任务。
M1 范围：scene 直达 + 智能编排 + reference_data 注入（50KB 阈值报错）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from api.deps import get_trace_id, require_api_key
from api.errors import ApiError
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


@router.post("/invoke/stream")
async def invoke_stream(
    req: AgentRequest,
    request: Request,
    _api_key: str = Depends(require_api_key),
) -> StreamingResponse:
    """流式执行：通过 NDJSON 实时推送 LLM 输出 token。

    响应格式：text/event-stream（NDJSON，每行一个 JSON 对象）
    事件类型：
      {"type":"token","content":"..."}      LLM 输出的文本片段
      {"type":"done","output":"...",...}    执行完成，附带完整结果
      {"type":"error","code":"...","message":"..."}  执行出错

    前端示例（fetch + ReadableStream）：
      const resp = await fetch('/agent/invoke/stream', {
        method: 'POST',
        headers: {'Content-Type':'application/json','X-API-Key':'xxx'},
        body: JSON.stringify({message:'你好'})
      });
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream:true});
        const lines = buffer.split('\\n');
        buffer = lines.pop();
        for (const line of lines) {
          if (!line.trim()) continue;
          const evt = JSON.parse(line);
          if (evt.type === 'token') appendText(evt.content);
          if (evt.type === 'done') console.log('完成:', evt.output);
          if (evt.type === 'error') console.error(evt);
        }
      }
    """
    trace_id = req.trace_id or get_trace_id(request)
    request.state.trace_id = trace_id

    async def event_generator():
        try:
            async for chunk in agent_runner.run_invoke_stream(
                message=req.message,
                scene=req.scene,
                inputs=req.inputs,
                thread_id=req.thread_id,
                context=req.context,
                reference_data=req.reference_data,
                response_format=req.response_format,
                timeout_seconds=req.timeout_seconds,
                trace_id=trace_id,
            ):
                yield chunk
        except ApiError as e:
            yield (
                f'{{"type":"error","code":"{e.code}","message":{_json_str(e.message)}}}\n'
            )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 Nginx 缓冲
        },
    )


def _json_str(s: str) -> str:
    """将字符串安全序列化为 JSON 字符串字面量（含引号）。"""
    import json as _json
    return _json.dumps(s, ensure_ascii=False)
