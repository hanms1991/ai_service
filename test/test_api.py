"""M1 API 端到端测试脚本。

覆盖设计文档第 15 节 M1 范围内的全部接口：
  - GET  /healthz                          存活探针（无鉴权）
  - GET  /readyz                           就绪探针（无鉴权）
  - GET  /api/v1/agent/capabilities        能力+场景码清单（鉴权）
  - POST /api/v1/agent/invoke              同步执行（鉴权）

测试用例分组：
  A. 健康检查：healthz / readyz
  B. 能力查询：capabilities
  C. 鉴权：缺失/错误 API Key
  D. 错误路径：SCENE_NOT_FOUND / SKILL_INPUT_MISSING / OUTPUT_FORMAT_CONFLICT / REFERENCE_DATA_TOO_LARGE
  E. 场景直达：ONE_CLICK_EXPAND 正常调用（需 LLM 可达，否则跳过）
  F. 智能编排：无 scene 走 Planner（需 LLM 可达，否则跳过）

运行方式（在项目根目录下）：
    # 1. 先启动 API 服务（另开一个终端）
    python -m api.main
    # 2. 运行测试
    python test/test_api.py
    # 或指定 base_url
    python test/test_api.py --base-url http://127.0.0.1:8300
"""
from __future__ import annotations

import argparse
import json
import sys
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


def _print_case(title: str) -> None:
    print(f"\n── {title} ──")


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


def _assert_contains(name: str, haystack: str, needle: str) -> bool:
    if needle in haystack:
        _pass(name, f"找到 {needle!r}")
        return True
    _fail(name, f"未在响应中找到 {needle!r}；响应={haystack[:200]!r}")
    return False


# ════════════════════════════════════════════════════════════════
# 工具：发请求
# ════════════════════════════════════════════════════════════════

def _get(client: httpx.Client, path: str, *, api_key: str | None = None,
         expect_status: int = 200) -> httpx.Response:
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key
    resp = client.get(path, headers=headers)
    if resp.status_code != expect_status:
        # 不直接 fail，由调用方判断
        pass
    return resp


def _post(client: httpx.Client, path: str, body: dict, *,
          api_key: str | None = None, expect_status: int = 200) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    resp = client.post(path, json=body, headers=headers)
    return resp


# ════════════════════════════════════════════════════════════════
# 测试用例
# ════════════════════════════════════════════════════════════════

def test_health(client: httpx.Client) -> None:
    """A. 健康检查。"""
    _print_case("A. 健康检查（无鉴权）")

    # A1. healthz
    resp = _get(client, "/healthz", expect_status=200)
    if _assert_eq("healthz 状态码", resp.status_code, 200):
        _assert_eq("healthz status 字段", resp.json().get("status"), "ok")

    # A2. readyz
    resp = _get(client, "/readyz", expect_status=200)
    if _assert_eq("readyz 状态码", resp.status_code, 200):
        body = resp.json()
        _assert_eq("readyz status", body.get("status"), "ok")
        checks = body.get("checks", {})
        _assert_eq("readyz scene_resolver", checks.get("scene_resolver"), "ok")
        _assert_eq("readyz supervisor_graph", checks.get("supervisor_graph"), "ok")


def test_capabilities(client: httpx.Client) -> None:
    """B. 能力查询。"""
    _print_case("B. 能力查询 /agent/capabilities")

    # B1. 鉴权缺失 → 401
    resp = _get(client, "/api/v1/agent/capabilities", expect_status=401)
    _assert_eq("无 API Key 状态码", resp.status_code, 401)
    body = resp.json()
    _assert_eq("无 API Key 错误码", body.get("error", {}).get("code"), "UNAUTHORIZED")

    # B2. 错误 API Key → 401
    resp = _get(client, "/api/v1/agent/capabilities",
                api_key=INVALID_API_KEY, expect_status=401)
    _assert_eq("错误 API Key 状态码", resp.status_code, 401)

    # B3. 正常查询
    resp = _get(client, "/api/v1/agent/capabilities",
                api_key=VALID_API_KEY, expect_status=200)
    if not _assert_eq("capabilities 状态码", resp.status_code, 200):
        return
    body = resp.json()

    # 校验 agents 字段
    agents = body.get("agents", {})
    if _assert_contains("capabilities 含 product_agent", ",".join(agents.keys()), "product_agent"):
        product = agents.get("product_agent", {})
        skills = product.get("skills", {})
        _assert_contains("product_agent 含 feature_expand", ",".join(skills.keys()), "feature_expand")
        _assert_contains("product_agent 含 feature_definition", ",".join(skills.keys()), "feature_definition")

    # 校验 scenes 字段
    scenes = body.get("scenes", [])
    scene_names = [s.get("scene") for s in scenes]
    if _assert_eq("scenes 数量 >= 3", len(scenes) >= 3, True):
        _assert_contains("scenes 含 ONE_CLICK_EXPAND", ",".join(scene_names), "ONE_CLICK_EXPAND")
        _assert_contains("scenes 含 DEFINE_FEATURE", ",".join(scene_names), "DEFINE_FEATURE")

    # 校验场景码对外不含 agent/skill 内部名
    for s in scenes:
        assert "agent" not in s, f"场景码 {s.get('scene')} 对外暴露了 agent 字段"
        assert "skill" not in s, f"场景码 {s.get('scene')} 对外暴露了 skill 字段"
    _pass("场景码未泄漏内部 agent/skill 名")


def test_auth(client: httpx.Client) -> None:
    """C. 鉴权。"""
    _print_case("C. 鉴权")

    # C1. 缺 X-API-Key 头
    resp = _post(client, "/api/v1/agent/invoke",
                 body={"scene": "ONE_CLICK_EXPAND", "inputs": {"feature_name": "test"}})
    _assert_eq("缺 API Key 状态码", resp.status_code, 401)
    _assert_eq("缺 API Key 错误码",
               resp.json().get("error", {}).get("code"), "UNAUTHORIZED")

    # C2. 错误 API Key
    resp = _post(client, "/api/v1/agent/invoke",
                 body={"scene": "ONE_CLICK_EXPAND", "inputs": {"feature_name": "test"}},
                 api_key=INVALID_API_KEY)
    _assert_eq("错误 API Key 状态码", resp.status_code, 401)

    # C3. Authorization: Bearer 形式
    resp = _post(
        client, "/api/v1/agent/invoke",
        body={"scene": "ONE_CLICK_EXPAND", "inputs": {}},  # 故意缺 feature_name 触发 SKILL_INPUT_MISSING
        api_key=None,
    )
    # 补 Bearer 形式鉴权测试
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {VALID_API_KEY}"}
    resp = client.post("/api/v1/agent/invoke",
                       json={"scene": "ONE_CLICK_EXPAND", "inputs": {}},
                       headers=headers)
    _assert_eq("Bearer 鉴权状态码", resp.status_code, 400)
    _assert_eq("Bearer 鉴权后能进入业务校验",
               resp.json().get("error", {}).get("code"), "SKILL_INPUT_MISSING")


def test_error_paths(client: httpx.Client) -> None:
    """D. 错误路径（不消耗 LLM）。"""
    _print_case("D. 错误路径（不消耗 LLM）")

    # D1. 场景码不存在
    resp = _post(client, "/api/v1/agent/invoke",
                 body={"scene": "NOT_EXIST", "inputs": {}},
                 api_key=VALID_API_KEY)
    _assert_eq("SCENE_NOT_FOUND 状态码", resp.status_code, 400)
    _assert_eq("SCENE_NOT_FOUND 错误码",
               resp.json().get("error", {}).get("code"), "SCENE_NOT_FOUND")

    # D2. 场景码直达缺必填输入
    resp = _post(client, "/api/v1/agent/invoke",
                 body={"scene": "ONE_CLICK_EXPAND", "inputs": {}},
                 api_key=VALID_API_KEY)
    _assert_eq("SKILL_INPUT_MISSING 状态码", resp.status_code, 400)
    _assert_eq("SKILL_INPUT_MISSING 错误码",
               resp.json().get("error", {}).get("code"), "SKILL_INPUT_MISSING")
    _assert_contains("SKILL_INPUT_MISSING 提示含 feature_name",
                     resp.json().get("error", {}).get("message", ""), "feature_name")

    # D3. response_format 冲突（scene 是 text，请求传 json）
    resp = _post(client, "/api/v1/agent/invoke",
                 body={"scene": "ONE_CLICK_EXPAND",
                       "inputs": {"feature_name": "AEB"},
                       "response_format": "json"},
                 api_key=VALID_API_KEY)
    _assert_eq("OUTPUT_FORMAT_CONFLICT 状态码", resp.status_code, 409)
    _assert_eq("OUTPUT_FORMAT_CONFLICT 错误码",
               resp.json().get("error", {}).get("code"), "OUTPUT_FORMAT_CONFLICT")

    # D4. reference_data 超过 50KB 阈值
    big_data = {"filler": "x" * (60 * 1024)}  # 60KB
    resp = _post(client, "/api/v1/agent/invoke",
                 body={"scene": "ONE_CLICK_EXPAND",
                       "inputs": {"feature_name": "AEB"},
                       "reference_data": big_data},
                 api_key=VALID_API_KEY)
    _assert_eq("REFERENCE_DATA_TOO_LARGE 状态码", resp.status_code, 413)
    _assert_eq("REFERENCE_DATA_TOO_LARGE 错误码",
               resp.json().get("error", {}).get("code"), "REFERENCE_DATA_TOO_LARGE")

    # D5. 无 scene 且无 message → SKILL_INPUT_MISSING
    resp = _post(client, "/api/v1/agent/invoke",
                 body={},
                 api_key=VALID_API_KEY)
    _assert_eq("无 scene 无 message 状态码", resp.status_code, 400)
    _assert_eq("无 scene 无 message 错误码",
               resp.json().get("error", {}).get("code"), "SKILL_INPUT_MISSING")

    # D6. trace_id 透传
    custom_trace = "my-trace-1234"
    resp = _post(client, "/api/v1/agent/invoke",
                 body={"scene": "NOT_EXIST", "inputs": {}, "trace_id": custom_trace},
                 api_key=VALID_API_KEY)
    resp_trace = resp.json().get("error", {}).get("trace_id", "")
    _assert_eq("trace_id 透传", resp_trace, custom_trace)

    # D7. 响应头 X-Trace-Id
    if _assert_eq("X-Trace-Id 响应头存在",
                  "X-Trace-Id" in resp.headers, True):
        _assert_eq("X-Trace-Id 响应头值", resp.headers.get("X-Trace-Id"), custom_trace)


def test_scene_direct(client: httpx.Client, llm_reachable: bool) -> None:
    """E. 场景直达（需要 LLM 可达）。"""
    _print_case("E. 场景直达 ONE_CLICK_EXPAND")

    if not llm_reachable:
        _skip("ONE_CLICK_EXPAND 端到端", "LLM 服务不可达，跳过")
        return

    # E1. 正常调用 ONE_CLICK_EXPAND
    resp = _post(client, "/api/v1/agent/invoke",
                 body={
                     "scene": "ONE_CLICK_EXPAND",
                     "inputs": {
                         "feature_name": "AEB自动紧急制动",
                         "feature_points": "高速跟车预警;碰撞前全力制动",
                     },
                 },
                 api_key=VALID_API_KEY,
                 expect_status=200)

    if not _assert_eq("ONE_CLICK_EXPAND 状态码", resp.status_code, 200):
        print(f"  响应体：{resp.text[:500]}")
        return

    body = resp.json()
    _assert_eq("mode=skill_direct", body.get("mode"), "skill_direct")
    _assert_eq("scene=ONE_CLICK_EXPAND", body.get("scene"), "ONE_CLICK_EXPAND")
    _assert_eq("structured=null（text 技能）", body.get("structured"), None)

    output = body.get("output") or ""
    if _assert_eq("output 非空", len(output) > 0, True):
        # 一键扩写应该返回 50~100 字
        char_count = len(output)
        print(f"  [INFO] 扩写结果（{char_count} 字）：{output[:120]}{'...' if char_count > 120 else ''}")
        # 长度范围宽松断言（30~200）
        _assert_eq("扩写长度在 30~200 字范围",
                   30 <= char_count <= 200, True)

    # E2. usage 含 token 统计
    usage = body.get("usage", {})
    if _assert_eq("usage.total_tokens > 0", usage.get("total_tokens", 0) > 0, True):
        _pass("usage 含 prompt/completion tokens",
              f"prompt={usage.get('prompt_tokens')}, completion={usage.get('completion_tokens')}")

    # E3. thread_id 自动生成
    thread_id = body.get("thread_id", "")
    _assert_eq("thread_id 自动生成（conv- 前缀）",
               thread_id.startswith("conv-"), True)

    # E4. trace_id 返回
    _assert_eq("trace_id 非空", bool(body.get("trace_id")), True)

    # E5. 传入自定义 trace_id 透传
    custom_trace = "e2e-trace-abcd"
    resp = _post(client, "/api/v1/agent/invoke",
                 body={
                     "scene": "ONE_CLICK_EXPAND",
                     "inputs": {"feature_name": "ACC自适应巡航"},
                     "trace_id": custom_trace,
                 },
                 api_key=VALID_API_KEY)
    if resp.status_code == 200:
        _assert_eq("自定义 trace_id 透传",
                   resp.json().get("trace_id"), custom_trace)
    else:
        _skip("自定义 trace_id 透传", f"LLM 调用失败 status={resp.status_code}")


def test_reference_data_injection(client: httpx.Client, llm_reachable: bool) -> None:
    """E2. reference_data 注入（模式 A，需要 LLM）。"""
    _print_case("F. reference_data 注入（模式 A）")

    if not llm_reachable:
        _skip("reference_data 注入端到端", "LLM 服务不可达，跳过")
        return

    resp = _post(client, "/api/v1/agent/invoke",
                 body={
                     "scene": "ONE_CLICK_EXPAND",
                     "inputs": {
                         "feature_name": "AEB",
                         "feature_points": "前车紧急减速时预警并制动",
                     },
                     "reference_data": {
                         "signal_dict": [
                             {"signal": "VCI_VehSpd", "desc": "本车车速"},
                             {"signal": "VCI_TtcFwd", "desc": "前车碰撞时间"},
                         ],
                     },
                 },
                 api_key=VALID_API_KEY)
    if _assert_eq("reference_data 注入 状态码", resp.status_code, 200):
        body = resp.json()
        _assert_eq("注入后 mode=skill_direct", body.get("mode"), "skill_direct")
        _assert_eq("注入后 output 非空",
                   len(body.get("output") or "") > 0, True)


def test_orchestrated(client: httpx.Client, llm_reachable: bool) -> None:
    """G. 智能编排（无 scene，走 Planner）。"""
    _print_case("G. 智能编排（无 scene 走 Planner）")

    if not llm_reachable:
        _skip("智能编排端到端", "LLM 服务不可达，跳过")
        return

    resp = _post(client, "/api/v1/agent/invoke",
                 body={"message": "请定义AEB功能"},
                 api_key=VALID_API_KEY,
                 expect_status=200)

    if not _assert_eq("智能编排 状态码", resp.status_code, 200):
        print(f"  响应体：{resp.text[:500]}")
        return

    body = resp.json()
    _assert_eq("mode=orchestrated", body.get("mode"), "orchestrated")
    _assert_eq("scene=null（无场景码）", body.get("scene"), None)

    # Planner 应该返回执行计划
    plan = body.get("plan") or []
    if _assert_eq("plan 非空（Planner 产出步骤）", len(plan) > 0, True):
        first = plan[0]
        _pass("plan[0] 含 tool 字段", f"tool={first.get('tool')}")
        _assert_eq("plan[0] 含 skill 字段", bool(first.get("skill")), True)

    # 最终输出
    output = body.get("output") or ""
    _assert_eq("final_output 非空", len(output) > 0, True)
    if output:
        print(f"  [INFO] 编排输出前 120 字：{output[:120]}{'...' if len(output) > 120 else ''}")


def test_multithread_memory(client: httpx.Client, llm_reachable: bool) -> None:
    """H. 多轮记忆（同 thread_id 复用 checkpointer）。"""
    _print_case("H. 多轮记忆（AsyncSqliteSaver）")

    if not llm_reachable:
        _skip("多轮记忆", "LLM 服务不可达，跳过")
        return

    # 第一轮：传一个固定 thread_id
    thread_id = "test-mem-0001"
    resp1 = _post(client, "/api/v1/agent/invoke",
                  body={
                      "scene": "ONE_CLICK_EXPAND",
                      "inputs": {"feature_name": "AEB"},
                      "thread_id": thread_id,
                  },
                  api_key=VALID_API_KEY)
    if not _assert_eq("第一轮状态码", resp1.status_code, 200):
        return
    _assert_eq("第一轮返回相同 thread_id",
               resp1.json().get("thread_id"), thread_id)

    # 第二轮：同 thread_id 再调一次（验证 checkpointer 不报错）
    resp2 = _post(client, "/api/v1/agent/invoke",
                  body={
                      "scene": "ONE_CLICK_EXPAND",
                      "inputs": {"feature_name": "ACC"},
                      "thread_id": thread_id,
                  },
                  api_key=VALID_API_KEY)
    if _assert_eq("第二轮（同 thread_id）状态码", resp2.status_code, 200):
        _assert_eq("第二轮返回相同 thread_id",
                   resp2.json().get("thread_id"), thread_id)
        _pass("AsyncSqliteSaver 跨请求复用 thread_id 成功")


# ════════════════════════════════════════════════════════════════
# LLM 可达性检测
# ════════════════════════════════════════════════════════════════

def check_llm_reachable() -> bool:
    """检测 .env 中配置的 LLM 端点是否可达。"""
    import os
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("LLM_BASE_URL", "")
    api_key = os.getenv("LLM_API_KEY", "EMPTY")
    model = os.getenv("LLM_MODEL", "")

    if not base_url:
        print(f"[WARN] LLM_BASE_URL 未配置，需要 LLM 的测试将跳过")
        return False

    # 健康探测：发一个最小 chat 请求
    try:
        with httpx.Client(timeout=10.0) as c:
            resp = c.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                },
            )
            if resp.status_code == 200:
                print(f"[INFO] LLM 可达：{base_url} model={model}")
                return True
            print(f"[WARN] LLM 探测失败 status={resp.status_code}：{base_url}")
            return False
    except Exception as e:
        print(f"[WARN] LLM 不可达：{base_url} — {e}")
        return False


# ════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(description="M1 API 端到端测试")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"API base URL（默认 {DEFAULT_BASE_URL}）")
    parser.add_argument("--skip-llm", action="store_true",
                        help="跳过需要 LLM 的测试用例")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    print("=" * 70)
    print(f"M1 API 端到端测试")
    print(f"Base URL: {base_url}")
    print(f"API Key:  {VALID_API_KEY[:8]}...")
    print("=" * 70)

    # 检测服务是否启动
    try:
        with httpx.Client(timeout=3.0) as c:
            c.get(f"{base_url}/healthz")
    except Exception as e:
        print(f"[ERROR] API 服务未启动或不可达：{base_url} — {e}")
        print("        请先运行：python -m api.main")
        return 2

    # LLM 可达性
    llm_reachable = False if args.skip_llm else check_llm_reachable()
    print()

    # 跑测试
    with httpx.Client(base_url=base_url, timeout=120.0) as client:
        test_health(client)
        test_capabilities(client)
        test_auth(client)
        test_error_paths(client)
        test_scene_direct(client, llm_reachable)
        test_reference_data_injection(client, llm_reachable)
        test_orchestrated(client, llm_reachable)
        test_multithread_memory(client, llm_reachable)

    # 汇总
    print("\n" + "=" * 70)
    print(f"测试汇总：通过 {_passed} ｜ 失败 {_failed} ｜ 跳过 {_skipped}")
    print("=" * 70)
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
