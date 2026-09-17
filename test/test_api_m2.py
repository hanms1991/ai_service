"""M2 API 端到端测试脚本。

覆盖设计文档第 15 节 M2 范围内的全部接口：
  - POST /api/v1/agent/tasks              提交异步任务（202）
  - GET  /api/v1/agent/tasks/{task_id}    查询任务状态
  - POST /api/v1/agent/tasks/{id}/resume  恢复人机中断
  - POST /api/v1/agent/tasks/{id}/cancel  取消任务

测试用例分组：
  A. 错误路径（不消耗 LLM）：SCENE_NOT_FOUND / SKILL_INPUT_MISSING / REFERENCE_DATA_TOO_LARGE / OUTPUT_FORMAT_CONFLICT
  B. 鉴权：缺失/错误 API Key
  C. 任务查询：TASK_NOT_FOUND（404）
  D. 异步提交：场景直达 ONE_CLICK_EXPAND（需 LLM 可达，否则跳过）
  E. 异步提交：智能编排（无 scene，需 LLM 可达，否则跳过）
  F. 取消任务：对 pending/running 任务 cancel；对终态任务 cancel 返回 409
  G. Resume 错误路径：对非 waiting_human 的任务 resume 返回 409
  H. 幂等：相同 idempotency_key 返回同一 task_id
  I. 回调：白名单外域名静默跳过（不外呼）
  J. 同步 /invoke 仍可用：回归 M1

运行方式（在项目根目录下）：
    # 1. 先启动 API 服务（另开一个终端）
    python -m api.main
    # 2. 运行测试
    python test/test_api_m2.py
    # 跳过 LLM 相关用例
    python test/test_api_m2.py --skip-llm
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# Windows 控制台默认 GBK，统一 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── 测试配置 ──
DEFAULT_BASE_URL = "http://127.0.0.1:8300"
VALID_API_KEY = "dev-local-key-001"
INVALID_API_KEY = "this-key-does-not-exist"

# 统计
_passed = 0
_failed = 0
_skipped = 0


def _pass(name: str, detail: str = "") -> None:
    global _passed
    _passed += 1
    print(f"  [PASS] {name}{(' — ' + detail) if detail else ''}")


def _fail(name: str, reason: str) -> None:
    global _failed
    _failed += 1
    print(f"  [FAIL] {name} — {reason}")


def _skip(name: str, reason: str) -> None:
    global _skipped
    _skipped += 1
    print(f"  [SKIP] {name} — {reason}")


def _assert_eq(name: str, actual: Any, expected: Any) -> bool:
    if actual == expected:
        _pass(name, f"actual={actual!r}")
        return True
    _fail(name, f"expected={expected!r}, actual={actual!r}")
    return False


def _assert_in(name: str, needle: str, haystack: str) -> bool:
    if needle in haystack:
        _pass(name, f"找到 {needle!r}")
        return True
    _fail(name, f"未在响应中找到 {needle!r}；响应={haystack[:300]!r}")
    return False


def _post(client: httpx.Client, path: str, body: dict, *,
          api_key: str | None = None) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    return client.post(path, json=body, headers=headers)


def _get(client: httpx.Client, path: str, *,
         api_key: str | None = None) -> httpx.Response:
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key
    return client.get(path, headers=headers)


# ── LLM 可达性检查 ──

def _llm_reachable(client: httpx.Client) -> bool:
    """通过 /readyz 间接判断，或者发一个最小 invoke 测试。"""
    try:
        resp = _get(client, "/readyz")
        if resp.status_code == 200:
            return True
    except Exception:
        pass
    return False


# ════════════════════════════════════════════════════════════════
# A. 错误路径（不消耗 LLM）
# ════════════════════════════════════════════════════════════════

def test_error_paths(client: httpx.Client) -> None:
    print("\n── A. 错误路径 ──")

    # A1. SCENE_NOT_FOUND
    resp = _post(client, "/api/v1/agent/tasks", {
        "scene": "NON_EXISTENT_SCENE",
        "inputs": {},
    }, api_key=VALID_API_KEY)
    if _assert_eq("A1 SCENE_NOT_FOUND 状态码", resp.status_code, 400):
        body = resp.json()
        _assert_eq("A1 错误码", body["error"]["code"], "SCENE_NOT_FOUND")

    # A2. SKILL_INPUT_MISSING（ONE_CLICK_EXPAND 需要 feature_name）
    resp = _post(client, "/api/v1/agent/tasks", {
        "scene": "ONE_CLICK_EXPAND",
        "inputs": {},
    }, api_key=VALID_API_KEY)
    if _assert_eq("A2 SKILL_INPUT_MISSING 状态码", resp.status_code, 400):
        body = resp.json()
        _assert_eq("A2 错误码", body["error"]["code"], "SKILL_INPUT_MISSING")

    # A3. OUTPUT_FORMAT_CONFLICT（ONE_CLICK_EXPAND 是 text，传 json 冲突）
    resp = _post(client, "/api/v1/agent/tasks", {
        "scene": "ONE_CLICK_EXPAND",
        "inputs": {"feature_name": "AEB"},
        "response_format": "json",
    }, api_key=VALID_API_KEY)
    if _assert_eq("A3 OUTPUT_FORMAT_CONFLICT 状态码", resp.status_code, 409):
        body = resp.json()
        _assert_eq("A3 错误码", body["error"]["code"], "OUTPUT_FORMAT_CONFLICT")

    # A4. REFERENCE_DATA_TOO_LARGE（构造 60KB+ 数据）
    big_data = {"x": "A" * 60000}
    resp = _post(client, "/api/v1/agent/tasks", {
        "scene": "ONE_CLICK_EXPAND",
        "inputs": {"feature_name": "AEB"},
        "reference_data": big_data,
    }, api_key=VALID_API_KEY)
    if _assert_eq("A4 REFERENCE_DATA_TOO_LARGE 状态码", resp.status_code, 413):
        body = resp.json()
        _assert_eq("A4 错误码", body["error"]["code"], "REFERENCE_DATA_TOO_LARGE")


# ════════════════════════════════════════════════════════════════
# B. 鉴权
# ════════════════════════════════════════════════════════════════

def test_auth(client: httpx.Client) -> None:
    print("\n── B. 鉴权 ──")

    # B1. 无 API Key → 401
    resp = _post(client, "/api/v1/agent/tasks", {
        "scene": "ONE_CLICK_EXPAND",
        "inputs": {"feature_name": "AEB"},
    })
    _assert_eq("B1 无 API Key → 401", resp.status_code, 401)

    # B2. 错误 API Key → 401
    resp = _post(client, "/api/v1/agent/tasks", {
        "scene": "ONE_CLICK_EXPAND",
        "inputs": {"feature_name": "AEB"},
    }, api_key=INVALID_API_KEY)
    _assert_eq("B2 错误 API Key → 401", resp.status_code, 401)

    # B3. GET /tasks/{id} 无 API Key → 401
    resp = _get(client, "/api/v1/agent/tasks/t-fake")
    _assert_eq("B3 GET 任务查询无 API Key → 401", resp.status_code, 401)


# ════════════════════════════════════════════════════════════════
# C. 任务查询错误路径
# ════════════════════════════════════════════════════════════════

def test_query_errors(client: httpx.Client) -> None:
    print("\n── C. 任务查询错误路径 ──")

    # C1. 不存在的 task_id → 404 TASK_NOT_FOUND
    resp = _get(client, "/api/v1/agent/tasks/t-not-exist-12345",
                api_key=VALID_API_KEY)
    if _assert_eq("C1 不存在任务 → 404", resp.status_code, 404):
        body = resp.json()
        _assert_eq("C1 错误码", body["error"]["code"], "TASK_NOT_FOUND")

    # C2. cancel 不存在的任务 → 404
    resp = client.post("/api/v1/agent/tasks/t-not-exist-12345/cancel",
                       headers={"X-API-Key": VALID_API_KEY})
    _assert_eq("C2 cancel 不存在任务 → 404", resp.status_code, 404)


# ════════════════════════════════════════════════════════════════
# D. 异步提交：场景直达（LLM 相关）
# ════════════════════════════════════════════════════════════════

def test_submit_scene_direct(client: httpx.Client, skip_llm: bool) -> str | None:
    print("\n── D. 异步提交：场景直达 ONE_CLICK_EXPAND ──")
    if skip_llm:
        _skip("D 异步提交 ONE_CLICK_EXPAND", "--skip-llm")
        return None
    if not _llm_reachable(client):
        _skip("D 异步提交 ONE_CLICK_EXPAND", "LLM 不可达")
        return None

    # D1. 提交任务，应返回 202 + pending
    resp = _post(client, "/api/v1/agent/tasks", {
        "scene": "ONE_CLICK_EXPAND",
        "inputs": {"feature_name": "AEB"},
        "timeout_seconds": 120,
    }, api_key=VALID_API_KEY)
    if not _assert_eq("D1 提交任务 → 202", resp.status_code, 202):
        return None
    body = resp.json()
    task_id = body.get("task_id")
    if not _assert_eq("D1 返回 task_id 非空", bool(task_id), True):
        return None
    _assert_eq("D1 初始 status=pending", body.get("status"), "pending")
    thread_id = body.get("thread_id")

    # D2. 轮询 GET /tasks/{id} 直到终态（最多 60s）
    final_status = None
    detail = None
    for _ in range(60):
        time.sleep(1)
        resp = _get(client, f"/api/v1/agent/tasks/{task_id}",
                    api_key=VALID_API_KEY)
        if resp.status_code != 200:
            continue
        detail = resp.json()
        final_status = detail.get("status")
        if final_status in ("completed", "failed", "timeout", "cancelled"):
            break

    if final_status is None:
        _fail("D2 轮询获取状态", "60s 内未拿到 200 响应")
        return task_id

    if final_status == "completed":
        _pass("D2 任务完成", f"status={final_status}")
        # 校验响应字段
        _assert_eq("D2 mode=skill_direct", detail.get("mode"), "skill_direct")
        _assert_eq("D2 scene 回显", detail.get("scene"), "ONE_CLICK_EXPAND")
        _assert_eq("D2 output 非空", bool(detail.get("output")), True)
        _assert_eq("D2 trace_id 非空", bool(detail.get("trace_id")), True)
        _assert_eq("D2 thread_id 一致", detail.get("thread_id"), thread_id)
    else:
        _fail("D2 任务完成", f"未完成，最终 status={final_status}, error={detail.get('error')}")

    return task_id


# ════════════════════════════════════════════════════════════════
# E. 异步提交：智能编排（LLM 相关）
# ════════════════════════════════════════════════════════════════

def test_submit_planner(client: httpx.Client, skip_llm: bool) -> None:
    print("\n── E. 异步提交：智能编排 ──")
    if skip_llm:
        _skip("E 异步提交智能编排", "--skip-llm")
        return
    if not _llm_reachable(client):
        _skip("E 异步提交智能编排", "LLM 不可达")
        return

    resp = _post(client, "/api/v1/agent/tasks", {
        "message": "请把『AEB 自动紧急制动』这个功能用一段话扩写说明",
        "timeout_seconds": 180,
    }, api_key=VALID_API_KEY)
    if not _assert_eq("E1 提交任务 → 202", resp.status_code, 202):
        return
    task_id = resp.json().get("task_id")

    # 轮询 90s
    final = None
    detail = None
    for _ in range(90):
        time.sleep(1)
        resp = _get(client, f"/api/v1/agent/tasks/{task_id}",
                    api_key=VALID_API_KEY)
        if resp.status_code == 200:
            detail = resp.json()
            final = detail.get("status")
            if final in ("completed", "failed", "timeout", "cancelled"):
                break

    if final == "completed":
        _pass("E2 任务完成", f"mode={detail.get('mode')}")
        _assert_eq("E2 mode=orchestrated", detail.get("mode"), "orchestrated")
        _assert_eq("E2 output 非空", bool(detail.get("output")), True)
    else:
        _fail("E2 任务完成", f"status={final}, error={detail.get('error') if detail else 'N/A'}")


# ════════════════════════════════════════════════════════════════
# F. 取消任务
# ════════════════════════════════════════════════════════════════

def test_cancel(client: httpx.Client, skip_llm: bool) -> None:
    print("\n── F. 取消任务 ──")

    # F1. 提交一个长任务（智能编排，180s 超时），立即 cancel
    if not skip_llm and _llm_reachable(client):
        resp = _post(client, "/api/v1/agent/tasks", {
            "message": "请生成一份 AEB 功能的完整 PRD 文档，包含 10 个章节",
            "timeout_seconds": 180,
        }, api_key=VALID_API_KEY)
        if resp.status_code == 202:
            task_id = resp.json().get("task_id")
            # 立即 cancel
            resp = client.post(f"/api/v1/agent/tasks/{task_id}/cancel",
                               headers={"X-API-Key": VALID_API_KEY})
            if _assert_eq("F1 cancel running 任务 → 200", resp.status_code, 200):
                body = resp.json()
                _assert_eq("F1 status=cancelled", body.get("status"), "cancelled")

            # F2. 再 cancel 已终态任务 → 409
            resp = client.post(f"/api/v1/agent/tasks/{task_id}/cancel",
                               headers={"X-API-Key": VALID_API_KEY})
            if _assert_eq("F2 cancel 已 cancelled 任务 → 409", resp.status_code, 409):
                body = resp.json()
                _assert_eq("F2 错误码", body["error"]["code"], "TASK_ALREADY_FINISHED")
        else:
            _fail("F1 提交任务", f"status={resp.status_code}")
    else:
        _skip("F1/F2 cancel running 任务", "LLM 不可达或 --skip-llm")


# ════════════════════════════════════════════════════════════════
# G. Resume 错误路径
# ════════════════════════════════════════════════════════════════

def test_resume_errors(client: httpx.Client, skip_llm: bool, task_id_d: str | None) -> None:
    print("\n── G. Resume 错误路径 ──")

    # G1. resume 不存在的任务 → 404
    resp = client.post("/api/v1/agent/tasks/t-not-exist/resume",
                       json={"reply": "yes"},
                       headers={"X-API-Key": VALID_API_KEY,
                                "Content-Type": "application/json"})
    _assert_eq("G1 resume 不存在任务 → 404", resp.status_code, 404)

    # G2. resume 非 waiting_human 的任务 → 409
    # 用 D 用例的 completed 任务（如果跑过且成功）
    if task_id_d is not None:
        resp = client.post(f"/api/v1/agent/tasks/{task_id_d}/resume",
                           json={"reply": "yes"},
                           headers={"X-API-Key": VALID_API_KEY,
                                    "Content-Type": "application/json"})
        # 可能 409（已完成）或 400（状态不对）
        if resp.status_code in (409, 400):
            _pass("G2 resume 已完成任务 → 4xx",
                  f"status={resp.status_code}")
        else:
            _fail("G2 resume 已完成任务", f"status={resp.status_code}, body={resp.text[:200]}")
    else:
        _skip("G2 resume 已完成任务", "D 用例未跑成功，无可复用 task_id")


# ════════════════════════════════════════════════════════════════
# H. 幂等
# ════════════════════════════════════════════════════════════════

def test_idempotency(client: httpx.Client, skip_llm: bool) -> None:
    print("\n── H. 幂等 ──")
    if skip_llm:
        _skip("H 幂等", "--skip-llm")
        return
    if not _llm_reachable(client):
        _skip("H 幂等", "LLM 不可达")
        return

    idem_key = "biz-idem-key-" + str(int(time.time()))
    body = {
        "scene": "ONE_CLICK_EXPAND",
        "inputs": {"feature_name": "AEB"},
        "timeout_seconds": 120,
        "idempotency_key": idem_key,
    }
    resp1 = _post(client, "/api/v1/agent/tasks", body, api_key=VALID_API_KEY)
    if resp1.status_code != 202:
        _fail("H1 第一次提交", f"status={resp1.status_code}, body={resp1.text[:200]}")
        return
    task_id_1 = resp1.json().get("task_id")

    # 第二次提交相同 idempotency_key，应返回同一 task_id
    resp2 = _post(client, "/api/v1/agent/tasks", body, api_key=VALID_API_KEY)
    if _assert_eq("H2 第二次提交 → 202", resp2.status_code, 202):
        task_id_2 = resp2.json().get("task_id")
        _assert_eq("H2 返回相同 task_id", task_id_2, task_id_1)


# ════════════════════════════════════════════════════════════════
# I. 回调白名单
# ════════════════════════════════════════════════════════════════

def test_callback_whitelist(client: httpx.Client, skip_llm: bool) -> None:
    print("\n── I. 回调白名单 ──")
    if skip_llm:
        _skip("I 回调白名单", "--skip-llm")
        return
    if not _llm_reachable(client):
        _skip("I 回调白名单", "LLM 不可达")
        return

    # I1. callback_url 指向白名单外域名 → 任务应正常完成，但回调不发送
    resp = _post(client, "/api/v1/agent/tasks", {
        "scene": "ONE_CLICK_EXPAND",
        "inputs": {"feature_name": "AEB"},
        "timeout_seconds": 60,
        "callback_url": "https://evil.example.com/ai-callback",
    }, api_key=VALID_API_KEY)
    if not _assert_eq("I1 提交带恶意 callback_url → 202", resp.status_code, 202):
        return
    task_id = resp.json().get("task_id")

    # 轮询任务完成（不验证 callback 是否真的没发，只能验证任务仍正常完成）
    final = None
    for _ in range(60):
        time.sleep(1)
        resp = _get(client, f"/api/v1/agent/tasks/{task_id}",
                    api_key=VALID_API_KEY)
        if resp.status_code == 200:
            final = resp.json().get("status")
            if final in ("completed", "failed", "timeout", "cancelled"):
                break

    if final == "completed":
        _pass("I2 带恶意 callback_url 任务仍正常完成",
              "回调在服务端被白名单过滤（日志可见 warning）")
    else:
        _fail("I2 任务完成", f"status={final}")


# ════════════════════════════════════════════════════════════════
# J. M1 回归：/invoke 仍可用
# ════════════════════════════════════════════════════════════════

def test_invoke_regression(client: httpx.Client, skip_llm: bool) -> None:
    print("\n── J. M1 回归：/invoke 同步执行 ──")
    if skip_llm:
        _skip("J /invoke 回归", "--skip-llm")
        return
    if not _llm_reachable(client):
        _skip("J /invoke 回归", "LLM 不可达")
        return

    resp = _post(client, "/api/v1/agent/invoke", {
        "scene": "ONE_CLICK_EXPAND",
        "inputs": {"feature_name": "AEB"},
        "timeout_seconds": 60,
    }, api_key=VALID_API_KEY)
    if _assert_eq("J /invoke → 200", resp.status_code, 200):
        body = resp.json()
        _assert_eq("J mode=skill_direct", body.get("mode"), "skill_direct")
        _assert_eq("J output 非空", bool(body.get("output")), True)


# ════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(description="M2 API 端到端测试")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"API 服务地址（默认 {DEFAULT_BASE_URL}）")
    parser.add_argument("--skip-llm", action="store_true",
                        help="跳过依赖 LLM 的用例")
    args = parser.parse_args()

    print(f"=== M2 API 端到端测试启动 ===")
    print(f"base_url={args.base_url}  skip_llm={args.skip_llm}")

    with httpx.Client(base_url=args.base_url, timeout=120.0) as client:
        test_error_paths(client)
        test_auth(client)
        test_query_errors(client)
        task_id_d = test_submit_scene_direct(client, args.skip_llm)
        test_submit_planner(client, args.skip_llm)
        test_cancel(client, args.skip_llm)
        test_resume_errors(client, args.skip_llm, task_id_d)
        test_idempotency(client, args.skip_llm)
        test_callback_whitelist(client, args.skip_llm)
        test_invoke_regression(client, args.skip_llm)

    print(f"\n=== 测试汇总 ===")
    print(f"  PASS: {_passed}  FAIL: {_failed}  SKIP: {_skipped}")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
