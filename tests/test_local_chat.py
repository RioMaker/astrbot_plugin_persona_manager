import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_persona_manager import simulation_bailian
from astrbot_plugin_persona_manager.settings import (
    BAILIAN_GROUP_CHAT_MODEL,
    BAILIAN_LIUYAO_MODEL,
    Settings,
)
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
WORKSPACE = Settings.bailian_workspace_id
TRIAL_8B = f"qq-main:234567=bailian:{WORKSPACE}:{BAILIAN_GROUP_CHAT_MODEL}"
TRIAL_4B = f"qq-main:456789=bailian:{WORKSPACE}:{BAILIAN_LIUYAO_MODEL}"


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
@pytest.mark.parametrize("config", [LOCAL_CONFIG, {"bailian_group_models": [TRIAL_8B]}])
def test_local_chat_respects_other_routes(entry, event_options, config):
    async def run():
        async with entry.running(dict(config)) as env:
            env.plugin.group_trials.bind = Mock()
            event = MockEvent(**({"extras": GATE_MARK} | event_options))
            before = dict(event.extras)
            await env.plugin.select_local_chat(event)
            assert event.extras == before
            env.plugin.group_trials.bind.assert_not_called()

    asyncio.run(run())


def test_concurrent_trials_do_not_change_session_defaults_or_each_others_clients(
    entry, monkeypatch
):
    def create(config, workspace, code, provider_id):
        return SimpleNamespace(
            model=code, key=simulation_bailian.api_key(config), terminate=AsyncMock()
        )

    monkeypatch.setattr(simulation_bailian, "create_agent_provider", create)

    async def run():
        cfg = {
            **LOCAL_CONFIG,
            "bailian_group_models": [TRIAL_8B, TRIAL_4B],
            "bailian_api_key": "key-one",
        }
        async with entry.running(cfg) as env:
            default = object()
            registry = {"default": default}
            env.context.provider_manager = SimpleNamespace(
                inst_map=registry, provider_insts=[default]
            )

            async def trial_turn(group, started, finish):
                event = MockEvent(group_id=group, extras=GATE_MARK)
                await env.plugin.select_local_chat(event)
                alias = event.get_extra("selected_provider")
                started.set_result((alias, registry[alias]))
                await finish.wait()
                assert registry[alias] is started.result()[1]

            finish = asyncio.Event()
            started = asyncio.get_running_loop().create_future()
            task8 = asyncio.create_task(trial_turn("234567", started, finish))
            alias8, eight = await asyncio.wait_for(started, 3)
            cfg["bailian_api_key"] = "key-two"
            started = asyncio.get_running_loop().create_future()
            task4 = asyncio.create_task(trial_turn("456789", started, finish))
            alias4, four = await asyncio.wait_for(started, 3)
            assert (eight.model, eight.key) == (BAILIAN_GROUP_CHAT_MODEL, "key-one")
            assert (four.model, four.key) == (BAILIAN_LIUYAO_MODEL, "key-two")
            for options in ({"group_id": "777888"}, {"platform_id": "qq-second"}):
                other = MockEvent(**options, extras=GATE_MARK)
                await env.plugin.select_local_chat(other)
                assert other.extras == GATE_MARK
            assert env.context.provider_manager.provider_insts == [default]
            assert await env.context.get_current_chat_provider_id() == "existing-model"
            task4.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task4
            assert alias4 not in registry and registry[alias8] is eight
            cfg["bailian_group_models"] = []
            restored = MockEvent(extras=GATE_MARK)
            await env.plugin.select_local_chat(restored)
            assert (
                restored.get_extra("selected_provider")
                == LOCAL_CONFIG["local_chat_provider_id"]
            )
            finish.set()
            await task8
            assert registry == {"default": default}
        four.terminate.assert_awaited_once()
        eight.terminate.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("revoke", ["binding", "model", "persona", "gate"])
def test_bailian_rechecks_target_authorization_before_selecting(entry, revoke):
    async def run():
        cfg = {"bailian_group_models": [TRIAL_8B]}
        async with entry.running(cfg) as env:
            env.plugin.group_trials.bind = Mock()
            if revoke == "persona":
                env.context.conversation_manager.get_conversation.return_value = (
                    SimpleNamespace(persona_id="其他人格")
                )
            elif revoke == "gate":
                env.context.stars[0].star_cls._entries = lambda key: set()
            else:

                async def reconfigure(source):
                    cfg[
                        "bailian_group_models"
                        if revoke == "binding"
                        else "bailian_model_codes"
                    ] = []
                    return True

                env.plugin._source_allowed = reconfigure
            event = MockEvent(extras=GATE_MARK)
            await env.plugin.select_local_chat(event)
            assert event.extras == GATE_MARK
            env.plugin.group_trials.bind.assert_not_called()

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
