"""端到端测试：supervisor_agent 调度 product_agent 完成 AEB 功能定义。

运行方式（在项目根目录下）：
    python test/test.py

预期链路：
    用户 "定义AEB功能"
      -> supervisor_agent 判断意图，调用工具 product_expert
         -> product_agent 调用 load_skill(feature_definition) 加载技能
         -> product_agent 按技能模板产出 AEB 功能定义
      -> supervisor_agent 将结果作为最终答复返回
"""
import sys
from pathlib import Path

# Windows 控制台默认 GBK，统一为 UTF-8 以完整打印功能定义内容
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 将项目根目录加入模块搜索路径，保证在任意工作目录下均可运行
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agents import AGENTS  # noqa: E402

supervisor_agent = AGENTS["platform_supervisor"]


def test_define_aeb_feature():
    user_input = "定义AEB功能"

    print("=" * 70)
    print(f"用户输入：{user_input}")
    print("=" * 70)

    result = supervisor_agent.invoke(
        {"messages": [{"role": "user", "content": user_input}]}
    )
    answer = result["messages"][-1].content

    print("=" * 70)
    print("中枢智能体最终输出：")
    print("=" * 70)
    print(answer)

    # 基础断言：确认子智能体被调度且按技能模板产出
    assert answer, "中枢智能体未返回内容"
    assert "功能概述" in answer, "输出未包含功能定义模板章节（功能概述）"
    assert "触发条件" in answer, "输出未包含触发条件章节"
    print("\n[测试通过] supervisor 已成功调度 product_agent 完成 AEB 功能定义。")


if __name__ == "__main__":
    test_define_aeb_feature()
