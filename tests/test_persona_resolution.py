"""Keep released and effective-config AstrBot persona contracts explicit."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_persona_manager.tests.astrbot_stub import MockContext, MockEvent
from astrbot_plugin_persona_manager.tests.test_integration import entry as entry
from astrbot_plugin_persona_manager.tests.test_integration import request


class EffectiveConfigPersonaManager:
    """Contract from AstrBot d66e3c2, after the v4.29.4 release.

    The authoritative ID comes from session effective config. Conversation
    persona IDs are no longer accepted, and the former force-applied ID field
    is absent from the three-item result.
    """

    def __init__(self, selected="可可子"):
        self.selected = selected
        self.calls = []

    async def resolve_selected_persona(self, *, umo, platform_name):
        self.calls.append((umo, platform_name))
        return self.selected, {"name": self.selected}, False


def test_released_resolver_receives_request_persona_and_honors_override(entry):
    async def run():
        context = MockContext()
        calls = []

        async def resolver(
            *, umo, conversation_persona_id, platform_name, provider_settings=None
        ):
            # v4.29.4 returns final ID, persona, forced session ID, WebChat flag.
            calls.append((umo, conversation_persona_id, platform_name))
            return "可可子", {"name": "可可子"}, "可可子", False

        context.persona_manager.resolve_selected_persona = resolver
        async with entry.running(context=context) as env:
            event = MockEvent()
            assert await env.plugin._selected(event, request("其他人格"))
            assert calls == [(event.unified_msg_origin, "其他人格", "aiocqhttp")]
            context.conversation_manager.get_curr_conversation_id.assert_not_called()

    asyncio.run(run())


def test_released_resolver_loads_current_conversation_for_queued_source(entry):
    async def run():
        async with entry.running() as env:
            event = MockEvent()
            assert await env.plugin._selected(event)
            env.context.conversation_manager.get_conversation.assert_awaited_once_with(
                event.unified_msg_origin, "conversation-1"
            )

    asyncio.run(run())


@pytest.mark.parametrize("selected", ["可可子", "其他人格", "[%None]", None])
def test_effective_config_resolver_owns_selection_without_conversation_lookup(
    entry, selected
):
    async def run():
        context = MockContext()
        manager = EffectiveConfigPersonaManager(selected)
        context.persona_manager = manager
        context.conversation_manager.get_curr_conversation_id.side_effect = (
            AssertionError(
                "New persona resolution must not depend on the legacy conversation API"
            )
        )
        async with entry.running(context=context) as env:
            event = MockEvent()
            assert await env.plugin._selected(event, request("旧会话人格")) is (
                selected == "可可子"
            )
            assert await env.plugin._selected(event) is (selected == "可可子")
            assert manager.calls == [(event.unified_msg_origin, "aiocqhttp")] * 2
            context.conversation_manager.get_curr_conversation_id.assert_not_called()

    asyncio.run(run())


def test_effective_config_resolver_allows_real_request_and_delivery_capture(entry):
    async def run():
        context = MockContext()
        context.persona_manager = EffectiveConfigPersonaManager()
        async with entry.running(context=context) as env:
            body = "今天练习了双手交替，现在准备休息"
            event = MockEvent(body, extras={"access_gate_original_text": body})
            await env.plugin.on_request(event, request("过期会话人格"))
            assert event.get_extra(env.module.PENDING_KEY)["text"] == body
            await env.plugin.after_sent(event)
            assert env.plugin.store.stats()["sources"] == 1
            assert env.plugin.store.stats()["pending_interactions"] == 1
            assert env.plugin._capture["state"] == "stored"

    asyncio.run(run())


def test_resolver_internal_typeerror_propagates_without_retry_or_fallback(entry):
    async def run():
        context = MockContext()
        fallback = AsyncMock(return_value={"name": "可可子"})
        calls = []

        async def resolver(*, umo, platform_name):
            calls.append(umo)
            raise TypeError("Resolver internal failure")

        context.persona_manager = SimpleNamespace(
            resolve_selected_persona=resolver, get_default_persona_v3=fallback
        )
        async with entry.running(context=context) as env:
            event = MockEvent()
            with pytest.raises(TypeError, match="Resolver internal failure"):
                await env.plugin._selected(event, request())
            assert calls == [event.unified_msg_origin]
            fallback.assert_not_called()

    asyncio.run(run())


def test_resolver_failure_is_reported_by_capture_without_saving_source(entry):
    async def run():
        context = MockContext()

        async def resolver(*, umo, platform_name):
            raise TypeError("Resolver internal failure")

        context.persona_manager.resolve_selected_persona = resolver
        async with entry.running(context=context) as env:
            body = "今天继续练习双手交替"
            event = MockEvent(body, extras={"access_gate_original_text": body})
            await env.plugin.on_request(event, request())
            assert env.plugin._capture["state"] == "request_error"
            assert event.get_extra(env.module.PENDING_KEY) is None
            assert env.plugin.store.stats()["sources"] == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "result",
    [
        None,
        "可可子",
        ("可可子",),
        ("可可子", None, None, False, None),
        (123, None, False),
    ],
)
def test_unknown_resolution_shape_fails_closed_without_guessing(entry, result):
    async def run():
        context = MockContext()

        async def resolver(*, umo, platform_name):
            return result

        context.persona_manager.resolve_selected_persona = resolver
        async with entry.running(context=context) as env:
            event = MockEvent()
            with pytest.raises(ValueError, match="Unsupported AstrBot persona"):
                await env.plugin._selected(event, request("可可子"))
            assert env.plugin.store.stats()["sources"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_legacy_without_resolver_still_uses_default_persona_id(entry, asynchronous):
    async def run():
        context = MockContext()
        fallback = (
            AsyncMock(return_value={"name": "可可子"})
            if asynchronous
            else lambda *, umo: {"name": "可可子"}
        )
        context.persona_manager = SimpleNamespace(get_default_persona_v3=fallback)
        async with entry.running(context=context) as env:
            event = MockEvent()
            assert await env.plugin._selected(event, request(None))
            assert not await env.plugin._selected(event, request("其他人格"))

    asyncio.run(run())
