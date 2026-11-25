"""Exercise the real AstrBot provider resolver and HTTP payload without spending tokens."""

import asyncio
import inspect
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_persona_manager.bailian_group_chat import GroupTrialProviders
from astrbot_plugin_persona_manager.settings import (
    BAILIAN_GROUP_CHAT_MODEL,
    BAILIAN_LIUYAO_MODEL,
)
from astrbot_plugin_persona_manager.tests.astrbot_stub import MockEvent


def test_native_runner_resolves_only_the_selected_events(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    pytest.importorskip("astrbot", reason="需安装 AstrBot 才能运行真实提供商集成测试")
    import httpx
    from astrbot.api.star import Context
    from astrbot.core.astr_main_agent import _select_provider
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    from astrbot_plugin_persona_manager.main import PersonaManagerPlugin

    received = []

    def reply(request):
        body = json.loads(request.content)
        assert (
            request.url
            == "https://ws-trial.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
        )
        assert request.headers["Authorization"] == "Bearer test-token"
        assert body["enable_thinking"] is False
        received.append((body["model"], body["messages"][-1]["content"]))
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "收到"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    monkeypatch.setattr(
        ProviderOpenAIOfficial,
        "_create_http_client",
        lambda *args: httpx.AsyncClient(transport=httpx.MockTransport(reply)),
    )
    normal = object()
    context = object.__new__(Context)
    context.provider_manager = SimpleNamespace(
        inst_map={"default": normal}, provider_insts=[normal]
    )
    context.get_using_provider = lambda **kwargs: normal
    context.get_using_provider_async = AsyncMock(return_value=normal)
    trials = GroupTrialProviders(context, logging.getLogger(__name__))
    plugin = object.__new__(PersonaManagerPlugin)
    plugin.context = context
    plugin.group_trials = trials
    plugin.config = {
        "bailian_api_key": "test-token",
        "bailian_workspace_id": "ws-trial",
        "bailian_group_models": [
            f"qq-main:234567=bailian:ws-trial:{BAILIAN_GROUP_CHAT_MODEL}",
            f"qq-main:456789=bailian:ws-trial:{BAILIAN_LIUYAO_MODEL}",
        ],
    }
    plugin._gate_loaded = lambda event: True
    plugin._source_allowed = AsyncMock(return_value=True)

    async def resolve(group):
        event = MockEvent(group_id=group, extras={"access_gate_original_text": "测试"})
        await plugin.select_local_chat(event)
        result = _select_provider(event, context)
        return await result if inspect.isawaitable(result) else result

    async def turn(group, code):
        provider = await resolve(group)
        assert provider.meta().id == f"bailian:ws-trial:{code}"
        assert await resolve("777888") is normal
        response = await provider.text_chat(prompt=group)
        assert response.completion_text == "收到"
        return provider.client

    async def run():
        clients = await asyncio.gather(
            turn("234567", BAILIAN_GROUP_CHAT_MODEL),
            turn("456789", BAILIAN_LIUYAO_MODEL),
        )
        await trials.close()
        assert context.provider_manager.inst_map == {"default": normal}
        assert context.get_all_providers() == [normal]
        assert all(client.is_closed() for client in clients)

    asyncio.run(run())
    assert sorted(received) == sorted(
        [(BAILIAN_GROUP_CHAT_MODEL, "234567"), (BAILIAN_LIUYAO_MODEL, "456789")]
    )
