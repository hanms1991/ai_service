"""异步任务 API 端到端测试脚本。

链路：
    POST /api/v1/agent/tasks   → 202 + task_id（立即返回，不阻塞）
    GET  /api/v1/agent/tasks/{task_id}  → 轮询状态直到终态
    POST /api/v1/agent/tasks/{task_id}/cancel   → Ctrl+C 主动取消
    POST /api/v1/agent/tasks/{task_id}/resume    → waiting_human 时人工续跑

适用场景：
    PRD 生成等长任务（同步 invoke 会受 600s 超时和客户端 socket 超时双重夹击，
    走异步路径立即返回 202，后台跑完落 completed，彻底绕开客户端超时坑）。

用法示例：
    # 1. PRD 生成（长任务）
    python test/test_api_async.py --scene PRD_GENERATE --feature "位置灯"

    # 2. 一键扩写（短任务，验证快速完成）
    python test/test_api_async.py --scene ONE_CLICK_EXPAND --feature "AEB自动紧急制动"

    # 3. 智能编排（不指定 scene，让 Planner 拆步骤）
    python test/test_api_async.py --message "介绍一下ACC功能"

    # 4. 自定义 inputs（覆盖 --feature 单参数）
    python test/test_api_async.py --scene PRD_GENERATE --inputs "{\"feature_name\":\"位置灯\",\"project\":\"EVX-2025\",\"version\":\"V1.0\"}"

    # 5. 带回调（域名必须在服务端白名单内）
    python test/test_api_async.py --scene PRD_GENERATE --feature "位置灯" --callback-url "http://127.0.0.1:9000/ai-callback"

    # 6. 幂等键（重复提交返回同一 task_id）
    python test/test_api_async.py --scene PRD_GENERATE --feature "位置灯" --idempotency-key "req-2026-001"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Windows 控制台默认 GBK，统一 UTF-8 以正常显示中文输出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ── 默认配置 ──
DEFAULT_BASE_URL = "http://127.0.0.1:8300"
DEFAULT_API_KEY = "dev-local-key-001"
DEFAULT_POLL_INTERVAL = 3.0       # 轮询间隔（秒）
DEFAULT_POLL_TIMEOUT = 1200       # 轮询整体上限（秒，20 分钟）

# 终态集合（与 api/schemas.py 的 TERMINAL_STATES 对齐）
TERMINAL_STATES = {"completed", "failed", "timeout", "cancelled", "rejected"}

# 需要 inputs.feature_name 的场景
SCENES_REQUIRING_FEATURE = {"PRD_GENERATE", "ONE_CLICK_EXPAND"}


# ════════════════════════════════════════════════════════════════
# HTTP 工具
# ════════════════════════════════════════════════════════════════

def _make_request(
    url: str,
    method: str,
    api_key: str,
    body: dict | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict]:
    """发起 HTTP 请求，返回 (status_code, parsed_json)。

    错误响应也尝试按 JSON 解析（设计文档统一错误体），失败则回退 {"raw": text}。
    """
    data = None
    headers = {"X-API-Key": api_key}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        # 4xx/5xx 走这里：HTTPError 也带响应体
        status = e.code
        raw = e.read().decode("utf-8")
    except urllib.error.URLError as e:
        raise RuntimeError(f"连接服务失败：{e}") from e

    try:
        return status, json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return status, {"raw": raw}


def submit_task(
    base_url: str,
    api_key: str,
    payload: dict,
    timeout: float = 30.0,
) -> tuple[str, str, str]:
    """提交异步任务。返回 (task_id, thread_id, initial_status)。"""
    url = f"{base_url}/api/v1/agent/tasks"
    status, data = _make_request(url, "POST", api_key, body=payload, timeout=timeout)
    if status != 202:
        raise RuntimeError(
            f"提交任务失败：HTTP {status}\n"
            f"{json.dumps(data, ensure_ascii=False, indent=2)}"
        )
    return data["task_id"], data["thread_id"], data["status"]


def get_task(base_url: str, api_key: str, task_id: str, timeout: float = 30.0) -> dict:
    """查询任务详情。"""
    url = f"{base_url}/api/v1/agent/tasks/{task_id}"
    status, data = _make_request(url, "GET", api_key, timeout=timeout)
    if status != 200:
        raise RuntimeError(
            f"查询任务失败：HTTP {status}\n"
            f"{json.dumps(data, ensure_ascii=False, indent=2)}"
        )
    return data


def cancel_task(base_url: str, api_key: str, task_id: str, timeout: float = 30.0) -> tuple[int, dict]:
    """取消任务。返回 (status, response_json)。"""
    url = f"{base_url}/api/v1/agent/tasks/{task_id}/cancel"
    return _make_request(url, "POST", api_key, timeout=timeout)


def resume_task(
    base_url: str,
    api_key: str,
    task_id: str,
    reply: str,
    timeout: float = 30.0,
) -> tuple[int, dict]:
    """续跑任务（waiting_human → running）。"""
    url = f"{base_url}/api/v1/agent/tasks/{task_id}/resume"
    return _make_request(url, "POST", api_key, body={"reply": reply}, timeout=timeout)


# ════════════════════════════════════════════════════════════════
# 展示与轮询
# ════════════════════════════════════════════════════════════════

def print_task_detail(task: dict) -> None:
    """按状态分支打印任务详情。"""
    status = task["status"]
    print(f"\n[task {task['task_id']}] status={status} mode={task.get('mode') or '-'}")

    if task.get("progress"):
        p = task["progress"]
        print(
            f"  progress: step {p.get('step_index')}/{p.get('step_total')}, "
            f"current_skill={p.get('current_skill') or '-'}"
        )

    if task.get("plan"):
        print(f"  plan ({len(task['plan'])} 步):")
        for i, step in enumerate(task["plan"]):
            print(
                f"    {i+1}. tool={step.get('tool') or '-'} "
                f"skill={step.get('skill') or '-'} mode={step.get('mode')} "
                f"is_final={step.get('is_final')} output_key={step.get('output_key')}"
            )

    if task.get("interrupt"):
        it = task["interrupt"]
        print(f"  [interrupt] type={it.get('type')}")
        if it.get("message"):
            print(f"    message: {it['message']}")
        if it.get("tool") or it.get("skill"):
            print(f"    tool={it.get('tool')} skill={it.get('skill')}")

    if status == "completed":
        out = task.get("output") or ""
        print(f"\n{'─' * 60}")
        print(f"最终输出（{len(out)} 字符）")
        print(f"{'─' * 60}")
        print(out)
        if task.get("usage"):
            print(f"\nusage: {json.dumps(task['usage'], ensure_ascii=False)}")
    elif status in {"failed", "timeout", "cancelled", "rejected"}:
        print(f"\n[错误] {task.get('error') or '(无 error 字段)'}")


def poll_loop(
    base_url: str,
    api_key: str,
    task_id: str,
    poll_timeout: float,
    poll_interval: float,
    heartbeat_interval: float = 30.0,
) -> None:
    """轮询任务直到终态、超时或用户中断（Ctrl+C 自动 cancel）。

    heartbeat_interval：状态无变化时，每 N 秒打印一次心跳，让用户知道脚本没卡死。
    """
    start = time.time()
    last_status = None
    last_heartbeat = 0.0

    try:
        while True:
            elapsed = time.time() - start

            # 整体上限：超时主动取消，避免无限等待
            if elapsed > poll_timeout:
                print(f"\n[轮询超时] 已等待 {int(elapsed)}s，主动取消任务")
                cancel_task(base_url, api_key, task_id)
                return

            task = get_task(base_url, api_key, task_id)
            status = task["status"]

            # 状态变化时打印
            if status != last_status:
                print(f"[{int(elapsed)}s] status: {last_status or '-'} → {status}")
                last_status = status
                last_heartbeat = elapsed  # 状态变化算一次心跳
            elif elapsed - last_heartbeat >= heartbeat_interval:
                # 状态长时间不变，打印心跳避免用户以为卡死
                print(f"[{int(elapsed)}s] still {status}...（vLLM 后台仍在生成 token）")
                last_heartbeat = elapsed

            # 终态 → 打印详情并退出
            if status in TERMINAL_STATES:
                print_task_detail(task)
                return

            # 人机中断 → 等待用户回复
            if status == "waiting_human":
                print_task_detail(task)
                reply = input(
                    "\n请回复（输入 'cancel' 取消任务，其他文本作为 resume 回复）: "
                ).strip()
                if reply.lower() == "cancel":
                    cancel_task(base_url, api_key, task_id)
                    print("任务已取消")
                    return
                st, data = resume_task(base_url, api_key, task_id, reply)
                if st != 200:
                    print(f"resume 失败: HTTP {st}")
                    print(json.dumps(data, ensure_ascii=False, indent=2))
                    return
                print("已提交 resume，继续轮询...")
                # resume 后立即查一次，避免等 poll_interval
                continue

            time.sleep(poll_interval)

    except KeyboardInterrupt:
        print("\n[用户中断 Ctrl+C] 正在取消任务...")
        try:
            cancel_task(base_url, api_key, task_id)
            print("任务已取消")
        except Exception as e:
            print(f"取消失败: {e}")


# ════════════════════════════════════════════════════════════════
# 入口
# ════════════════════════════════════════════════════════════════

def build_payload(args: argparse.Namespace) -> dict:
    """从命令行参数构造 AgentRequest 请求体。"""
    payload: dict = {"timeout_seconds": args.timeout}

    # scene 与 message 二选一
    if args.scene:
        payload["scene"] = args.scene
    elif args.message:
        payload["message"] = args.message
    else:
        raise SystemExit("错误：必须指定 --scene 或 --message")

    # inputs 优先级：--inputs > --feature
    if args.inputs:
        try:
            payload["inputs"] = json.loads(args.inputs)
        except json.JSONDecodeError as e:
            raise SystemExit(f"--inputs JSON 解析失败：{e}") from e
    elif args.feature:
        payload["inputs"] = {"feature_name": args.feature}
    elif args.scene in SCENES_REQUIRING_FEATURE:
        raise SystemExit(
            f"错误：scene={args.scene} 需要 --feature <功能名> 或 --inputs '<json>'"
        )

    # 可选字段
    if args.thread_id:
        payload["thread_id"] = args.thread_id
    if args.idempotency_key:
        payload["idempotency_key"] = args.idempotency_key
    if args.callback_url:
        payload["callback_url"] = args.callback_url
    if args.context:
        try:
            payload["context"] = json.loads(args.context)
        except json.JSONDecodeError as e:
            raise SystemExit(f"--context JSON 解析失败：{e}") from e

    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="异步任务 API 端到端测试脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("AGENT_BASE_URL", DEFAULT_BASE_URL),
        help=f"API 基础地址（默认 {DEFAULT_BASE_URL}，可用环境变量 AGENT_BASE_URL 覆盖）",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("AI_API_KEY", DEFAULT_API_KEY),
        help="API Key（默认 dev-local-key-001，可用环境变量 AI_API_KEY 覆盖）",
    )
    parser.add_argument("--scene", help="场景码，如 PRD_GENERATE / ONE_CLICK_EXPAND")
    parser.add_argument("--message", help="自由文本（智能编排必填，与 scene 二选一）")
    parser.add_argument("--feature", help='快捷参数：等价于 inputs={"feature_name":"<value>"}')
    parser.add_argument(
        "--inputs",
        help='inputs 的 JSON 字符串，覆盖 --feature（例：\'{"feature_name":"位置灯","version":"V1.0"}\'）',
    )
    parser.add_argument(
        "--context",
        help='业务上下文 JSON 字符串（例：\'{"user_id":"u001","requirement_id":"r2025"}\'）',
    )
    parser.add_argument("--thread-id", help="会话 ID，不传则服务端新建并返回")
    parser.add_argument("--idempotency-key", help="幂等键，TTL 内重复提交返回同一 task_id")
    parser.add_argument("--callback-url", help="终态回调 URL（域名必须在服务端白名单内）")
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_POLL_TIMEOUT,
        help=f"整体轮询上限（秒，默认 {DEFAULT_POLL_TIMEOUT}）",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help=f"轮询间隔（秒，默认 {DEFAULT_POLL_INTERVAL}）",
    )
    args = parser.parse_args()

    payload = build_payload(args)

    # 打印提交信息
    print("=" * 70)
    print("提交异步任务")
    print("=" * 70)
    print(f"BASE_URL      : {args.base_url}")
    print(f"scene         : {payload.get('scene') or '(无，走智能编排)'}")
    print(f"message       : {payload.get('message') or '-'}")
    print(f"inputs        : {json.dumps(payload.get('inputs', {}), ensure_ascii=False)}")
    print(f"timeout       : {args.timeout}s")
    print(f"poll_interval : {args.poll_interval}s")
    if payload.get("idempotency_key"):
        print(f"idempotency   : {payload['idempotency_key']}")
    if payload.get("callback_url"):
        print(f"callback_url  : {payload['callback_url']}")
    print()

    # 提交
    try:
        task_id, thread_id, initial_status = submit_task(
            args.base_url, args.api_key, payload
        )
    except RuntimeError as e:
        print(f"[提交失败] {e}")
        sys.exit(1)

    print("[v] 任务已提交（202 Accepted）")
    print(f"    task_id      : {task_id}")
    print(f"    thread_id    : {thread_id}")
    print(f"    initial     : {initial_status}")
    print(f"\n开始轮询（Ctrl+C 可取消任务）...")

    # 轮询
    poll_loop(
        args.base_url,
        args.api_key,
        task_id,
        poll_timeout=args.timeout,
        poll_interval=args.poll_interval,
    )


if __name__ == "__main__":
    main()
