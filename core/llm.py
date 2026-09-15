"""LLM 配置模块。

从项目根目录 .env 读取 LLM（OpenAI 兼容协议）接口配置，
全局复用同一个 ChatOpenAI 模型实例供各 Agent 使用。
"""
import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

# 本文件位于 <项目根目录>/core/llm.py，.env 在项目根目录下
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

# LLM 原生兼容 OpenAI 协议，只需替换 base_url
model = ChatOpenAI(
    model=os.getenv("LLM_MODEL", "deepseek-chat"),
    api_key=os.getenv("LLM_API_KEY", "EMPTY"),
    base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"),
    temperature=float(os.getenv("LLM_TEMPERATURE", "0.7")),
    max_tokens=int(os.getenv("LLM_MAX_TOKENS", "8192")),
    timeout=float(os.getenv("LLM_TIMEOUT", "120")),
)
