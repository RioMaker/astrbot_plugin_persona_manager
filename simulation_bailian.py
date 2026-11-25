"""DashScope generation and AstrBot adapters for the configured Beijing workspace."""

import asyncio
import os

import aiohttp

from .settings import (
    BAILIAN_GROUP_CHAT_MODEL,
    BAILIAN_KEY_FORMAT,
    Settings,
    configured_bailian_key,
)

MISSING_KEY = "请在「设置与方向 → 模型与预算」填写并保存「百炼试聊 API Key」。"


def is_group_chat_v2(workspace, model_code):
    return (
        workspace == Settings.bailian_workspace_id
        and model_code == BAILIAN_GROUP_CHAT_MODEL
    )


def api_key(config):
    return (
        configured_bailian_key(config)
        or os.environ.get("DASHSCOPE_API_KEY", "").strip()
    )


def validate_key(key):
    if not key:
        raise ValueError(MISSING_KEY)
    if not BAILIAN_KEY_FORMAT.fullmatch(key):
        raise ValueError("百炼试聊 API Key 格式无效，请检查插件密钥配置")


def create_agent_provider(config, workspace, model, provider_id, *, turn=None):
    key = api_key(config)
    validate_key(key)
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

    provider_class = ProviderOpenAIOfficial
    if turn is not None:
        from .trial_provider import TrialProvider

        provider_class = TrialProvider
    provider = provider_class(
        {
            "id": provider_id,
            "type": "openai_chat_completion",
            "model": model,
            "key": [key],
            "api_base": f"https://{workspace}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "modalities": ["text", "tool_use"],
            "timeout": 360,
            "custom_extra_body": {"max_tokens": 512, "enable_thinking": False},
        },
        {},
    )
    if turn is not None:
        provider.turn = turn
    return provider


async def generate(workspace, model_code, system, messages, max_tokens, key):
    validate_key(key)
    url = (
        f"https://{workspace}.cn-beijing.maas.aliyuncs.com"
        "/api/v1/services/aigc/text-generation/generation"
    )
    parameters = {
        "result_format": "message",
        "max_tokens": max_tokens,
        "enable_thinking": False,
    }
    if is_group_chat_v2(workspace, model_code):
        # The reasoning budget and sampling match this deployment's evaluated chat profile.
        parameters.update(
            enable_thinking=True, max_tokens=1536, temperature=0.6, top_p=0.95, top_k=20
        )
    body = {
        "model": model_code,
        "input": {"messages": [{"role": "system", "content": system}, *messages]},
        "parameters": parameters,
    }
    timeout = aiohttp.ClientTimeout(total=120, sock_connect=15)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout, trust_env=True) as client,
            client.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                json=body,
                allow_redirects=False,
            ) as response,
        ):
            if response.status != 200:
                hint = {
                    400: "请核对 ModelCode 和消息长度",
                    401: "请核对北京业务空间的 API Key",
                    403: "请核对业务空间权限和服务器出口 IP 白名单",
                    404: "请核对业务空间、ModelCode 和部署状态",
                    429: "请求受到限流，请稍后重试",
                }.get(response.status, "请检查服务状态后重试")
                raise ValueError(f"阿里云百炼返回 HTTP {response.status}，{hint}")
            try:
                data = await response.json()
                text = data["output"]["choices"][0]["message"]["content"]
            except (
                aiohttp.ContentTypeError,
                ValueError,
                KeyError,
                IndexError,
                TypeError,
            ):
                raise ValueError("阿里云百炼返回了无法识别的回复格式") from None
    except asyncio.TimeoutError:
        raise ValueError("等待阿里云百炼回复超时，请稍后重试") from None
    except aiohttp.ClientError:
        raise ValueError(
            "无法连接阿里云百炼，请检查服务器网络和北京业务空间 ID"
        ) from None
    if not isinstance(text, str) or not text.strip():
        raise ValueError("阿里云百炼没有返回正文，请重试")
    return text.strip()
