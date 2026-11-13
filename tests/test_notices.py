"""Group notices use saved text and respect silent mode and source permissions."""

import asyncio

import pytest
from astrbot_plugin_persona_manager.configuration import validate_changes
from astrbot_plugin_persona_manager.ingress import sample
from astrbot_plugin_persona_manager.settings import Settings
from astrbot_plugin_persona_manager.tests.astrbot_stub import MockEvent
from astrbot_plugin_persona_manager.tests.test_integration import entry as entry


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({}, "正在思考，整理一下刚才的记忆……"),
        ({"thinking_notice_text": "  想一想……\n马上回来～  "}, "想一想……\n马上回来～"),
        ({"thinking_notice_text": ""}, None),
        ({"thinking_notice_text": " \n\t "}, None),
        ({"thinking_notice": False, "thinking_notice_text": "稍等～"}, None),
    ],
)
def test_notice_uses_saved_text_or_stays_silent(entry, changes, expected):
    async def run():
        async with entry.running(
            config=validate_changes({}, changes or {"enabled": True})
        ) as env:
            source = sample(MockEvent(), env.plugin.settings(), "interaction")
            await env.plugin._thinking_notice(source)
            if expected is None:
                env.context.send_message.assert_not_awaited()
            else:
                env.context.send_message.assert_awaited_once()
                umo, chain = env.context.send_message.await_args.args
                assert umo == source["umo"]
                assert [item.text for item in chain.chain] == [expected]

    asyncio.run(run())


@pytest.mark.parametrize("case", ["private", "blocked_group", "config_changed"])
def test_notice_rechecks_source_and_discards_changed_settings(entry, case):
    async def run():
        async with entry.running() as env:
            source = sample(MockEvent(), env.plugin.settings(), "interaction")
            if case == "private":
                source["umo"] = "qq-main:FriendMessage:123456"
            elif case == "blocked_group":
                source.update(group_id="999999", umo="qq-main:GroupMessage:999999")
            else:
                guard = env.plugin._source_allowed

                async def change_during_check(row):
                    allowed = await guard(row)
                    assert allowed
                    env.plugin.config["thinking_notice_text"] = "新提示"
                    return allowed

                env.plugin._source_allowed = change_during_check
            await env.plugin._thinking_notice(source)
            env.context.send_message.assert_not_awaited()

    asyncio.run(run())


def test_native_notice_config_is_bounded_and_old_config_keeps_default():
    assert Settings.read({}).thinking_notice_text == "正在思考，整理一下刚才的记忆……"
    assert (
        Settings.read({"thinking_notice_text": None}).thinking_notice_text
        == Settings().thinking_notice_text
    )
    assert (
        Settings.read({"thinking_notice_text": "长" * 1001}).thinking_notice_text
        == "长" * 1000
    )
