"""Metadata discovery must never turn an unselected/private chat into learning."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_persona_manager.groups import group_metadata
from astrbot_plugin_persona_manager.settings import Settings
from astrbot_plugin_persona_manager.storage import Store
from astrbot_plugin_persona_manager.tests import test_integration as integration
from astrbot_plugin_persona_manager.tests.astrbot_stub import MockContext, MockEvent

entry = integration.entry
actual_gate = integration.actual_gate


def named_event(name="太鼓练习群", **kwargs):
    event = MockEvent(**kwargs)
    event.message_obj.group = SimpleNamespace(
        group_id=event.get_group_id(), group_name=name
    )
    event.bot = SimpleNamespace(
        call_action=AsyncMock(
            return_value={
                "group_id": int(event.get_group_id() or "234567"),
                "group_name": name,
                "member_count": 42,
            }
        )
    )
    return event


async def finish(discovery):
    await asyncio.gather(*list(discovery.tasks.values()))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"private": True},
        {"platform_name": "telegram"},
        {"group_id": "bad"},
        {"platform_id": "invalid:id"},
    ],
)
def test_metadata_rejects_private_and_non_qq_groups(kwargs):
    event = MockEvent(**kwargs)
    assert group_metadata(event) is None


def test_name_comes_from_group_metadata_not_sender_text_or_quoted_content():
    event = MockEvent("群名：伪造名称")
    event.message_obj.sender.nickname = "假群名"
    event.message_obj.raw_message = {"group_id": 234567, "group_name": "真实群名\n"}
    assert group_metadata(event)["group_name"] == "真实群名"
    event.message_obj.raw_message = {"group_id": 456789, "group_name": "其他群"}
    assert group_metadata(event)["group_name"] == ""


@pytest.mark.parametrize("case", ["allowed", "denied", "private", "private_bind"])
def test_discovery_precedes_gate_without_changing_route_or_spending_tokens(
    entry, actual_gate, case
):
    async def run():
        ctx = MockContext()
        gate = actual_gate(ctx, {"group_whitelist": ["234567"]})
        ctx.stars[0].star_cls = gate
        private = case.startswith("private")
        event = named_event(
            group_id="" if private else "456789" if case == "denied" else "234567",
            private=private,
            text="/rtlink bind fake-key 1234" if case == "private_bind" else "随便聊聊",
        )
        async with entry.running(config={}, context=ctx) as env:
            before = (
                event.stopped,
                event.call_llm,
                event.is_at_or_wake_command,
                event.message_str,
                dict(event.extras),
            )
            await env.plugin.discover_groups(event)
            await finish(env.plugin.discovery)
            assert before == (
                event.stopped,
                event.call_llm,
                event.is_at_or_wake_command,
                event.message_str,
                dict(event.extras),
            )
            await gate.enforce_access(event)
            await gate.route_wake(event)
            await env.plugin.observe(event)
            await env.plugin.runtime.run_once()
            groups = env.plugin.store.known_groups()
            assert len(groups) == (0 if private else 1)
            if groups:
                assert groups[0]["group_name"] == "太鼓练习群"
                assert "随便聊聊" not in json.dumps(groups, ensure_ascii=False)
            else:
                event.bot.call_action.assert_not_called()
            assert env.plugin.store.pending_sources() == []
            assert event.is_stopped() == (case in {"denied", "private"})
            ctx.llm_generate.assert_not_called()
            ctx.get_current_chat_provider_id.assert_not_called()
            ctx.provider.text_chat.assert_not_called()
            filters = env.plugin.discover_groups._astrbot_filters
            assert filters[0][2]["priority"] > 10001

    asyncio.run(run())


def test_discovery_continues_while_learning_paused_and_does_not_read_body(entry):
    async def run():
        async with entry.running(config={"learning_enabled": False}) as env:
            event = named_event()
            event.message_obj.message = None
            env.plugin.store.set_meta("paused", True)
            await env.plugin.discover_groups(event)
            await finish(env.plugin.discovery)
            assert len(env.plugin.store.known_groups()) == 1
            assert env.plugin.store.pending_sources() == []

    asyncio.run(run())


def test_group_info_is_coalesced_cached_and_renamed_without_model_call(entry):
    async def run():
        async with entry.running(config={}) as env:
            discovery = env.plugin.discovery
            now = [1000.0]
            discovery.clock = lambda: now[0]
            event = named_event()
            await asyncio.gather(*(discovery.observe(event) for _ in range(5)))
            await finish(discovery)
            event.bot.call_action.assert_awaited_once_with(
                "get_group_info", group_id=234567, self_id="345678"
            )
            row = env.plugin.store.known_groups()[0]
            assert row["member_count"] == 42
            assert row["first_seen"] == row["last_seen"] == 1000
            now[0] += 61
            await discovery.observe(event)
            await finish(discovery)
            assert event.bot.call_action.await_count == 1
            # Real metadata can announce a new name without waiting for the TTL.
            event.message_obj.group.group_name = "改名后的鼓群"
            await discovery.observe(event)
            assert env.plugin.store.known_groups()[0]["group_name"] == "改名后的鼓群"
            now[0] += 86400
            event.bot.call_action.return_value["group_name"] = "再次改名"
            event.message_obj.group.group_name = None
            await discovery.observe(event)
            await finish(discovery)
            assert env.plugin.store.known_groups()[0]["group_name"] == "再次改名"
            env.context.llm_generate.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize(
    "result",
    [
        {"group_id": 456789, "group_name": "错误的群"},
        {},
        RuntimeError("private detail"),
    ],
)
def test_failed_name_lookup_keeps_catalog_and_cached_name_with_retry(entry, result):
    async def run():
        async with entry.running(config={}) as env:
            event = named_event("缓存名")
            if isinstance(result, Exception):
                event.bot.call_action.side_effect = result
            else:
                event.bot.call_action.return_value = result
            await env.plugin.discover_groups(event)
            await finish(env.plugin.discovery)
            row = env.plugin.store.known_groups()[0]
            assert row["group_name"] == "缓存名"
            assert row["name_status"] == "unavailable"
            assert row["name_retry_at"] == row["name_checked_at"] + 3600

    asyncio.run(run())


def test_slow_qq_lookup_never_blocks_gate_and_shutdown_cancels_lookup(entry):
    async def run():
        async with entry.running(config={}) as env:
            event = named_event()
            started, cancelled = asyncio.Event(), asyncio.Event()

            async def blocked(*args, **kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            event.bot.call_action.side_effect = blocked
            await asyncio.wait_for(env.plugin.discover_groups(event), timeout=1)
            await asyncio.wait_for(started.wait(), timeout=1)
            await env.plugin.discovery.close()
            assert cancelled.is_set()
            assert env.plugin.discovery.tasks == {}

    asyncio.run(run())


def test_old_sources_backfill_metadata_once_and_survive_reopen(tmp_path):
    path = tmp_path / "groups.sqlite3"
    store = Store(path)
    row = integration.sample(
        MockEvent("不应复制到群目录的正文"), Settings(), "interaction"
    )
    store.add_source(row)
    store.db.execute("DELETE FROM meta WHERE key='group_catalog_backfill_v1'")
    store.db.commit()
    store.close()
    for _ in range(2):
        store = Store(path)
        try:
            groups = store.known_groups()
            assert len(groups) == 1
            assert groups[0]["group_id"] == "234567"
            assert groups[0]["group_name"] == ""
            assert "正文" not in json.dumps(groups, ensure_ascii=False)
        finally:
            store.close()


@pytest.mark.parametrize(
    "observation,memory", [(False, False), (True, False), (False, True), (True, True)]
)
def test_group_switches_independently_control_real_capture(entry, observation, memory):
    async def run():
        cfg = {
            "observation_groups": ["234567"] if observation else [],
            "memory_groups": ["234567"] if memory else [],
        }
        async with entry.running(config=cfg) as env:
            ambient = MockEvent("这局稳如老狗", message_id="ambient")
            await env.plugin.observe(ambient)
            assert bool(env.plugin.store.pending_sources()) is observation
            interaction = MockEvent(
                "以后回答简短",
                message_id="interaction",
                extras={"access_gate_original_text": "以后回答简短"},
            )
            req = integration.request()
            await env.plugin.on_request(interaction, req)
            await env.plugin.after_sent(interaction)
            assert bool(env.plugin.store.profile("可可子", "123456")) is memory
            env.context.llm_generate.assert_not_called()

    asyncio.run(run())


def test_group_list_requires_login_and_never_exposes_message_bodies(entry):
    async def run():
        async with entry.running(config={}) as env:
            await env.plugin.discover_groups(MockEvent("只登记群信息"))
            result = await integration.route(env.context, "state")()
            assert result["groups"][0]["group_id"] == "234567"
            assert result["config"]["values"]["memory_groups"] == []
            env.request.username = None
            assert (await integration.route(env.context, "state")()).status_code == 403

    asyncio.run(run())
