"""auto-kb 知识库客户端 —— 技能级知识注入（通道一）的检索封装。

对接独立部署的 auto-kb 服务（FastAPI + pgvector，默认 8200 端口），
为技能执行提供「历史项目参考」动态检索能力：

  - 配置：agents/configs/knowledge.yaml（服务地址/鉴权/领域→库名映射），模块级缓存
  - 库名 → kb_id：首次检索时 GET /api/kb 解析并进程内缓存
  - 检索：POST /api/retrieve，同步 httpx（与 _execute_skill_core 同步链路一致）
  - 软降级：服务未配置/不可达/解析失败一律返回空结果并记日志，
    绝不抛异常阻断技能执行——知识库是增强依赖，不是硬依赖
  - 注入段落格式化：带溯源（文件名/分层/sheet行号/页码/章节）+ 大小上限

使用方（capability_registry._resolve_knowledge_block）只需：
    chunks = retrieve_knowledge(domain, query, ...)
    block = format_knowledge_block(chunks, domain)
"""
from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

logger = logging.getLogger(__name__)

# knowledge.yaml 位置：项目根/agents/configs/knowledge.yaml
CONFIG_PATH = Path(__file__).resolve().parent.parent / "agents" / "configs" / "knowledge.yaml"
_PROJECT_ROOT = CONFIG_PATH.parent.parent.parent

# 确保进程加载过项目根 .env（kb_client 可能先于 core.llm 被导入；override=False
# 不覆盖进程中已显式设置的环境变量）
try:
    from dotenv import load_dotenv

    load_dotenv(_PROJECT_ROOT / ".env", override=False)
except Exception:  # noqa: BLE001 —— .env 不存在/未装 python-dotenv 时退回纯环境变量
    pass

# ── 注入段落大小控制（防止撑爆 LLM 上下文） ──
MAX_CHUNK_CHARS = 800      # 单块内容最大字符数（超出截断）
MAX_TOTAL_CHARS = 8000     # 注入段落正文总字符上限（超出丢弃后续块）
DEFAULT_TOP_K = 5

# 分层注入（map_reduce 规划阶段）：一次大召回后按 meta.layer 客户端分组
LAYER_LABELS = {
    "function_list": "功能清单层",
    "failure_mode": "失效模式层",
    "hara_event": "HARA 危害事件层",
    "safety_goal": "安全目标层",
}
MAX_LAYER_CHUNK_CHARS = 1200   # 分层段落单块上限
MAX_LAYER_TOTAL_CHARS = 20000  # 分层段落总上限

# ── 模块级缓存（与 capability_registry 缓存风格一致） ──
_cache_lock = threading.Lock()
_config_cache: dict[str, Any] | None = None
_kb_name_to_id: dict[str, int] | None = None


# ════════════════════════════════════════════════════════════════
# 配置加载
# ════════════════════════════════════════════════════════════════

def load_kb_config(refresh: bool = False) -> dict[str, Any]:
    """加载 knowledge.yaml（模块级缓存）。

    文件缺失或 base_url 留空均视为「功能关闭」，返回的配置让所有调用安全走空路径。
    """
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


_ENV_REF_PATTERN = re.compile(r"^\s*(?:\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*))\s*$")
_BARE_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _resolve_config_value(raw: Any) -> str:
    """解析 knowledge.yaml 中的环境变量引用。

    支持三种写法：
      - 字面值：       http://127.0.0.1:8200
      - 显式引用：     ${KB_BASE_URL} 或 $KB_BASE_URL
      - 裸环境变量名： KB_BASE_URL（环境中存在该变量时展开，否则按字面值处理）
    """
    value = str(raw or "").strip()
    if not value:
        return ""
    m = _ENV_REF_PATTERN.match(value)
    if m:
        return os.getenv(m.group(1) or m.group(2), "").strip()
    if _BARE_ENV_NAME_PATTERN.match(value) and os.getenv(value) is not None:
        return os.getenv(value, "").strip()
    return value


def kb_enabled() -> bool:
    """知识库功能是否启用（base_url 解析后为合法 http(s) 地址才视为启用）。"""
    url = _service_base_url()
    return bool(url) and urlparse(url).scheme in ("http", "https")


def _service_base_url() -> str:
    raw = (load_kb_config().get("kb_service") or {}).get("base_url")
    return _resolve_config_value(raw).rstrip("/")


def _auth_headers() -> dict[str, str]:
    """X-API-Key 鉴权头；key 从 knowledge.yaml 指定的环境变量读取（不落代码）。"""
    cfg = load_kb_config().get("kb_service") or {}
    env_name = str(cfg.get("api_key_env") or "KB_API_KEY")
    key = os.getenv(env_name, "").strip()
    return {"X-API-Key": key} if key else {}


def _timeout_seconds() -> float:
    cfg = load_kb_config().get("kb_service") or {}
    try:
        return float(cfg.get("timeout_seconds") or 5)
    except (TypeError, ValueError):
        return 5.0


# ════════════════════════════════════════════════════════════════
# 库名 → kb_id 解析（带进程内缓存）
# ════════════════════════════════════════════════════════════════

def _fetch_kb_name_map() -> dict[str, int]:
    """GET /api/kb → {库名: id}。网络/鉴权失败向上抛 httpx 异常（由调用方降级）。"""
    resp = httpx.get(
        f"{_service_base_url()}/api/kb",
        headers=_auth_headers(),
        timeout=_timeout_seconds(),
    )
    resp.raise_for_status()
    items = resp.json() or []
    return {str(item["name"]): int(item["id"]) for item in items}


def resolve_kb_ids(domain: str) -> list[int]:
    """领域名 → kb_id 列表（按 knowledge.yaml domains 中的库名映射）。

    库名解析失败或映射缺失返回 []（调用方据此跳过检索，软降级）。
    """
    global _kb_name_to_id
    domains = load_kb_config().get("domains") or {}
    names = domains.get(domain) or []
    if isinstance(names, str):
        names = [names]
    if not names:
        logger.warning("[kb_client] 领域 %r 未在 knowledge.yaml domains 中配置，跳过知识检索", domain)
        return []
    with _cache_lock:
        if _kb_name_to_id is None:
            try:
                _kb_name_to_id = _fetch_kb_name_map()
            except Exception as exc:  # noqa: BLE001 —— 解析失败软降级，下次调用自动重试
                logger.warning("[kb_client] 获取知识库列表失败：%s", exc)
                return []
        name_map = dict(_kb_name_to_id)
    ids = [name_map[n] for n in names if n in name_map]
    missing = [n for n in names if n not in name_map]
    if missing:
        logger.warning("[kb_client] auto-kb 中不存在库名 %s（领域 %r），已忽略", missing, domain)
    return ids


def refresh_kb_id_cache() -> None:
    """强制清空库名缓存（auto-kb 侧建库/改库后可调用）。"""
    global _kb_name_to_id
    with _cache_lock:
        _kb_name_to_id = None


# ════════════════════════════════════════════════════════════════
# 检索与注入格式化
# ════════════════════════════════════════════════════════════════

def retrieve_knowledge(
    domain: str,
    query: str,
    *,
    top_k: int | None = None,
    score_threshold: float | None = None,
    meta_filter: dict[str, Any] | None = None,
    layer: str | None = None,
) -> list[dict[str, Any]]:
    """检索指定领域的知识库，返回分块列表（content/score/filename/meta）。

    layer：可选的分块级分层过滤（meta.layer）。auto-kb 的 meta_filter 只过滤
    文档级元数据，分块级 layer 由本函数在客户端过滤（不增加服务端改动）。

    软降级承诺：任何失败（功能未启用、query 为空、库名不可解析、网络异常、
    响应格式异常）都返回 [] 并记 warning 日志，绝不抛异常阻断技能执行。
    """
    query = (query or "").strip()
    if not query:
        return []
    raw_base_url = str((load_kb_config().get("kb_service") or {}).get("base_url") or "").strip()
    base_url = _service_base_url()
    if raw_base_url and (not base_url or urlparse(base_url).scheme not in ("http", "https")):
        logger.warning(
            "[kb_client] kb_service.base_url=%r 解析后不是有效 http(s) 地址"
            "（检查 knowledge.yaml 拼写与 .env 中对应环境变量是否设置），跳过知识检索",
            raw_base_url,
        )
        return []
    if not base_url:
        return []
    try:
        kb_ids = resolve_kb_ids(domain)
        body: dict[str, Any] = {"query": query, "top_k": int(top_k or DEFAULT_TOP_K)}
        if kb_ids:
            body["kb_ids"] = kb_ids
        else:
            # 库名无法解析时不做全库检索：避免把其他领域知识误注入本技能
            logger.warning("[kb_client] 领域 %r 无可用知识库，跳过检索（query=%r）", domain, query[:50])
            return []
        if score_threshold is not None:
            body["score_threshold"] = float(score_threshold)
        if meta_filter:
            body["meta_filter"] = meta_filter

        resp = httpx.post(
            f"{_service_base_url()}/api/retrieve",
            json=body,
            headers=_auth_headers(),
            timeout=_timeout_seconds(),
        )
        resp.raise_for_status()
        chunks = resp.json()
        if not isinstance(chunks, list):
            logger.warning("[kb_client] /api/retrieve 返回非列表，忽略（domain=%s）", domain)
            return []
        if layer:
            # 分块级 layer 客户端过滤（auto-kb 的 meta_filter 仅过滤文档级元数据）
            chunks = [
                c for c in chunks
                if str((c.get("meta") or {}).get("layer") or "") == layer
            ]
        logger.info(
            "[kb_client] 知识检索命中 %d 块（domain=%s，layer=%s，query=%r）",
            len(chunks), domain, layer or "-", query[:50],
        )
        return chunks
    except Exception as exc:  # noqa: BLE001 —— 软降级兜底，见 docstring
        logger.warning("[kb_client] 知识库检索降级（domain=%s）：%s", domain, exc)
        return []


def _format_source(chunk: dict[str, Any]) -> str:
    """把分块溯源信息格式化为「文件名（分层=xx；Sheet=xx；行=xx…）」。"""
    filename = str(chunk.get("filename") or "未知文件")
    meta = chunk.get("meta") or {}
    parts: list[str] = []
    for key, label in (
        ("layer", "分层"), ("sheet", "Sheet"), ("row", "行"),
        ("page", "页"), ("section", "章节"), ("table_row", "表行"),
    ):
        value = meta.get(key)
        if value not in (None, ""):
            parts.append(f"{label}={value}")
    return f"{filename}（{'；'.join(parts)}）" if parts else filename


def format_knowledge_block(chunks: list[dict[str, Any]], domain: str) -> str:
    """检索结果 → 追加到用户消息的只读注入段落；空结果返回空串。

    段落自带使用约束（参考口径、以用户输入为准、引用注明出处），
    技能 prompt_template 无需为此改动。
    """
    if not chunks:
        return ""
    lines: list[str] = [
        f"\n\n【历史项目参考（检索自「{domain}」知识库，只读；用于对齐历史项目的"
        f"分析口径、命名习惯与产出粒度，禁止照搬历史结论）】"
    ]
    total = 0
    kept = 0
    for chunk in chunks:
        content = str(chunk.get("content") or "").strip()
        if not content:
            continue
        content = content[:MAX_CHUNK_CHARS]
        try:
            score = float(chunk.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        line = f"[{kept + 1}] 来源：{_format_source(chunk)} ｜ 相似度 {score:.2f}\n{content}"
        if total + len(line) > MAX_TOTAL_CHARS:
            break
        lines.append(line)
        total += len(line)
        kept += 1
    if kept == 0:
        return ""
    lines.append(
        "使用要求：以上为历史项目数据，仅供格式与粒度参考；"
        "与用户当前输入冲突时一律以用户输入为准；引用历史结论时注明出处文件名。"
    )
    return "\n".join(lines)


def format_knowledge_layered_block(
    chunks: list[dict[str, Any]],
    layers: list[str],
    domain: str,
) -> str:
    """分层注入：大召回结果按 meta.layer 分段组织（供 map_reduce 规划阶段使用）。

    layers 给定分段顺序（如 function_list/failure_mode/hara_event/safety_goal）；
    不属于任何声明层的块归入「其他」段（若有的话，放在最后）。
    每段独立编号，便于 LLM 在条目 source 中引用「文件 + 分层 + 序号」。
    """
    if not chunks:
        return ""
    grouped: dict[str, list[dict[str, Any]]] = {layer: [] for layer in layers}
    extras: list[dict[str, Any]] = []
    for chunk in chunks:
        layer = str((chunk.get("meta") or {}).get("layer") or "")
        if layer in grouped:
            grouped[layer].append(chunk)
        else:
            extras.append(chunk)
    if extras:
        grouped["__other__"] = extras

    lines: list[str] = [
        f"\n\n【历史项目参考（检索自「{domain}」知识库，按数据层分组，只读）】"
    ]
    total = 0
    kept_any = False
    for layer, layer_chunks in grouped.items():
        if not layer_chunks:
            continue
        label = LAYER_LABELS.get(layer, layer)
        lines.append(f"\n── {label} ──")
        kept = 0
        for chunk in layer_chunks:
            content = str(chunk.get("content") or "").strip()
            if not content:
                continue
            content = content[:MAX_LAYER_CHUNK_CHARS]
            try:
                score = float(chunk.get("score") or 0)
            except (TypeError, ValueError):
                score = 0.0
            line = (
                f"[{layer}#{kept + 1}] 来源：{_format_source(chunk)} ｜ 相似度 {score:.2f}\n"
                + content
            )
            if total + len(line) > MAX_LAYER_TOTAL_CHARS:
                lines.append("（本层剩余历史条目因篇幅省略）")
                break
            lines.append(line)
            total += len(line)
            kept += 1
        if kept:
            kept_any = True
    if not kept_any:
        return ""
    lines.append(
        "\n使用要求：①以上为历史项目真实条目，本次分析应优先沿用/改编其中与当前相关项"
        "匹配的功能、失效模式、危害事件、S/E/C 评级与安全目标，沿用条目必须在 source 中"
        "注明来源文件与历史标识；②历史不足或不适用处才允许新增，新增条目标 source.type=new；"
        "③即使沿用历史 ASIL，也必须完整输出 S/E/C 取值与理由以便审计。"
    )
    return "\n".join(lines)
