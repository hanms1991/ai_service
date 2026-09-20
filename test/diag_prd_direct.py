"""PRD 生成直接调用诊断脚本。

绕过 API 层（FastAPI/asyncio.wait_for/asyncio.to_thread），直接调
agents.capability_registry.execute_skill_v2 测 PRD 生成实际耗时。

用途：
    - 如果直接调用也慢（>10 分钟）→ LLM 真的慢，需要优化 prompt 或调超时
    - 如果直接调用快（<10 分钟）→ API 层有问题，需要排查 asyncio 链路

运行：
    python test/diag_prd_direct.py
    python test/diag_prd_direct.py --feature "位置灯" --project "EVX-2025" --version "V1.0"
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Windows 控制台 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 项目根目录加入模块搜索路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import yaml  # noqa: E402
from agents.capability_registry import (  # noqa: E402
    execute_skill_v2,
    init_registry_from_supervisor,
)
from core.logging import LLMInteractionLogger  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="PRD 生成直接调用诊断脚本（绕过 API 层）")
    parser.add_argument("--feature", default="位置灯", help="功能名称（默认 位置灯）")
    parser.add_argument("--project", default="通用", help="项目名（默认 通用）")
    parser.add_argument("--version", default="V1.0", help="版本号（默认 V1.0）")
    parser.add_argument(
        "--log",
        action="store_true",
        help="是否启用 LLMInteractionLogger（写入 logs/llm_trace.log）",
    )
    args = parser.parse_args()

    # ── 1. 初始化注册表（与 api.main lifespan 一致） ──
    supervisor_cfg = yaml.safe_load(
        open(PROJECT_ROOT / "agents" / "configs" / "supervisor_agent.yaml", encoding="utf-8")
    )
    init_registry_from_supervisor(supervisor_cfg)
    print("[1/3] 能力注册表初始化 OK")

    # ── 2. 构造 runnable_config（可选 logger） ──
    inputs = {
        "feature_name": args.feature,
        "project": args.project,
        "version": args.version,
    }
    print(f"[2/3] inputs = {inputs}")

    runnable_config = None
    if args.log:
        log_path = PROJECT_ROOT / "logs" / "llm_trace.log"
        logger = LLMInteractionLogger(log_path, verbose=True)
        runnable_config = {
            "configurable": {"thread_id": "diag-prd-direct"},
            "callbacks": [logger],
        }
        print(f"      logger → {log_path}")
    else:
        print("      logger 未启用（--log 开启）")

    # ── 3. 直接调用 execute_skill_v2，测时间 ──
    print(f"[3/3] 开始调用 execute_skill_v2（agent=product_agent, skill=prd_generation）...")
    print(f"      LLM 调用同步阻塞，请耐心等待...")

    start = time.time()
    try:
        result = execute_skill_v2(
            agent_name="product_agent",
            skill_name="prd_generation",
            inputs=inputs,
            reference_data=None,
            runnable_config=runnable_config,
        )
    except Exception as e:
        elapsed = time.time() - start
        print(f"\n[失败] 耗时 {elapsed:.1f}s")
        print(f"  异常类型: {type(e).__name__}")
        print(f"  异常消息: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    elapsed = time.time() - start

    # ── 4. 打印结果 ──
    print(f"\n{'═' * 70}")
    print(f"完成！耗时 {elapsed:.1f}s ({int(elapsed // 60)}m {int(elapsed % 60)}s)")
    print(f"{'═' * 70}")
    print(f"输出长度: {len(result.text)} 字符")
    print(f"usage: {result.structured or result.usage}")

    print(f"\n{'─' * 70}")
    print("PRD 文档前 500 字符预览:")
    print(f"{'─' * 70}")
    print(result.text[:500])
    print(f"\n... (剩余 {len(result.text) - 500} 字符省略)")

    # 评估
    print(f"\n{'═' * 70}")
    print("诊断结论:")
    print(f"{'═' * 70}")
    if elapsed < 60:
        print(f"  ✗ LLM 实际只用 {elapsed:.1f}s，但异步任务卡了 1200s → API 层有问题")
        print("  排查方向：asyncio.to_thread 线程池 / asyncio.wait_for 取消传播 / TaskStore 锁竞争")
    elif elapsed < 600:
        print(f"  ✓ LLM 实际用 {elapsed:.1f}s（< 10 分钟），异步任务应该能完成")
        print("  排查方向：API 层 asyncio 链路有问题，或任务记录未正确更新状态")
    else:
        print(f"  ⚠ LLM 实际用 {elapsed:.1f}s（> 10 分钟），vLLM 真的慢")
        print("  排查方向：vLLM tokens/s 吞吐 / prompt 长度 / 模型选择")


if __name__ == "__main__":
    main()
