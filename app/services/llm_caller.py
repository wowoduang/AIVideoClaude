"""
llm_caller.py
-------------
统一的 LLM 同步调用辅助模块。

优先使用 LiteLLM provider（已在 webui.py 注册），
兜底使用 requests 直接调用 OpenAI-compatible API。

供 plot_understanding.py 和 generate_narration_script.py 使用。
"""
from __future__ import annotations

import json
import re
from typing import Optional
from loguru import logger


def call_llm_sync(
    system: str,
    user: str,
    api_key: str = "",
    base_url: str = "",
    model: str = "",
    temperature: float = 0.3,
    timeout: int = 180,
) -> str:
    """
    同步调用 LLM，自动选择最佳方式：
    1. LiteLLM provider（已注册时优先使用）
    2. 直接 HTTP 请求兜底
    """
    # 方式一：通过已注册的 LiteLLM provider
    result = _try_litellm_provider(system, user, model, temperature)
    if result:
        return result

    # 方式二：直接 HTTP 调用（兜底）
    return _try_direct_http(system, user, api_key, base_url, model, temperature, timeout)


def get_llm_config_from_app_config():
    """
    从 app.config 读取 LLM 配置，兼容 litellm / openai / deepseek 等多种 provider。
    返回 (api_key, base_url, model)
    """
    try:
        from app.config import config
        provider = config.app.get("text_llm_provider", "litellm").lower()

        # litellm provider 的配置 key 是 text_litellm_*
        api_key = (
            config.app.get(f"text_{provider}_api_key")
            or config.app.get("text_litellm_api_key")
            or ""
        )
        model = (
            config.app.get(f"text_{provider}_model_name")
            or config.app.get("text_litellm_model_name")
            or ""
        )
        base_url = (
            config.app.get(f"text_{provider}_base_url")
            or config.app.get("text_litellm_base_url")
            or ""
        )
        return api_key, base_url, model
    except Exception as e:
        logger.warning(f"读取 LLM 配置失败: {e}")
        return "", "", ""


def _try_litellm_provider(system: str, user: str, model: str, temperature: float) -> str:
    """尝试使用已注册的 LiteLLM TextProvider（同步包装异步调用）"""
    try:
        import asyncio
        from app.services.llm.manager import LLMServiceManager
        if not LLMServiceManager.is_registered():
            return ""
        provider = LLMServiceManager.get_text_provider()
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                provider.generate_text(
                    prompt=user,
                    system_prompt=system,
                    temperature=temperature,
                )
            )
            return str(result or "")
        finally:
            loop.close()
    except Exception as e:
        logger.debug(f"LiteLLM provider 调用失败，尝试直接 HTTP: {e}")
        return ""


def _try_direct_http(
    system: str, user: str, api_key: str, base_url: str, model: str,
    temperature: float, timeout: int
) -> str:
    """直接 HTTP 调用 OpenAI-compatible API（兜底）"""
    try:
        import requests
    except ImportError:
        return ""

    if not api_key or not model:
        logger.warning("LLM 配置不完整（缺少 api_key 或 model），跳过调用")
        return ""

    # 处理 base_url
    if not base_url:
        # 根据 model 前缀推断 base_url
        if model.startswith("deepseek/"):
            base_url = "https://api.deepseek.com/v1"
        elif model.startswith("gemini/"):
            base_url = "https://generativelanguage.googleapis.com/v1beta"
        elif model.startswith("qwen/"):
            base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        else:
            base_url = "https://api.openai.com/v1"

    # 去掉 model 的 provider 前缀（直接 HTTP 调用不需要）
    clean_model = model.split("/", 1)[-1] if "/" in model else model

    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": clean_model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"LLM HTTP 调用失败: {e}")
        return ""
