import asyncio
from types import SimpleNamespace

import pytest
from astrbot_plugin_persona_manager.tests.astrbot_stub import (
    At,
    MockEvent,
    Plain,
    Reply,
)
from astrbot_plugin_persona_manager.tests.test_integration import entry as entry
from astrbot_plugin_persona_manager.tests.test_integration import request

LOCAL_CONFIG = {
    "local_chat_provider_id": "local/persona-chat",
    "local_chat_groups": ["qq-main:234567"],
}
GATE_MARK = {"access_gate_original_text": "可可子，今天练了新曲！"}


@pytest.mark.parametrize("native_wake", [True, False])
def test_text_chat_uses_local_provider_for_one_event(entry, native_wake):
    async def run():
        async with entry.running(dict(LOCAL_CONFIG, learning_enabled=False)) as env:
            event = MockEvent(
                extras={} if native_wake else GATE_MARK,
                chain=[
                    Reply(chain=[Plain("昨天练了前半段")]),
                    At("345678"),
                    Plain("今天全连了"),
                ],
            )
            if native_wake:
                env.context.stars[0].star_cls._bool = lambda key, default: (
                    False if key == "wake_enabled" else default
                )
            await env.plugin.select_local_chat(event)
            assert event.get_extra("selected_provider") == "local/persona-chat"
            # The main runner caches its request AFTER the waiting hook.
            req = request()
            event.set_extra("provider_request", req)
            await env.plugin.on_request(event, req)
            assert event.get_extra("selected_provider") == "local/persona-chat"
            assert event.call_llm is False and event.stop_calls == 0
            env.context.llm_generate.assert_not_called()
            env.context.send_message.assert_not_called()
            assert await env.context.get_current_chat_provider_id() == "existing-model"

    asyncio.run(run())


@pytest.mark.parametrize(
    "event_options",
    [
        {"private": True},
        {"stopped": True},
        {"group_id": "456789"},
        {"platform_id": "other"},
        {"extras": {}},
        {"extras": {**GATE_MARK, "selected_provider": "explicit"}},
        {"extras": {**GATE_MARK, "selected_model": "explicit"}},
        {"extras": {**GATE_MARK, "provider_request": request()}},
        {"extras": {**GATE_MARK, "handlers_parsed_params": {"plugin": {}}}},
        {"text": "/六爻"},
        {"chain": [Plain("看看"), SimpleNamespace(url="image")]},
        {"chain": [Plain("看看"), Reply()]},
        {"chain": [Plain("看看"), Reply(chain=[SimpleNamespace(url="image")])]},
    ],
)
def test_local_chat_respects_other_routes(entry, event_options):
    async def run():
        async with entry.running(dict(LOCAL_CONFIG)) as env:
            event = MockEvent(**({"extras": GATE_MARK} | event_options))
            before = dict(event.extras)
            await env.plugin.select_local_chat(event)
            assert event.extras == before

    asyncio.run(run())


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"local_chat_groups": ["234567"]},
        {"local_chat_provider_id": "local"},
        LOCAL_CONFIG,
    ],
)
def test_local_chat_requires_opt_in_and_source_authorization(entry, config):
    async def run():
        async with entry.running(dict(config)) as env:
            event = MockEvent(extras=GATE_MARK)
            if config == LOCAL_CONFIG:
                env.context.conversation_manager.get_conversation.return_value = (
                    SimpleNamespace(persona_id="另一个人格")
                )
            await env.plugin.select_local_chat(event)
            assert event.get_extra("selected_provider") is None
            env.context.conversation_manager.get_conversation.return_value = (
                SimpleNamespace(persona_id="可可子")
            )
            env.context.cfg["provider_settings"]["enable"] = False
            await env.plugin.select_local_chat(event)
            assert event.get_extra("selected_provider") is None

    asyncio.run(run())
