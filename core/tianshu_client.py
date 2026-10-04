"""天枢业务后端客户端 —— Agent 与天枢业务后端的确定性 HTTP 交互。

与 auto-kb（core/kb_client.py）同构：独立 HTTP 客户端模块，在技能 prepare 钩子
或执行内核中确定性调用，不注册为 LangChain tool（LLM 无需决策是否调用）。

当前提供：
  - fetch_uc_template(scene, reference_data)：查询 UC 模板（12 字段定义 + candidate_schema）

后续可扩展：架构生成模板、需求追溯查询等。

降级策略：服务未配置/不可达/鉴权失败时，返回内置默认模板，不阻断技能执行。
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "agents" / "configs" / "tianshu_backend.yaml"
_PROJECT_ROOT = CONFIG_PATH.parent.parent.parent

# 确保进程加载过项目根 .env
try:
    from dotenv import load_dotenv

    load_dotenv(_PROJECT_ROOT / ".env", override=False)
except Exception:  # noqa: BLE001
    pass


# ════════════════════════════════════════════════════════════════
# 内置默认 UC 模板（拉取失败时的降级兜底）
# 与天枢后端 /v1/agent/uc-template 返回结构一致；字段当前稳定。
# ════════════════════════════════════════════════════════════════
_DEFAULT_UC_TEMPLATE: dict[str, Any] = {
    "scene": "FROM_PRD_CREATE_UC",
    "template_id": "uc-default",
    "template_version": "1",
    "fields": [
        {"name": "name", "label": "用例名称", "value_type": "string", "required": True, "generated_by": "agent"},
        {"name": "description", "label": "用例描述", "value_type": "string", "required": True, "generated_by": "agent"},
        {"name": "number", "label": "用例编号", "value_type": "string", "required": False, "generated_by": "backend",
         "description": "正式保存时由后端生成；Agent候选必须省略，不返回空编号或假编号。"},
        {"name": "actors", "label": "参与者", "value_type": "string", "required": True, "generated_by": "agent"},
        {"name": "preconditions", "label": "前置条件", "value_type": "string", "required": True, "generated_by": "agent"},
        {"name": "scenario", "label": "场景", "value_type": "object", "required": True, "generated_by": "agent",
         "children": [
             {"name": "main_flow", "label": "主事件流", "value_type": "string", "required": True, "generated_by": "agent"},
             {"name": "branches", "label": "分支事件流", "value_type": "array", "required": True, "generated_by": "agent",
              "description": "string[]，按PRD确定数量，可为空，不固定4条；最多100条。"},
             {"name": "exceptions", "label": "异常事件流", "value_type": "array", "required": True, "generated_by": "agent",
              "description": "string[]，按PRD确定数量，可为空，不固定5条；最多100条。"},
         ]},
        {"name": "postconditions", "label": "后置条件", "value_type": "string", "required": True, "generated_by": "agent"},
        {"name": "extensions", "label": "扩展用例", "value_type": "string", "required": True, "generated_by": "agent"},
        {"name": "hmi", "label": "HMI交互逻辑", "value_type": "string", "required": True, "generated_by": "agent"},
        {"name": "regulations", "label": "法规需求", "value_type": "string", "required": True, "generated_by": "agent"},
        {"name": "performance", "label": "性能需求", "value_type": "string", "required": True, "generated_by": "agent"},
        {"name": "nonfunctional", "label": "非功能需求", "value_type": "string", "required": True, "generated_by": "agent"},
    ],
    "candidate_schema": {
        "$defs": {
            "UcScenario": {
                "additionalProperties": False,
                "properties": {
                    "main_flow": {"maxLength": 20000, "minLength": 1, "title": "Main Flow", "type": "string"},
                    "branches": {"items": {"type": "string"}, "maxItems": 100, "title": "Branches", "type": "array"},
                    "exceptions": {"items": {"type": "string"}, "maxItems": 100, "title": "Exceptions", "type": "array"},
                },
                "required": ["main_flow", "branches", "exceptions"],
                "title": "UcScenario",
                "type": "object",
            }
        },
        "additionalProperties": False,
        "description": "11 Agent fields; the 12th field (number) is assigned only on formal save.",
        "properties": {
            "name": {"maxLength": 200, "minLength": 1, "title": "Name", "type": "string"},
            "description": {"maxLength": 20000, "minLength": 1, "title": "Description", "type": "string"},
            "actors": {"maxLength": 20000, "title": "Actors", "type": "string"},
            "preconditions": {"maxLength": 20000, "title": "Preconditions", "type": "string"},
            "scenario": {"$ref": "#/$defs/UcScenario"},
            "postconditions": {"maxLength": 20000, "title": "Postconditions", "type": "string"},
            "extensions": {"maxLength": 20000, "title": "Extensions", "type": "string"},
            "hmi": {"maxLength": 20000, "title": "Hmi", "type": "string"},
            "regulations": {"maxLength": 20000, "title": "Regulations", "type": "string"},
            "performance": {"maxLength": 20000, "title": "Performance", "type": "string"},
            "nonfunctional": {"maxLength": 20000, "title": "Nonfunctional", "type": "string"},
        },
        "required": ["name", "description", "actors", "preconditions", "scenario",
                     "postconditions", "extensions", "hmi", "regulations", "performance", "nonfunctional"],
        "title": "UcCandidateContent",
        "type": "object",
    },
}


# ════════════════════════════════════════════════════════════════
# 配置加载
# ════════════════════════════════════════════════════════════════
_cache_lock = threading.Lock()
_config_cache: dict[str, Any] | None = None


def load_tianshu_config(refresh: bool = False) -> dict[str, Any]:
    """加载 tianshu_backend.yaml（模块级缓存）。"""
    global _config_cache
    if _config_cache is not None and not refresh:
        return _config_cache
    with _cache_lock:
        if _config_cache is None or refresh:
            if not CONFIG_PATH.is_file():
                _config_cache = {}
            else:
                data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
                _config_cache = data if isinstance(data, dict) else {}
    return _config_cache


import re  # noqa: E402

_ENV_REF_PATTERN = re.compile(r"^\s*(?:\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*))\s*$")
_BARE_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _resolve_config_value(raw: Any) -> str:
    """解析配置中的环境变量引用（同 kb_client._resolve_config_value）。"""
    value = str(raw or "").strip()
    if not value:
        return ""
    m = _ENV_REF_PATTERN.match(value)
    if m:
        return os.getenv(m.group(1) or m.group(2), "").strip()
    if _BARE_ENV_NAME_PATTERN.match(value) and os.getenv(value) is not None:
        return os.getenv(value, "").strip()
    return value


def _service_base_url() -> str:
    raw = (load_tianshu_config().get("tianshu_backend") or {}).get("base_url")
    return _resolve_config_value(raw).rstrip("/")


def _callback_key() -> str:
    cfg = load_tianshu_config().get("tianshu_backend") or {}
    env_name = str(cfg.get("callback_key_env") or "TIANSHU_AGENT_CALLBACK_KEY")
    return os.getenv(env_name, "").strip()


def _timeout_seconds() -> float:
    cfg = load_tianshu_config().get("tianshu_backend") or {}
    try:
        return float(cfg.get("timeout_seconds") or 10)
    except (TypeError, ValueError):
        return 10.0


def tianshu_enabled() -> bool:
    """天枢后端功能是否启用（base_url 解析后为合法 http(s) 地址）。"""
    url = _service_base_url()
    return bool(url) and urlparse(url).scheme in ("http", "https")


# 模板进程内缓存（按 template_id + version）
_template_cache: dict[str, dict[str, Any]] = {}


# ════════════════════════════════════════════════════════════════
# UC 模板查询
# ════════════════════════════════════════════════════════════════

def fetch_uc_template(reference_data: dict[str, Any] | None) -> dict[str, Any]:
    """查询 UC 模板。

    Args:
        reference_data: 后端传入的参考数据，需包含 uc_template.path 与 uc_template.auth_header。

    Returns:
        模板 dict（含 fields 与 candidate_schema）。拉取失败时返回内置默认模板，绝不抛异常。
    """
    uc_template = (reference_data or {}).get("uc_template") or {}
    path = str(uc_template.get("path") or "").strip()
    auth_header = str(uc_template.get("auth_header") or "X-Agent-Internal-Key").strip()
    template_id = str(uc_template.get("template_id") or "uc-default")
    template_version = str(uc_template.get("template_version") or "1")
    cache_key = f"{template_id}:{template_version}"

    # 进程内缓存命中
    if cache_key in _template_cache:
        return _template_cache[cache_key]

    base_url = _service_base_url()
    if not base_url or urlparse(base_url).scheme not in ("http", "https"):
        logger.warning("[tianshu_client] 天枢后端 base_url 未配置或无效，使用内置默认 UC 模板")
        return _DEFAULT_UC_TEMPLATE

    callback_key = _callback_key()
    if not callback_key:
        logger.warning("[tianshu_client] 天枢后端回调凭证未配置，使用内置默认 UC 模板")
        return _DEFAULT_UC_TEMPLATE

    if not path:
        logger.warning("[tianshu_client] reference_data.uc_template.path 为空，使用内置默认 UC 模板")
        return _DEFAULT_UC_TEMPLATE

    url = f"{base_url}{path}"
    try:
        resp = httpx.get(
            url,
            headers={auth_header: callback_key, "Accept": "application/json"},
            timeout=_timeout_seconds(),
            trust_env=False,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or "fields" not in data:
            logger.warning("[tianshu_client] UC 模板响应结构异常，使用内置默认模板")
            return _DEFAULT_UC_TEMPLATE
        logger.info(
            "[tianshu_client] UC 模板拉取成功（template_id=%s, version=%s, fields=%d）",
            data.get("template_id", template_id),
            data.get("template_version", template_version),
            len(data.get("fields") or []),
        )
        _template_cache[cache_key] = data
        return data
    except Exception as exc:  # noqa: BLE001 —— 软降级，绝不阻断技能执行
        logger.warning("[tianshu_client] UC 模板拉取失败，使用内置默认模板：%s", exc)
        return _DEFAULT_UC_TEMPLATE


def format_uc_template_block(template: dict[str, Any]) -> str:
    """把模板渲染为注入 prompt 的只读说明段落（字段清单 + 约束）。"""
    fields = template.get("fields") or []
    lines = ["\n\n【UC 模板（来自天枢后端，只读；按此结构生成候选，不得增删字段）】"]

    lines.append("正式 UC 共 12 个顶层字段；其中 number 由后端正式保存时生成，Agent 候选必须省略。")
    lines.append("Agent 需返回除 number 外的 11 个字段，scenario 为嵌套对象（main_flow/branches/exceptions）。")
    lines.append("")
    lines.append("字段清单：")
    for f in fields:
        name = f.get("name", "")
        label = f.get("label", "")
        vtype = f.get("value_type", "")
        gen = f.get("generated_by", "")
        req = "必填" if f.get("required") else "可选"
        extra = ""
        if name == "scenario":
            children = f.get("children") or []
            child_names = ", ".join(c.get("name", "") for c in children)
            extra = f"（嵌套子字段：{child_names}）"
        elif name == "number":
            extra = "（后端生成，Agent 不返回）"
        desc = f.get("description") or ""
        desc_part = f" —— {desc}" if desc else ""
        lines.append(f"  - {name}（{label}，{vtype}，{req}，{gen}）{extra}{desc_part}")

    lines.append("")
    lines.append("约束：")
    lines.append("  - 未明确的事实用 <待确认> 或 <待标定> 标记，不编造信号/接口/数值/法规；")
    lines.append("  - branches/exceptions 按 PRD 实际数量生成，可为空数组 []，不固定条数；")
    lines.append("  - 不返回 number、candidate_id、ordinal 或任何正式对象/版本 ID；")
    lines.append("  - 只生成供审阅的候选，不保存、不确认、不调用业务写入接口。")

    return "\n".join(lines)
