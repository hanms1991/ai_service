"""意图解析：用 LLM 从自然语言提取结构化检索意图。"""
from typing import Any

def parse(ctx, catalog_text: str) -> tuple[dict, dict]:
    """调 LLM 解析用户检索意图。

    返回 (parsed_dict, usage_dict)。
    parsed_dict = {"domain": str, "query_type": str, "params": dict}
    """
    skill_cfg = ctx.skill_cfg or {}
    exec_cfg = skill_cfg.get("execution") or {}
    plan_cfg = exec_cfg.get("plan") or {}
    # 装配 system prompt 并替换 {adapter_catalog}
    system_prompt = ctx.build_stage_prompt(plan_cfg)
    system_prompt = system_prompt.replace("{adapter_catalog}", catalog_text)
    # 用户原文作为 user message
    user_query = str((ctx.inputs or {}).get("query") or "").strip()
    user_message = f"用户检索意图：{user_query}"
    max_tokens = plan_cfg.get("max_tokens") or 4096
    parsed, response = ctx.invoke_json_stage(
        ctx.bind_json_model(max_tokens),
        system_prompt,
        user_message,
        stage_name="检索意图解析",
        max_tokens=max_tokens,
        runnable_config=ctx.runnable_config,
    )
    usage = ctx.extract_usage(response)
    # 规范化
    domain = str(parsed.get("domain") or "").strip()
    query_type = str(parsed.get("query_type") or "").strip()
    params = parsed.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    return {"domain": domain, "query_type": query_type, "params": params}, usage
