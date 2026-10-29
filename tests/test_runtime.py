"""Runtime integration tests with real SQLite and an entirely local fake model."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest
from astrbot_plugin_persona_manager import runtime as runtime_module
from astrbot_plugin_persona_manager.runtime import Runtime
from astrbot_plugin_persona_manager.settings import Settings
from astrbot_plugin_persona_manager.storage import Store
from astrbot_plugin_persona_manager.temporal import BEIJING


class FakeContext:
    def __init__(self):
        self.calls = []
        self.provider_lookups = []
        self.providers = {}
        self.error = None
        self.block = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def get_current_chat_provider_id(self, *, umo):
        self.provider_lookups.append(umo)
        return self.providers.get(umo, "session-provider")

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        try:
            if self.block:
                await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error is not None:
            raise self.error
        records = json.loads(kwargs["prompt"].split("原始消息 JSON：\n", 1)[1])
        if "任务：只提出群梗" in kwargs["prompt"]:
            proposals = (
                [
                    {
                        "term": "稳如老狗",
                        "meaning": "表现稳定",
                        "usage": "闲聊",
                        "avoid": "求助",
                        "evidence": [
                            {"source_id": row["id"], "quote": row["text"]}
                            for row in records
                        ],
                    }
                ]
                if records
                else []
            )
        else:
            proposals = [
                {
                    "source_id": row["id"],
                    "evidence": row["text"],
                    "kind": "preference",
                    "key": "drink_preference",
                    "content": row["text"],
                    "state": "asserted",
                }
                for row in records
            ]
        return SimpleNamespace(
            completion_text=json.dumps({"proposals": proposals}, ensure_ascii=False)
        )


class FakeLogger:
    def __init__(self):
        self.messages = []

    def warning(self, message, *args, **kwargs):
        self.messages.append((message, args, kwargs))


@pytest.fixture
def harness(tmp_path, monkeypatch):
    now = datetime(2026, 10, 29, 12, tzinfo=BEIJING).timestamp()
    clock = SimpleNamespace(now=now)
    monkeypatch.setattr(runtime_module, "time", SimpleNamespace(time=lambda: clock.now))
    path = tmp_path / "runtime.sqlite3"
    store = Store(path, clock=lambda: clock.now)
    config = [
        Settings(
            persona_id="cocoa",
            observation_groups=frozenset({"g1", "g2"}),
            min_observation_batch=2,
        )
    ]
    context = FakeContext()
    logger = FakeLogger()
    result = SimpleNamespace(
        store=store,
        config=config,
        context=context,
        logger=logger,
        clock=clock,
        path=path,
        runtime=Runtime(store, context, lambda: config[0], logger),
    )
    yield result
    result.store.close()


def sample(
    harness,
    identity="m1",
    *,
    user="123",
    group="g1",
    purpose="interaction",
    text="我喜欢喝温水",
    age=0,
    **extra,
):
    row = {
        "id": identity,
        "persona_id": "cocoa",
        "platform_id": "qq1",
        "bot_id": "999",
        "group_id": group,
        "user_id": user,
        "text": text,
        "sent_at": harness.clock.now - age,
        "purpose": purpose,
        "scope_key": f"qq1:999:{group}",
        "umo": f"qq1:GroupMessage:{group}",
    }
    row.update(extra)
    return row


def payload(context):
    return json.loads(
        context[context.index("{") : context.rindex("\n</kokoko_persona_context>")]
    )


def add_style(harness, identity, group, expression, contexts=None):
    row = sample(
        harness, identity, group=group, purpose="observation", text=f"这首{expression}"
    )
    harness.store.add_source(row)
    style_id = harness.store.apply_styles(
        [
            {
                "source_ids": [identity],
                "expression": expression,
                "meaning": "本群表达",
                "contexts": contexts or ["闲聊"],
                "avoid": ["求助"],
            }
        ],
        harness.store.epoch(),
    )[0]
    version = harness.store.set_style_status(style_id, "active")
    harness.store.mark_sources([identity], harness.store.epoch())
    return style_id, version


def test_success_uses_session_provider_without_tools_or_main_history(harness):
    harness.store.add_source(sample(harness))
    asyncio.run(harness.runtime.run_once())
    assert len(harness.context.calls) == 1
    call = harness.context.calls[0]
    assert call["chat_provider_id"] == "session-provider"
    assert call["tools"] is None and call["contexts"] == []
    assert call["max_tokens"] == 1200
    assert harness.store.profile("cocoa", "123")[0]["content"] == "我喜欢喝温水"
    assert harness.store.pending_sources() == []
    assert harness.runtime.last_error == ""


def test_explicit_local_preference_is_saved_without_paid_call(harness):
    asyncio.run(harness.runtime.capture(sample(harness, text="以后回答简短")))
    asyncio.run(harness.runtime.run_once())
    assert harness.context.calls == []
    assert harness.store.profile("cocoa", "123")[0]["key"] == "response_length"
    assert harness.store.pending_sources() == []


def test_call_budget_and_failure_cooldown_survive_runtime_and_sqlite_reopen(harness):
    harness.config[0] = replace(harness.config[0], daily_calls=2, daily_group_calls=2)
    harness.context.error = RuntimeError("secret-key-12345 private user message")
    harness.store.add_source(sample(harness))
    asyncio.run(harness.runtime.run_once())
    assert len(harness.context.calls) == 1
    harness.store.close()
    harness.store = Store(harness.path, clock=lambda: harness.clock.now)
    harness.runtime = Runtime(
        harness.store, harness.context, lambda: harness.config[0], harness.logger
    )
    asyncio.run(harness.runtime.run_once())
    assert len(harness.context.calls) == 1
    harness.clock.now += harness.config[0].learning_interval_seconds + 1
    asyncio.run(harness.runtime.run_once())
    assert len(harness.context.calls) == 2
    harness.clock.now += harness.config[0].learning_interval_seconds + 1
    asyncio.run(harness.runtime.run_once())
    assert len(harness.context.calls) == 2
    assert harness.store.budget_used("2026-10-29", "*") == 2
    assert harness.store.budget_used("2026-10-29", "qq1:999:g1") == 2
    assert harness.store.pending_sources()[0]["id"] == "m1"
    assert harness.runtime.last_error == "RuntimeError"
    assert "secret-key" not in repr(harness.logger.messages)
    assert "private user" not in repr(harness.logger.messages)


def test_two_people_in_same_group_do_not_share_personal_batch_cooldown(harness):
    harness.store.add_source(sample(harness, "u1", user="123"))
    harness.store.add_source(sample(harness, "u2", user="456", text="我喜欢喝热茶"))
    asyncio.run(harness.runtime.run_once())
    asyncio.run(harness.runtime.run_once())
    assert len(harness.context.calls) == 2
    assert harness.store.profile("cocoa", "123")[0]["content"] == "我喜欢喝温水"
    assert harness.store.profile("cocoa", "456")[0]["content"] == "我喜欢喝热茶"


def test_timeout_is_cancelled_counted_and_has_no_raw_error_log(harness):
    harness.config[0] = replace(harness.config[0], model_timeout_seconds=0.01)
    harness.context.block = True
    harness.store.add_source(sample(harness))
    asyncio.run(harness.runtime.run_once())
    assert harness.context.cancelled
    assert harness.runtime.last_error == "TimeoutError"
    assert harness.store.profile("cocoa", "123") == []
    assert harness.store.budget_used("2026-10-29", "*") == 1
    assert "我喜欢喝温水" not in repr(harness.logger.messages)


@pytest.mark.parametrize("mode", ["paused", "disabled", "learning_disabled"])
def test_learning_pause_stops_paid_work_but_keeps_retention_cleanup(harness, mode):
    harness.store.add_source(
        sample(harness, purpose="observation", text="这局稳如老狗", age=3 * 86400)
    )
    if mode == "paused":
        harness.store.set_meta("paused", True)
    elif mode == "disabled":
        harness.config[0] = replace(harness.config[0], enabled=False)
    else:
        harness.config[0] = replace(harness.config[0], learning_enabled=False)
    asyncio.run(harness.runtime.run_once())
    assert harness.context.calls == []
    assert harness.store.get_source("m1")["text"] == ""


def test_forget_during_inflight_extraction_blocks_result_and_old_source_replay(harness):
    row = sample(harness)
    harness.store.add_source(row)
    harness.context.block = True

    async def scenario():
        task = asyncio.create_task(harness.runtime.run_once())
        await asyncio.wait_for(harness.context.started.wait(), timeout=2)
        harness.store.forget("cocoa", "123")
        harness.context.release.set()
        await task
        await harness.runtime.capture(row)
        await harness.runtime.run_once()

    asyncio.run(scenario())
    assert harness.store.profile("cocoa", "123") == []
    assert harness.store.get_source("m1")["text"] == ""
    assert harness.store.pending_sources() == []
    assert len(harness.context.calls) == 1


def test_style_rollback_invalidates_already_running_extraction(harness):
    old_id, version = add_style(harness, "baseline", "g1", "牢谱")
    for i in range(2):
        harness.store.add_source(
            sample(harness, f"new{i}", purpose="observation", text="这局稳如老狗")
        )
    harness.context.block = True

    async def scenario():
        task = asyncio.create_task(harness.runtime.run_once())
        await asyncio.wait_for(harness.context.started.wait(), timeout=2)
        harness.store.rollback_styles("qq1:999:g1", version)
        harness.context.release.set()
        await task

    asyncio.run(scenario())
    assert [x["id"] for x in harness.store.styles("qq1:999:g1", status=None)] == [
        old_id
    ]
    assert {x["id"] for x in harness.store.pending_sources()} == {"new0", "new1"}


def test_removing_observation_whitelist_while_model_runs_discards_result(harness):
    for i in range(2):
        harness.store.add_source(
            sample(harness, str(i), purpose="observation", text="这局稳如老狗")
        )
    harness.context.block = True

    async def scenario():
        task = asyncio.create_task(harness.runtime.run_once())
        await asyncio.wait_for(harness.context.started.wait(), timeout=2)
        harness.config[0] = replace(harness.config[0], observation_groups=frozenset())
        harness.context.release.set()
        await task

    asyncio.run(scenario())
    assert harness.store.styles("qq1:999:g1", status=None) == []
    assert len(harness.store.pending_sources()) == 2


def test_pause_while_model_runs_discards_result(harness):
    harness.store.add_source(sample(harness))
    harness.context.block = True

    async def scenario():
        task = asyncio.create_task(harness.runtime.run_once())
        await asyncio.wait_for(harness.context.started.wait(), timeout=2)
        harness.store.set_meta("paused", True)
        harness.context.release.set()
        await task

    asyncio.run(scenario())
    assert harness.store.profile("cocoa", "123") == []
    assert len(harness.store.pending_sources()) == 1


def test_same_qq_memory_recalled_across_groups_without_crossing_user(harness):
    asyncio.run(harness.runtime.capture(sample(harness, text="以后回答简短")))
    own_context, _ = asyncio.run(
        harness.runtime.chat_context(
            sample(harness, "new", group="g2", text="你记得我吗")
        )
    )
    other_context, _ = asyncio.run(
        harness.runtime.chat_context(sample(harness, "other", user="456", group="g2"))
    )
    assert payload(own_context)["当前认知"][0]["内容"] == "以后回答简短"
    assert payload(other_context)["当前认知"] == []
    assert "source_group" not in own_context


def test_group_styles_stay_scoped_and_match_context_list_entries(harness):
    first, _ = add_style(harness, "style1", "g1", "牢谱", ["闲聊"])
    second, _ = add_style(harness, "style2", "g2", "稳如老狗", ["闲聊"])
    first_context, first_ids = asyncio.run(
        harness.runtime.chat_context(sample(harness, group="g1", text="今天闲聊一下"))
    )
    second_context, second_ids = asyncio.run(
        harness.runtime.chat_context(sample(harness, group="g2", text="今天闲聊一下"))
    )
    assert [x["expression"] for x in payload(first_context)["本群表达参考"]] == ["牢谱"]
    assert [x["expression"] for x in payload(second_context)["本群表达参考"]] == [
        "稳如老狗"
    ]
    assert first_ids["style_ids"] == [first] and second_ids["style_ids"] == [second]


def test_professional_request_suppresses_learned_banter(harness):
    add_style(harness, "style1", "g1", "牢谱", ["闲聊"])
    context, detail = asyncio.run(
        harness.runtime.chat_context(sample(harness, text="帮我分析牢谱成绩"))
    )
    assert payload(context)["本群表达参考"] == []
    assert detail["style_ids"] == []


def test_each_group_uses_its_own_current_session_provider(harness):
    harness.context.providers = {
        "qq1:GroupMessage:g1": "p1",
        "qq1:GroupMessage:g2": "p2",
    }
    harness.store.add_source(sample(harness, "one", group="g1"))
    harness.store.add_source(sample(harness, "two", group="g2", user="456"))
    asyncio.run(harness.runtime.run_once())
    asyncio.run(harness.runtime.run_once())
    assert [call["chat_provider_id"] for call in harness.context.calls] == ["p1", "p2"]


def test_explicit_learning_provider_overrides_session_provider(harness):
    harness.config[0] = replace(harness.config[0], learning_provider_id="learning-only")
    harness.store.add_source(sample(harness))
    asyncio.run(harness.runtime.run_once())
    assert harness.context.calls[0]["chat_provider_id"] == "learning-only"
    assert harness.context.provider_lookups == []


def test_missing_provider_skips_call_and_preserves_pending_evidence(harness):
    harness.context.providers["qq1:GroupMessage:g1"] = None
    harness.store.add_source(sample(harness))
    asyncio.run(harness.runtime.run_once())
    assert harness.context.calls == []
    assert harness.store.pending_sources()[0]["id"] == "m1"
    assert harness.store.budget_used("2026-10-29", "*") == 0
