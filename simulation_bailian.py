"""DashScope text generation for the Dashboard simulator."""

import asyncio
import os

import aiohttp

from .settings import BAILIAN_KEY_FORMAT, configured_bailian_key

MISSING_KEY = "请在「设置与方向 → 模型与预算」填写并保存「百炼试聊 API Key」。"


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


async def generate(workspace, model_code, system, messages, max_tokens, key):
    validate_key(key)
    url = (
        f"https://{workspace}.cn-beijing.maas.aliyuncs.com"
        "/api/v1/services/aigc/text-generation/generation"
    )
    body = {
        "model": model_code,
        "input": {"messages": [{"role": "system", "content": system}, *messages]},
        "parameters": {
            "result_format": "message",
            "max_tokens": max_tokens,
            "enable_thinking": False,
        },
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
