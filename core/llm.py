"""LLM 配置模块。

从项目根目录 .env 读取 LLM（OpenAI 兼容协议）接口配置。

支持在前端切换模型。预设来源按优先级：
  1. ``LLM_PRESETS_FILE``：指向一个 JSON 文件（默认项目根目录 ``llm_presets.json``），
     每个元素含 name/label/base_url/model/api_key(或api_key_env)/temperature/enabled；
  2. ``LLM_PRESETS``：内联 JSON 数组（.env 中一行），格式同上；
  3. 回退到旧字段 ``LLM_MODEL``/``LLM_BASE_URL``/``LLM_API_KEY``/``LLM_TEMPERATURE``
     构建名为 ``default`` 的单预设。
  - ``enabled``：布尔值，缺省 True。设为 false 时该预设不在前端下拉中显示，
    也不允许切换到；若当前模型被禁用，自动回退到第一个启用项。
  - API Key 推荐用 ``api_key_env`` 引用 .env 中的环境变量名（如 ``"api_key_env":
    "LLM_API_KEY_GLM"``），密钥本身留在已被 .gitignore 忽略的 .env 中，JSON 文件
    可安全分享/提交；``api_key`` 字面量仅作向后兼容。
  - ``LLM_DEFAULT_MODEL`` 指定启动时选中的预设 name（缺省取第一个启用项）。

对外暴露：
  - ``list_models()``        → 预设列表（不含 api_key）
  - ``get_current_model_name()`` / ``set_current_model(name)``
  - ``get_model()``          → 当前生效的底层 ChatOpenAI
  - ``model``                → 代理 Runnable，所有调用转发到当前模型，
    ``bind``/``bind_tools``/``with_structured_output`` 绑定到代理自身，
    调用时才解析当前模型，因此已构建的 agent 也能动态切换。
"""
from __future__ import annotations

import json
import os
from typing import Any

from dotenv import load_dotenv
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI

# 本文件位于 <项目根目录>/core/llm.py，.env 在项目根目录下
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

# 全局默认参数（所有预设共用）
_DEFAULT_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "8192"))
_DEFAULT_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "120"))


def _build_presets() -> list[dict[str, Any]]:
    """解析 .env 中的模型预设列表。优先级：文件 > 内联 > 旧字段。"""
    # 1. JSON 文件（默认项目根 llm_presets.json）
    presets_file = os.getenv("LLM_PRESETS_FILE", "").strip()
    if not presets_file:
        presets_file = os.path.join(_PROJECT_ROOT, "llm_presets.json")
    elif not os.path.isabs(presets_file):
        presets_file = os.path.join(_PROJECT_ROOT, presets_file)
    presets: list[dict[str, Any]] = []
    if os.path.isfile(presets_file):
        try:
            with open(presets_file, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("name") and item.get("model"):
                        presets.append(item)
        except (json.JSONDecodeError, OSError):
            pass
    # 2. 内联 JSON
    if not presets:
        raw = os.getenv("LLM_PRESETS", "").strip()
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("name") and item.get("model"):
                            presets.append(item)
            except json.JSONDecodeError:
                pass
    # 3. 回退：旧字段构成的单预设
    if not presets:
        legacy = {
            "name": "default",
            "label": os.getenv("LLM_MODEL", "default"),
            "base_url": os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"),
            "model": os.getenv("LLM_MODEL", "deepseek-chat"),
            "api_key": os.getenv("LLM_API_KEY", "EMPTY"),
            "temperature": float(os.getenv("LLM_TEMPERATURE", "0.7")),
        }
        presets.append(legacy)
    return presets


_PRESETS: dict[str, dict[str, Any]] = {p["name"]: p for p in _build_presets()}
_INSTANCES: dict[str, ChatOpenAI] = {}


def _resolve_api_key(preset: dict[str, Any]) -> str:
    """解析预设的 API Key：优先 api_key_env 引用环境变量，回退 api_key 字面量。"""
    env_name = preset.get("api_key_env")
    if env_name:
        val = os.getenv(str(env_name))
        if val:
            return val
    return preset.get("api_key") or "EMPTY"


def _make_instance(preset: dict[str, Any], *, streaming: bool = True) -> ChatOpenAI:
    return ChatOpenAI(
        model=preset["model"],
        api_key=_resolve_api_key(preset),
        base_url=preset["base_url"],
        temperature=float(preset.get("temperature", 0.7)),
        max_tokens=int(preset.get("max_tokens") or _DEFAULT_MAX_TOKENS),
        timeout=float(preset.get("timeout") or _DEFAULT_TIMEOUT),
        streaming=streaming,
    )


# 流式实例缓存（供 _astream / astream_events 路径使用）
_INSTANCES: dict[str, ChatOpenAI] = {}
# 非流式实例缓存（供 _generate / _agenerate / ainvoke 路径使用，
# 避免 streaming=True 时同步调用收到 Stream 对象报错）
_INSTANCES_NOSTREAM: dict[str, ChatOpenAI] = {}


def _get_instance(name: str, *, streaming: bool = True) -> ChatOpenAI:
    cache = _INSTANCES if streaming else _INSTANCES_NOSTREAM
    if name not in cache:
        preset = _PRESETS.get(name)
        if preset is None:
            raise KeyError(f"未知模型预设: {name!r}")
        cache[name] = _make_instance(preset, streaming=streaming)
    return cache[name]


def _is_enabled(preset: dict[str, Any]) -> bool:
    """预设是否启用：enabled 缺省为 True（未声明即启用）。"""
    return bool(preset.get("enabled", True))


def _first_enabled_name() -> str | None:
    for p in _PRESETS.values():
        if _is_enabled(p):
            return p["name"]
    return None


# 启动时选中的预设（仅从启用项中选择；默认项被禁用时回退到第一个启用项）
_current_name: str = os.getenv("LLM_DEFAULT_MODEL") or ""
if _current_name not in _PRESETS or not _is_enabled(_PRESETS[_current_name]):
    _current_name = _first_enabled_name() or next(iter(_PRESETS))


def list_models() -> list[dict[str, Any]]:
    """返回已启用的预设列表（不包含 api_key）。"""
    return [
        {
            "name": p["name"],
            "label": p.get("label") or p["model"],
            "model": p["model"],
            "base_url": p.get("base_url", ""),
            "temperature": p.get("temperature"),
        }
        for p in _PRESETS.values()
        if _is_enabled(p)
    ]


def get_current_model_name() -> str:
    """返回当前生效的预设名；若当前预设已被禁用，自动回退到第一个启用项。"""
    global _current_name
    cur = _PRESETS.get(_current_name)
    if cur is None or not _is_enabled(cur):
        fallback = _first_enabled_name()
        if fallback is not None:
            _current_name = fallback
    return _current_name


def set_current_model(name: str) -> str:
    """切换当前模型（仅允许启用项）；返回生效后的预设名。"""
    global _current_name
    preset = _PRESETS.get(name)
    if preset is None:
        raise KeyError(f"未知模型预设: {name!r}")
    if not _is_enabled(preset):
        raise ValueError(f"模型预设 {name!r} 已禁用，无法切换")
    _current_name = name
    return _current_name


def get_model() -> ChatOpenAI:
    """当前生效的底层 ChatOpenAI 实例（流式）。"""
    return _get_instance(_current_name, streaming=True)


def _get_nostream_model() -> ChatOpenAI:
    """当前生效的底层 ChatOpenAI 实例（非流式）。

    供 _ModelSwitchProxy 的 _generate / _agenerate 使用，避免 streaming=True
    导致同步调用（graph.ainvoke / model.invoke）收到 Stream 对象报错。
    """
    return _get_instance(_current_name, streaming=False)


class _ModelSwitchProxy(BaseChatModel):
    """代理 ChatModel：所有调用转发到当前生效的底层模型。

    bind/bind_tools/with_structured_output 绑定到代理自身，调用时才解析当前模型，
    因此已构建的 agent（含工具绑定）也能随切换生效。
    """

    @property
    def _llm_type(self) -> str:
        return get_model()._llm_type

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return get_model()._identifying_params

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        # 用非流式实例，避免 streaming=True 时收到 Stream 对象
        return _get_nostream_model()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        # 用非流式实例，避免 streaming=True 时收到 Stream 对象
        return await _get_nostream_model()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ):
        return get_model()._stream(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ):
        async for chunk in get_model()._astream(messages, stop=stop, run_manager=run_manager, **kwargs):
            yield chunk

    def with_structured_output(self, schema: Any, **kwargs: Any):
        """委托给当前模型（规划层等按次调用，取当前模型的原生实现）。

        用非流式实例构建，避免 streaming=True 导致 JSON mode 同步调用报错。
        """
        return _get_nostream_model().with_structured_output(schema, **kwargs)


# 全局代理：所有 from core.llm import model 与 _resolve_model 都拿到它
model = _ModelSwitchProxy()
