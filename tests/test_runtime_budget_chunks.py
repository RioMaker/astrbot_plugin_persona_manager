"""Ready memory backlogs fit bounded calls without changing configured quotas."""

import asyncio
import hashlib
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
from astrbot_plugin_persona_manager.tests.test_runtime import FakeLogger
from astrbot_plugin_persona_manager.usage import estimate_request_tokens

DAY = "2026-11-12"
REPLY = "可以，慢速练习稳定了再加速，注意放松和节奏。先保证每一次交替动作稳定，逐步提高速度会更好。"


class MemoryContext:
    def __init__(self):
        self.calls = []
        self.invalid_response = False

    async def get_current_chat_provider_id(self, *, umo):
        return "local-fake-model"

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        rows = records(kwargs)
        row = rows[0]
        proposal = {
            "class_id": "cache",
            "key": "taiko_practice",
            "content": row["text"],
            "subject_source_id": row["id"],
            "evidence": [{"source_id": row["id"], "quote": row["text"]}],
            "provenance": "self_report",
            "confidence": 0.9,
        }
        return SimpleNamespace(
            completion_text="invalid"
            if self.invalid_response
            else json.dumps({"proposals": [proposal]}, ensure_ascii=False),
            usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        )


def records(call):
    return json.loads(call["prompt"].split("原始消息 JSON：\n", 1)[1])


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 11, 12, 12, tzinfo=BEIJING).timestamp())
    monkeypatch.setattr(runtime_module, "time", SimpleNamespace(time=lambda: clock.now))
    store = Store(tmp_path / "chunks.sqlite3", clock=lambda: clock.now)
    settings = [Settings(auto_dream=False, thinking_notice=False)]
    context = MemoryContext()
    scope = json.dumps(
        [settings[0].persona_id, "qq-main", "345678", "234567"],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    runtime = Runtime(store, context, lambda: settings[0], FakeLogger())
    yield SimpleNamespace(
        store=store,
        settings=settings,
        context=context,
        runtime=runtime,
        clock=clock,
        scope=scope,
    )
    store.close()


def add_backlog(env, count=20, length=50):
    rows = []
    for index in range(count):
        row = {
            "id": hashlib.sha256(str(index).encode()).hexdigest(),
            "persona_id": env.settings[0].persona_id,
            "user_id": "123456",
            "group_id": "234567",
            "bot_id": "345678",
            "platform_id": "qq-main",
            "scope_key": env.scope,
            "umo": "qq-main:GroupMessage:234567",
            "purpose": "interaction",
            "text": ("我练太鼓时会先慢速练习双手交替然后逐步提高速度。" * 30)[:length],
            "sent_at": env.clock.now - 100 + index,
            "message_id": str(index),
        }
        env.store.add_source(row)
        env.store.record_delivery(row, REPLY, [], epoch=env.store.epoch())
        rows.append(row)
    return rows


def pending_ids(env):
    return {
        row["id"]
        for row in env.store.pending_learning(persona_id=env.settings[0].persona_id)
    }


def assert_one_bounded_call(env, sources, *, limit):
    assert len(env.context.calls) == 1
    call = env.context.calls[0]
    sent = records(call)
    assert 0 < len(sent) < len(sources)
    sent_ids = {row["id"] for row in sent}
    assert pending_ids(env) == {row["id"] for row in sources} - sent_ids
    estimate = estimate_request_tokens(
        call["prompt"], call["system_prompt"], call["max_tokens"]
    )
    assert estimate["reserved_tokens"] <= limit
    assert call["max_tokens"] == env.settings[0].max_output_tokens
    assert call["tools"] is None
    assert env.runtime.scheduler_status["accepted"] == 1
    report = env.store.usage_report(DAY)
    assert report["recent"][0]["estimated_input"] == estimate["estimated_input"]
    assert report["recent"][0]["metadata"]["sample_count"] == len(sent)
    assert report["recent"][0]["metadata"]["schedule_reason"] == "batch_chunk"


def test_backlog_threshold_can_exceed_call_batch_size(env):
    env.settings[0] = replace(env.settings[0], batch_size=10)
    sources = add_backlog(env, length=20)
    asyncio.run(env.runtime.run_once())
    assert_one_bounded_call(env, sources, limit=20_000)
    assert len(records(env.context.calls[0])) == 10
    assert env.store.budget_used(DAY, "*") == 1


def test_default_50_character_dialogues_fit_without_changing_limits(env):
    sources = add_backlog(env)
    original_settings = env.settings[0]
    asyncio.run(env.runtime.run_once())
    assert_one_bounded_call(env, sources, limit=20_000)
    assert env.settings[0] == original_settings
    assert env.store.budget_used(DAY, env.scope) == 1
    assert len(env.store.usage_report(DAY)["recent"]) == 1
    # Context reduction retains the real sent reply for its selected source.
    call = env.context.calls[0]
    episodes = json.loads(
        call["prompt"].split("标记的对话片段：", 1)[1].split("\n原始消息 JSON：", 1)[0]
    )
    selected_ids = {row["id"] for row in records(call)}
    assert episodes
    for block in episodes:
        for row in block:
            assert row["id"] in selected_ids
            assert row["bot_sent_reply"] == REPLY


@pytest.mark.parametrize(
    "field,prior_day,prior_scope",
    [
        ("daily_token_limit", DAY, "other-group"),
        ("daily_group_token_limit", DAY, "current"),
        ("memory_daily_mtokens", DAY, "other-group"),
        ("memory_monthly_mtokens", "2026-11-11", "other-group"),
    ],
)
def test_batch_uses_remaining_tokens_in_each_budget(env, field, prior_day, prior_scope):
    env.settings[0] = replace(
        env.settings[0],
        daily_token_limit=1_000_000,
        daily_group_token_limit=1_000_000,
        memory_daily_mtokens=1.0,
        memory_monthly_mtokens=1.0,
    )
    env.settings[0] = replace(
        env.settings[0], **{field: 0.020 if field.endswith("mtokens") else 20_000}
    )
    prior = env.store.reserve_learning(
        prior_day,
        env.scope if prior_scope == "current" else prior_scope,
        persona_id=env.settings[0].persona_id,
        provider_id="local-fake-model",
        purpose="interaction",
        estimated_input=7_000,
        output_limit=0,
        global_calls=12,
        group_calls=4,
        global_tokens=1_000_000,
        group_tokens=1_000_000,
    )
    assert prior is not None
    env.store.settle_learning(prior, "success", usage={"total_tokens": 7_000})
    sources = add_backlog(env, length=100)
    asyncio.run(env.runtime.run_once())
    assert_one_bounded_call(env, sources, limit=13_000)
    assert env.runtime.scheduler_status["available_tokens"] == 13_000


def test_single_source_that_cannot_fit_has_diagnostic_and_no_charge(env):
    env.settings[0] = replace(env.settings[0], daily_group_token_limit=1)
    sources = add_backlog(env)
    asyncio.run(env.runtime.run_once())
    assert env.context.calls == []
    assert pending_ids(env) == {row["id"] for row in sources}
    assert env.store.budget_used(DAY, "*") == 0
    assert env.store.usage_report(DAY)["recent"] == []
    status = env.runtime.scheduler_status
    assert status["state"] == "budget_exhausted"
    assert "本群今日 token 预算不足" in status["reason"]
    assert "单条记忆整理" in status["reason"]
    assert status["estimated_tokens"] > status["available_tokens"] == 1
    assert sources[0]["text"] not in json.dumps(status, ensure_ascii=False)


def test_exhausted_call_limit_is_not_bypassed_by_smaller_batches(env):
    env.settings[0] = replace(env.settings[0], daily_group_calls=0)
    sources = add_backlog(env)
    asyncio.run(env.runtime.run_once())
    assert env.context.calls == []
    assert pending_ids(env) == {row["id"] for row in sources}
    assert env.store.usage_report(DAY)["recent"] == []
    assert "本群今日调用次数" in env.runtime.scheduler_status["reason"]


def test_smaller_batch_does_not_bypass_backlog_threshold(env):
    env.settings[0] = replace(env.settings[0], batch_size=10)
    sources = add_backlog(env, count=19)
    env.clock.now += env.settings[0].max_learning_wait_seconds + 1
    asyncio.run(env.runtime.run_once())
    assert env.context.calls == []
    assert pending_ids(env) == {row["id"] for row in sources}
    assert env.runtime.scheduler_status["state"] == "collecting"


def test_failed_shrunken_batch_does_not_complete_any_source(env):
    sources = add_backlog(env)
    env.context.invalid_response = True
    asyncio.run(env.runtime.run_once())
    assert len(env.context.calls) == 1
    assert pending_ids(env) == {row["id"] for row in sources}
    assert env.runtime.scheduler_status["state"] == "error"
    assert env.store.budget_used(DAY, "*") == 1


def test_reservation_race_refits_to_new_allowance_without_extra_model_call(env):
    sources = add_backlog(env)
    original_reserve = env.runtime._reserve_learning
    first = True

    async def reserve(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            prior = env.store.reserve_learning(
                DAY,
                env.scope,
                persona_id=env.settings[0].persona_id,
                provider_id="another-worker",
                purpose="interaction",
                estimated_input=7_000,
                output_limit=0,
                global_calls=12,
                group_calls=4,
                global_tokens=60_000,
                group_tokens=20_000,
            )
            assert prior is not None
            env.store.settle_learning(prior, "success", usage={"total_tokens": 7_000})
        return await original_reserve(*args, **kwargs)

    env.runtime._reserve_learning = reserve
    asyncio.run(env.runtime.run_once())
    assert_one_bounded_call(env, sources, limit=13_000)
    assert len(env.store.usage_report(DAY)["recent"]) == 2
    assert env.store.budget_used(DAY, "*") == 2
