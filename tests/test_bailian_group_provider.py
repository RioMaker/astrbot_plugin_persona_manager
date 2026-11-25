"""Exercise the real AstrBot provider resolver and HTTP payload without spending tokens."""

import asyncio
import inspect
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_persona_manager.bailian_group_chat import GroupTrialProviders
from astrbot_plugin_persona_manager.settings import (
    BAILIAN_GROUP_CHAT_MODEL,
    BAILIAN_LIUYAO_MODEL,
)
from astrbot_plugin_persona_manager.tests.astrbot_stub import MockEvent


def test_dashboard_discovers_trial_page(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    pytest.importorskip("astrbot", reason="需安装 AstrBot 才能运行页面发现测试")
    root = Path(__file__).resolve().parents[1]
    plugin = SimpleNamespace(root_dir_name=root.name, reserved=False)
    manager = SimpleNamespace(plugin_store_path=str(root.parent))
    try:
        from astrbot.dashboard.services.plugin_page_service import PluginPageService
    except ModuleNotFoundError as exc:
        if exc.name not in {
            "astrbot.dashboard.services",
            "astrbot.dashboard.services.plugin_page_service",
        }:
            raise
        from astrbot.dashboard.routes.plugin import PluginRoute

        service = object.__new__(PluginRoute)
        service.plugin_manager = manager
        pages = asyncio.run(service._discover_plugin_pages(plugin))
    else:
        pages = asyncio.run(PluginPageService(manager).discover_plugin_pages(plugin))
    assert {"manage", "测试群调用"} <= {page.name for page in pages}


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
        step = provider.turn.detail()["steps"][-1]
        assert step["usage"]["total_tokens"] is None
        assert step["prompt"] == f"[user]\n{group}"
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


@pytest.mark.parametrize("stream", [False, True])
def test_traced_adapter_records_each_wire_prompt_and_reported_usage(
    tmp_path, monkeypatch, stream
):
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    pytest.importorskip("astrbot", reason="需安装 AstrBot 才能运行真实提供商集成测试")
    import httpx
    from astrbot.core.agent.tool import FunctionTool, ToolSet
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    from astrbot_plugin_persona_manager.simulation_bailian import create_agent_provider
    from astrbot_plugin_persona_manager.trial_trace import TrialHistory, prompt_text

    source = {"platform_id": "qq", "group_id": "234567", "persona_id": "可可子"}
    turn = TrialHistory().begin(source, "trial", "练习记录", time.monotonic())
    requests = []
    tool_call = {
        "id": "c1",
        "type": "function",
        "function": {"name": "lookup", "arguments": '{"item":"42"}'},
    }

    def reply(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["stream"] == stream
        assert turn.detail()["steps"][-1]["prompt"] == prompt_text(body["messages"])
        assert json.loads(turn.detail()["steps"][-1]["tools"]) == body["tools"]
        head = {"id": "resp", "model": "trial", "created": 1}
        first = len(requests) == 1
        usage = {
            "prompt_tokens": 12 if first else 18,
            "completion_tokens": 3,
            "total_tokens": 15 if first else 21,
        }
        message = {
            "role": "assistant",
            **({"tool_calls": [tool_call]} if first else {"content": "查到了 42"}),
        }
        finish = "tool_calls" if first else "stop"
        if not stream:
            return httpx.Response(
                200,
                json={
                    **head,
                    "object": "chat.completion",
                    "usage": usage,
                    "choices": [
                        {"index": 0, "message": message, "finish_reason": finish}
                    ],
                },
            )
        if first:
            message["tool_calls"] = [{"index": 0, **tool_call}]

        def chunk(choices, **extra):
            data = {
                **head,
                "object": "chat.completion.chunk",
                "choices": choices,
                **extra,
            }
            return "data: " + json.dumps(data) + "\n\n"

        data = chunk([{"index": 0, "delta": message, "finish_reason": None}])
        data += chunk([{"index": 0, "delta": {}, "finish_reason": finish}])
        data += chunk([], usage=usage) + "data: [DONE]\n\n"
        return httpx.Response(
            200, text=data, headers={"Content-Type": "text/event-stream"}
        )

    monkeypatch.setattr(
        ProviderOpenAIOfficial,
        "_create_http_client",
        lambda *args: httpx.AsyncClient(transport=httpx.MockTransport(reply)),
    )
    tools = ToolSet()
    tools.add_tool(
        FunctionTool(
            name="lookup",
            description="查练习记录",
            parameters={"type": "object", "properties": {"item": {"type": "string"}}},
        )
    )

    async def run():
        provider = create_agent_provider(
            {"bailian_api_key": "private-key-for-test"},
            "ws-example",
            "trial",
            "test",
            turn=turn,
        )
        kwargs = dict(system_prompt="人格正文", func_tool=tools)

        async def ask(**extra):
            if stream:
                replies = [
                    r async for r in provider.text_chat_stream(**kwargs, **extra)
                ]
                return replies[-1]
            return await provider.text_chat(**kwargs, **extra)

        try:
            first = await ask(prompt="练习记录")
            assert first.tools_call_name == ["lookup"]
            final = await ask(
                contexts=[
                    {"role": "user", "content": "练习记录"},
                    {"role": "assistant", "content": None, "tool_calls": [tool_call]},
                    {"role": "tool", "tool_call_id": "c1", "content": "练习完成"},
                ]
            )
            assert final.completion_text == "查到了 42"
        finally:
            await provider.terminate()

    asyncio.run(run())
    trace = turn.detail()
    assert (
        trace["reported_tokens"] == 36
        and trace["calls"] == trace["reported_calls"] == 2
    )
    steps = trace["steps"][1:]
    assert [s["usage"]["input_tokens"] for s in steps] == [12, 18]
    assert all(
        s["usage"]["output_tokens"] == 3 and s["status"] == "done" for s in steps
    )
    assert "lookup" in steps[0]["output"] and "练习完成" in steps[1]["prompt"]
    assert steps[1]["output"] == "查到了 42"
    assert "private-key-for-test" not in str(trace)
