"""端到端测试：LangGraph 规划-执行分离的 Supervisor 调度 product_agent 完成 AEB 功能定义。

运行方式（在项目根目录下）：
    python test/test.py

预期链路：
    用户 "定义AEB功能"
      → planner LLM 输出 JSON 计划：[{tool: product_agent, input: 定义AEB功能, is_final: true, mode: single}]
      → executor 调用 product_agent → product_agent 调用 load_skill → 按模板产出 AEB 功能定义
      → route 检测到 final_output 已设置 → END
"""
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage

# Windows 控制台默认 GBK，统一为 UTF-8 以完整打印功能定义内容
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 将项目根目录加入模块搜索路径，保证在任意工作目录下均可运行
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agents import AGENTS  # noqa: E402
from core.logging import LLMInteractionLogger  # noqa: E402

# LangGraph 图（supervisor 已从 YAML 加载为编译后的图）
supervisor_agent = AGENTS["supervisor_agent"]

# 创建 LLM 交互日志记录器，日志文件输出到项目根 logs/ 目录
LOG_PATH = PROJECT_ROOT / "logs" / "llm_trace.log"
llm_logger = LLMInteractionLogger(LOG_PATH, verbose=True)

# 多轮记忆：同一个 thread_id 复用 MemorySaver 中的历史状态
THREAD_CONFIG = {"configurable": {"thread_id": "test-aeb"}, "callbacks": [llm_logger]}


def test_expand_aeb_feature():
    user_input = "扩写AEB功能"

    print("=" * 70)
    print(f"用户输入：{user_input}")
    print(f"LLM 交互日志：{LOG_PATH}")
    print(f"thread_id：{THREAD_CONFIG['configurable']['thread_id']}")
    print("=" * 70)

    # 调用 LangGraph 图：传入 messages + thread_id
    result = supervisor_agent.invoke(
        {"messages": [HumanMessage(content=user_input)]},
        config=THREAD_CONFIG,
    )

    # 从 state 中读取 final_output
    answer = result.get("final_output") or ""

    # 也检查 messages 中最后的 AIMessage
    if not answer and result.get("messages"):
        last_msg = result["messages"][-1]
        answer = getattr(last_msg, "content", str(last_msg))

    print("=" * 70)
    print("中枢智能体最终输出：")
    print("=" * 70)
    print(answer)

    # # 基础断言：确认子智能体被调度且按技能模板产出
    # assert answer, "中枢智能体未返回内容"
    # assert "功能概述" in answer, "输出未包含功能定义模板章节（功能概述）"
    # assert "触发条件" in answer, "输出未包含触发条件章节"

    # 打印 planner 生成的执行计划
    plan = result.get("plan", [])
    print("\n--- Planner 生成的执行计划 ---")
    for i, step in enumerate(plan):
        skill = step.get("skill") or "-"
        print(f"  step_{i+1}: tool={step.get('tool')} skill={skill} mode={step.get('mode')} "
              f"is_final={step.get('is_final')} output_key={step.get('output_key')}")
        if step.get("inputs"):
            print(f"           inputs={step.get('inputs')}")

    # print("\n[测试通过] LangGraph supervisor 已成功规划并调度 product_agent 完成 AEB 功能定义。")


if __name__ == "__main__":
    test_expand_aeb_feature()
