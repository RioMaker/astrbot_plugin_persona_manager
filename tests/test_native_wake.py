"""Native AstrBot wake routing still learns when Access Gate wake routing is off."""

import asyncio
import copy
from types import SimpleNamespace

import pytest
from astrbot_plugin_persona_manager.memory_tools import MemoryTools
from astrbot_plugin_persona_manager.tests.astrbot_stub import (
    MockContext,
    MockEvent,
    Plain,
    ResultContentType,
)
from astrbot_plugin_persona_manager.tests.test_integration import (
    actual_gate as actual_gate,
)
from astrbot_plugin_persona_manager.tests.test_integration import entry as entry
from astrbot_plugin_persona_manager.tests.test_integration import request


def native_config():
    return {"memory_default_enabled": True, "memory_batch_threshold": 20}


async def native_event(gate, text="今天练习左右手交替更稳定", **kwargs):
    event = MockEvent(text, **kwargs)
    event._has_send_oper = False
    await gate.enforce_access(event)
    await gate.route_wake(event)
    assert event.get_extra("access_gate_original_text") is None
    req = request()
    req.prompt = event.message_str
    # Both v4.24.5 and current Core cache this exact request for native chat.
    event.set_extra("provider_request", req)
    return event, req


@pytest.mark.parametrize("streaming", [False, True])
def test_native_wake_records_only_after_actual_delivery(entry, actual_gate, streaming):
    async def run():
        context = MockContext()
        gate = actual_gate(
            context, {"group_whitelist": ["234567"], "wake_enabled": False}
        )
        context.stars[0].star_cls = gate
        async with entry.running(native_config(), context) as env:
            event, req = await native_event(gate)
            assert event.is_at_or_wake_command and not event.call_llm
            await env.plugin.on_request(event, req)
            assert event.get_extra(env.module.PENDING_KEY) is not None
            assert env.plugin.store.pending_sources() == []
            # Dispatching a send hook without an actual send must not learn.
            await env.plugin.after_sent(event)
            assert env.plugin.store.pending_sources() == []
            status = await MemoryTools(env.plugin).execute(event, "status")
            assert status["ok"], status
            event._has_send_oper = True
            if streaming:
                event._result = SimpleNamespace(
                    chain=[Plain("收到")],
                    result_content_type=ResultContentType.STREAMING_FINISH,
                )
                await env.plugin.after_stream(event)
            else:
                await env.plugin.after_sent(event)
            rows = env.plugin.store.pending_sources()
            assert len(rows) == 1
            assert rows[0]["purpose"] == "interaction"
            assert rows[0]["text"] == event.message_str
            assert rows[0]["user_id"] == event.get_sender_id()
            context.llm_generate.assert_not_called()

    asyncio.run(run())


def test_native_wake_tools_require_hook_authorization(entry, actual_gate):
    async def run():
        context = MockContext()
        gate = actual_gate(
            context, {"group_whitelist": ["234567"], "wake_enabled": False}
        )
        context.stars[0].star_cls = gate
        async with entry.running(native_config(), context) as env:
            event, req = await native_event(gate, "请记住：双手交替需要保持节奏")
            service = MemoryTools(env.plugin)
            arguments = {
                "key": "太鼓手法",
                "content": "双手交替需要保持节奏",
                "evidence": "双手交替需要保持节奏",
                "category": "cache",
            }
            before_hook = await service.execute(event, "remember", **arguments)
            assert not before_hook["ok"]
            await env.plugin.on_request(event, req)
            saved = await service.execute(event, "remember", **arguments)
            assert saved["ok"], saved
            assert env.plugin.store.memory_catalog("可可子")["total"] == 1
            context.llm_generate.assert_not_called()

    asyncio.run(run())


def test_native_wake_is_not_preempted_by_observation_capture(entry, actual_gate):
    async def run():
        context = MockContext()
        gate = actual_gate(
            context, {"group_whitelist": ["234567"], "wake_enabled": False}
        )
        context.stars[0].star_cls = gate
        config = {**native_config(), "observation_groups": ["234567"]}
        async with entry.running(config, context) as env:
            event, req = await native_event(gate)
            # Adapter-message handlers precede on_llm_request in Core.
            await env.plugin.observe(event)
            assert env.plugin.store.pending_sources() == []
            await env.plugin.on_request(event, req)
            event._has_send_oper = True
            await env.plugin.after_sent(event)
            rows = env.plugin.store.pending_sources()
            assert len(rows) == 1
            assert rows[0]["purpose"] == "interaction"
            assert (
                len(
                    env.plugin.store.pending_learning(
                        persona_id="可可子", lane="interaction"
                    )
                )
                == 1
            )

            ambient, _ = await native_event(
                gate, "今天换新鼓面了", wake=False, message_id="ambient"
            )
            await env.plugin.observe(ambient)
            rows = env.plugin.store.pending_sources()
            assert {row["purpose"] for row in rows} == {"observation", "interaction"}

    asyncio.run(run())


@pytest.mark.parametrize(
    "boundary",
    [
        "wake_routing_enabled",
        "not_native_wake",
        "missing_cached_request",
        "different_cached_request",
        "default_llm_suppressed",
        "command",
        "blocked_group",
        "other_persona",
        "gate_unloaded",
    ],
)
def test_native_fallback_preserves_source_boundaries(entry, actual_gate, boundary):
    async def run():
        context = MockContext()
        gate = actual_gate(
            context, {"group_whitelist": ["234567"], "wake_enabled": False}
        )
        context.stars[0].star_cls = gate
        async with entry.running(native_config(), context) as env:
            event, req = await native_event(
                gate, group_id="456789" if boundary == "blocked_group" else "234567"
            )
            if boundary == "wake_routing_enabled":
                gate.config["wake_enabled"] = True
            elif boundary == "not_native_wake":
                event.is_at_or_wake_command = False
            elif boundary == "missing_cached_request":
                event.extras.pop("provider_request")
            elif boundary == "different_cached_request":
                event.set_extra("provider_request", copy.copy(req))
            elif boundary == "default_llm_suppressed":
                event.call_llm = True
            elif boundary == "command":
                event.set_extra("handlers_parsed_params", {"plugin.command": {}})
            elif boundary == "other_persona":
                req.conversation.persona_id = "其他人格"
                context.conversation_manager.get_conversation.return_value.persona_id = "其他人格"
            elif boundary == "gate_unloaded":
                context.stars.clear()
            await env.plugin.on_request(event, req)
            event._has_send_oper = True
            await env.plugin.after_sent(event)
            assert event.get_extra(env.module.PENDING_KEY) is None
            assert env.plugin.store.pending_sources() == []
            result = await MemoryTools(env.plugin).execute(event, "status")
            assert not result["ok"]
            context.llm_generate.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["message", "gate_wake", "group_access"])
def test_native_tool_trust_is_bound_to_current_message_and_access(
    entry, actual_gate, change
):
    async def run():
        context = MockContext()
        gate = actual_gate(
            context, {"group_whitelist": ["234567"], "wake_enabled": False}
        )
        context.stars[0].star_cls = gate
        async with entry.running(native_config(), context) as env:
            event, req = await native_event(gate)
            await env.plugin.on_request(event, req)
            assert event.get_extra(env.module.PENDING_KEY) is not None
            if change == "message":
                event.message_obj.message_id = "another-message"
            elif change == "gate_wake":
                gate.config["wake_enabled"] = True
            else:
                gate.config["group_whitelist"] = []
            result = await MemoryTools(env.plugin).execute(event, "status")
            assert not result["ok"]
            assert env.plugin.store.pending_sources() == []

    asyncio.run(run())
