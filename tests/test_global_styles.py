"""Cross-group expressions remain opt-in, scoped to one persona and reversible."""

import asyncio
from dataclasses import replace

import pytest
from astrbot_plugin_persona_manager.configuration import validate_changes
from astrbot_plugin_persona_manager.ingress import sample as event_sample
from astrbot_plugin_persona_manager.settings import Settings
from astrbot_plugin_persona_manager.style_matching import fits_group
from astrbot_plugin_persona_manager.tests.astrbot_stub import MockEvent
from astrbot_plugin_persona_manager.tests.test_integration import entry as entry
from astrbot_plugin_persona_manager.tests.test_integration import request, snapshot
from astrbot_plugin_persona_manager.tests.test_runtime import (
    add_style,
    payload,
    sample,
)
from astrbot_plugin_persona_manager.tests.test_runtime import (
    harness as harness,
)


def configure(h, *, enabled=True, tags="太鼓达人"):
    h.config[0] = replace(
        h.config[0],
        global_style_groups=frozenset({"qq1:234567"}) if enabled else frozenset(),
        group_tags=frozenset({f"qq1:234567={tags}"}) if tags else frozenset(),
    )


def recall(h, text="今天闲聊一下", **kwargs):
    return asyncio.run(
        h.runtime.chat_context(sample(h, group="234567", text=text, **kwargs))
    )


@pytest.mark.parametrize(
    "enabled,tags,expected",
    [
        (False, "太鼓达人", False),
        (True, "", False),
        (True, "读书", False),
        (True, "太鼓达人", True),
    ],
)
def test_global_pool_requires_target_switch_and_matching_tags(
    harness, enabled, tags, expected
):
    sid, _ = add_style(harness, "origin", "g1", "稳如老狗", ["太鼓达人", "闲聊"])
    configure(harness, enabled=enabled, tags=tags)
    text, trace = recall(harness)
    assert (sid in trace["style_ids"]) is expected
    assert ("稳如老狗" in text) is expected
    assert harness.context.calls == []


def test_global_recall_does_not_enable_collection_or_memory(harness):
    add_style(harness, "origin", "g1", "稳如老狗", ["太鼓达人", "闲聊"])
    configure(harness)
    asyncio.run(harness.runtime.capture(sample(harness, "target", group="234567")))
    asyncio.run(
        harness.runtime.capture(
            sample(harness, "target2", group="234567", purpose="observation")
        )
    )
    asyncio.run(
        harness.runtime.capture(
            sample(harness, "target3", group="234567", purpose="style_recall")
        )
    )
    assert harness.store.pending_sources() == []
    data = payload(recall(harness)[0])
    assert data["当前认知"] == [] and data["相处"] == {}
    assert data["本群表达参考"]
    assert harness.store.get_source("target") is None
    assert harness.store.get_source("target2") is None
    assert harness.store.get_source("target3") is None


def test_local_priority_duplicate_expression_and_recent_suppression(harness):
    configure(harness)
    harness.config[0] = replace(
        harness.config[0], observation_groups=frozenset({"234567"})
    )
    local, _ = add_style(harness, "local", "234567", "稳如老狗", ["太鼓达人", "闲聊"])
    add_style(harness, "duplicate", "g1", "稳如老狗", ["太鼓达人", "闲聊"])
    other, _ = add_style(harness, "other", "g2", "牢谱", ["太鼓达人", "闲聊"])
    text, trace = recall(harness)
    assert trace["style_ids"] == [local, other]
    assert len(payload(text)["本群表达参考"]) == 2
    assert recall(harness)[1]["style_ids"] == []


def test_irrelevant_local_duplicate_does_not_hide_suitable_global_usage(harness):
    configure(harness)
    harness.config[0] = replace(
        harness.config[0], observation_groups=frozenset({"234567"})
    )
    add_style(harness, "local", "234567", "稳如老狗", ["读书"])
    sid, _ = add_style(harness, "global", "g1", "稳如老狗", ["太鼓达人", "闲聊"])
    assert recall(harness)[1]["style_ids"] == [sid]


@pytest.mark.parametrize("action", ["disable", "forget", "rollback", "candidate"])
def test_global_pool_references_original_lifecycle(harness, action):
    configure(harness)
    _, old_version = add_style(harness, "earlier", "g1", "书山有路", ["读书"])
    sid, _ = add_style(harness, "origin", "g1", "稳如老狗", ["太鼓达人", "闲聊"])
    if action == "forget":
        harness.store.forget("cocoa", "123")
    elif action == "rollback":
        harness.store.rollback_styles("qq1:999:g1", old_version)
    else:
        harness.store.set_style_status(
            sid, "disabled" if action == "disable" else "candidate"
        )
    assert recall(harness)[1]["style_ids"] == []


def test_global_pool_never_crosses_personas(harness):
    configure(harness)
    row = sample(
        harness,
        "other",
        persona_id="other-persona",
        purpose="observation",
        text="稳如老狗",
        scope_key="other-scope",
    )
    harness.store.add_source(row)
    sid = harness.store.apply_styles(
        [
            {
                "source_ids": ["other"],
                "expression": "稳如老狗",
                "meaning": "太鼓达人",
                "contexts": ["闲聊"],
            }
        ],
        harness.store.epoch(),
    )[0]
    harness.store.set_style_status(sid, "active")
    assert recall(harness)[1]["style_ids"] == []


@pytest.mark.parametrize("text", ["帮我分析", "太鼓达人求助", "好难受", "解卦"])
def test_serious_queries_still_suppress_global_styles(harness, text):
    configure(harness)
    add_style(harness, "origin", "g1", "稳如老狗", ["太鼓达人", "闲聊"])
    assert recall(harness, text)[1]["style_ids"] == []


def test_global_styles_still_require_current_conversation_context(harness):
    configure(harness)
    add_style(harness, "origin", "g1", "稳如老狗", ["太鼓达人打歌"])
    assert recall(harness, "午饭吃啥")[1]["style_ids"] == []
    assert recall(harness, "太鼓达人打歌")[1]["style_ids"]


def test_global_recall_obeys_gate_and_private_origin(harness):
    configure(harness)
    add_style(harness, "origin", "g1", "稳如老狗", ["太鼓达人", "闲聊"])
    assert recall(harness, umo="qq1:FriendMessage:123")[0] == ""

    async def deny(source):
        return False

    harness.runtime.source_guard = deny
    assert recall(harness)[0] == ""


@pytest.mark.parametrize("mutation", ["tags", "switch", "forget"])
def test_change_during_global_read_cannot_inject_stale_expression(harness, mutation):
    configure(harness)
    add_style(harness, "origin", "g1", "稳如老狗", ["太鼓达人", "闲聊"])
    original = harness.runtime.db

    async def mutate(method, *args, **kwargs):
        result = await original(method, *args, **kwargs)
        if method == "global_styles":
            if mutation == "forget":
                harness.store.forget("cocoa", "123")
            else:
                configure(
                    harness,
                    enabled=mutation != "switch",
                    tags="读书" if mutation == "tags" else "太鼓达人",
                )
        return result

    harness.runtime.db = mutate
    text, trace = recall(harness)
    assert "稳如老狗" not in text and trace["style_ids"] == []


@pytest.mark.parametrize(
    "tags,query,contexts,avoid,expected",
    [
        (("技术",), "闲聊", ["技术交流"], [], True),
        (("AI",), "闲聊", ["rain"], [], False),
        (("AI",), "闲聊", ["ai discussion"], [], True),
        (("技术",), "闲聊", ["不适合技术交流"], [], False),
        (("技术",), "闲聊", ["技术交流"], ["技术"], False),
        (("技术",), "比赛失败", ["技术交流"], ["失败"], False),
    ],
)
def test_local_tag_match_has_exclusion_precedence(
    tags, query, contexts, avoid, expected
):
    assert (
        fits_group(
            {"meaning": "表达", "contexts": contexts, "avoid": avoid}, tags, query
        )
        is expected
    )


def test_tag_settings_roundtrip_and_specific_connection_override():
    cfg = validate_changes(
        {},
        {
            "global_style_groups": ["qq1:234567"],
            "group_tags": ["234567=轻松，太鼓达人", "qq2:234567=技术"],
        },
    )
    settings = Settings.read(cfg)
    assert settings.tags_for("234567", "qq1") == ("轻松", "太鼓达人")
    assert settings.tags_for("234567", "qq2") == ("技术",)
    assert settings.uses_global_styles("234567", "qq1")
    assert not settings.uses_global_styles("234567", "qq2")
    assert not settings.observes("234567", "qq1")
    assert not settings.remembers("234567", "qq1")


@pytest.mark.parametrize(
    "entries",
    [
        ["234567"],
        ["234567="],
        ["xxx=轻松"],
        ["234567=" + "长" * 25],
        ["234567=甲", "234567=乙"],
        ["234567=<script>"],
        ["234567=" + ",".join(str(x) for x in range(13))],
    ],
)
def test_invalid_group_tag_configuration_rejected(entries):
    with pytest.raises(ValueError):
        validate_changes({}, {"group_tags": entries})


@pytest.mark.parametrize(
    "case", ["normal", "private", "stopped", "plugin_request", "no_gate"]
)
def test_global_only_injection_preserves_other_plugins_and_private_boundaries(
    entry, case
):
    async def run():
        cfg = {
            "global_style_groups": ["qq-main:234567"],
            "group_tags": ["qq-main:234567=太鼓达人"],
        }
        async with entry.running(config=cfg) as env:
            origin = event_sample(
                MockEvent(group_id="456789", text="稳如老狗"),
                env.plugin.settings(),
                "observation",
            )
            store = env.plugin.store
            store.add_source(origin)
            sid = store.apply_styles(
                [
                    {
                        "source_ids": [origin["id"]],
                        "expression": "稳如老狗",
                        "meaning": "稳定",
                        "contexts": ["太鼓达人", "闲聊"],
                    }
                ],
                store.epoch(),
            )[0]
            store.set_style_status(sid, "active")
            store.mark_sources([origin["id"]], store.epoch())
            event = MockEvent(
                text="今天闲聊",
                private=case == "private",
                stopped=case == "stopped",
                extras={}
                if case == "no_gate"
                else {"access_gate_original_text": "今天闲聊"},
            )
            if case == "plugin_request":
                event.set_extra("provider_request", request())
            req = request()
            before = snapshot(req)
            original_parts = list(req.extra_user_content_parts)
            await env.plugin.observe(event)
            await env.plugin.on_request(event, req)
            await env.plugin.after_sent(event)
            assert snapshot(req) == before
            if case == "normal":
                assert len(req.extra_user_content_parts) == len(original_parts) + 1
                assert "稳如老狗" in req.extra_user_content_parts[-1].text
            else:
                assert req.extra_user_content_parts == original_parts
            assert store.pending_sources() == []
            env.context.llm_generate.assert_not_called()
            assert event.stop_calls == 0

    asyncio.run(run())
