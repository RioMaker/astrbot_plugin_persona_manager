"""Behavioral acceptance of the rebuilt memory/expression pipeline."""

import asyncio
import json
import sqlite3
from dataclasses import replace

import pytest
from astrbot_plugin_persona_manager.expression import eligibility
from astrbot_plugin_persona_manager.learning import (
    build_prompt,
    parse_learning_response,
)
from astrbot_plugin_persona_manager.settings import Settings
from astrbot_plugin_persona_manager.storage import Store
from astrbot_plugin_persona_manager.storage_base import StorageBase
from astrbot_plugin_persona_manager.tests.test_storage import proposal, source, ts


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "memory.sqlite3", clock=lambda: ts("2026-10-29T12:00"))
    yield result
    result.close()


def add_rule(
    store,
    *,
    pattern="稳住了",
    kind="phrase",
    intent="casual",
    keywords=None,
    identity="s1",
):
    row = source(
        identity,
        purpose="observation",
        text=f"这局{pattern}" if kind == "phrase" else "这局稳住了",
    )
    store.add_source(row)
    form = {
        "kind": kind,
        "pattern": pattern,
        "situation": "闲聊提到练习进展",
        "avoid": "难受、求助",
        "intent": intent,
        "keywords": keywords or [],
    }
    rule = {
        "form": form,
        "source_ids": [identity],
        "evidence": [{"source_id": identity, "quote": row["text"]}],
    }
    result = store.apply_styles([rule], store.epoch())[0]
    store.set_style_status(result, "active")
    return result, row, rule


def test_upgrade_preserves_ids_statuses_budget_and_creates_one_backup(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    old = StorageBase(path, clock=lambda: ts("2026-10-29T12:00"))
    old.add_source(source())
    memory = old.apply_memories([proposal()], old.epoch())[0]
    old.reserve_call_budget("2026-10-29", "g1", 10, 5)
    old.mark_sources(["m1"], old.epoch())
    old.close()
    new = Store(path, clock=lambda: ts("2026-10-29T12:00"))
    assert new.get_memory(memory)["content"] == "在练幽玄"
    assert new.recall("cocoa", "123", "幽玄")[0]["id"] == memory
    assert new.budget_used("2026-10-29", "*") == 1
    assert new.pending_learning(persona_id="cocoa") == []
    backup = path.with_suffix(".before-v1.sqlite3")
    assert backup.exists()
    before = backup.read_bytes()
    new.close()
    again = Store(path)
    assert backup.read_bytes() == before
    assert again.db.execute("SELECT count(*) FROM memory_traits").fetchone()[0] == 1
    again.close()


def test_fact_and_traits_are_one_atomic_commit(store):
    store.add_source(source())
    store.db.execute(
        "CREATE TRIGGER abort_traits BEFORE INSERT ON memory_traits BEGIN SELECT RAISE(ABORT,'test failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.apply_memories([proposal()], store.epoch())
    assert store.timeline("cocoa", "123") == []


def test_invalid_pre_upgrade_backup_stops_before_schema_change(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    original = StorageBase(path)
    original.close()
    backup = path.with_suffix(".before-v1.sqlite3")
    backup.write_bytes(b"incomplete backup")
    with pytest.raises(sqlite3.DatabaseError):
        Store(path)
    with sqlite3.connect(path) as check:
        assert (
            check.execute(
                "SELECT name FROM sqlite_master WHERE name='memory_traits'"
            ).fetchone()
            is None
        )
    assert backup.read_bytes() == b"incomplete backup"


def test_two_learning_lanes_progress_independently(store):
    store.add_source(source())
    store.queue_expression("m1")
    store.mark_learning(["m1"], store.epoch(), "interaction")
    assert [r["purpose"] for r in store.pending_learning(persona_id="cocoa")] == [
        "observation"
    ]
    store.mark_learning(["m1"], store.epoch(), "observation")
    assert store.pending_sources() == []
    store.queue_expression("m1")
    assert store.pending_learning(persona_id="cocoa") == []


def test_full_archive_recall_finds_old_relevant_event(store):
    for i in range(70):
        store.add_source(source(str(i), text=f"我完成了练习{i}"))
        store.apply_memories(
            [
                proposal(
                    str(i),
                    "我完成了幽玄全连" if i == 0 else f"我参加了无关聚会{i}",
                    **{},
                )
                | {"kind": "event", "key": "经历"}
            ],
            store.epoch(),
        )
    assert store.recall("cocoa", "123", "幽玄全连")[0]["content"] == "我完成了幽玄全连"
    assert store.recall("cocoa", "999", "幽玄全连") == []


def test_pending_and_conditional_memory_never_become_unconditional_facts(store):
    store.add_source(source())
    pending = store.apply_memories([proposal(confidence=0.5)], store.epoch())[0]
    assert store.get_memory(pending)["status"] == "pending"
    assert store.recall("cocoa", "123", "幽玄") == []
    assert store.profile("cocoa", "123") == []
    store.manage_memory(pending, action="approve", revision=1)
    assert store.recall("cocoa", "123", "幽玄")
    with pytest.raises(ValueError):
        store.manage_memory(pending, action="pin", revision=1)
    store.manage_memory(pending, action="pin", revision=2)
    assert store.get_memory(pending)["pinned"]
    store.add_source(source("conditional"))
    conditional = store.apply_memories(
        [
            {
                **proposal("conditional", "打游戏时希望少说话", qualifiers=["打游戏"]),
                "kind": "preference",
                "key": "语气",
            }
        ],
        store.epoch(),
    )[0]
    assert conditional not in {x["id"] for x in store.recall("cocoa", "123", "少说话")}
    assert conditional in {
        x["id"] for x in store.recall("cocoa", "123", "打游戏少说话")
    }


def test_unconfirmed_correction_keeps_old_fact_active(store):
    store.add_source(source())
    old = store.apply_memories([proposal()], store.epoch())[0]
    store.add_source(source("new", text="我最近在练万戈"))
    pending = store.apply_memories(
        [
            proposal(
                "new",
                "我最近在练万戈",
                corrects_id=old,
                expected_revision=1,
                confidence=0.6,
            )
        ],
        store.epoch(),
    )[0]
    assert store.get_memory(old)["status"] == "active"
    assert store.get_memory(pending)["status"] == "pending"
    store.manage_memory(pending, action="approve", revision=2)
    assert store.get_memory(old)["status"] == "corrected"
    assert old not in {x["id"] for x in store.recall("cocoa", "123", "幽玄")}


def test_stale_model_correction_does_not_retire_revised_memory(store):
    store.add_source(source())
    old = store.apply_memories([proposal()], store.epoch())[0]
    store.manage_memory(old, action="pin", revision=1)
    store.add_source(source("new"))
    assert (
        store.apply_memories(
            [proposal("new", corrects_id=old, expected_revision=1)], store.epoch()
        )
        == []
    )
    assert store.get_memory(old)["status"] == "active"


def test_structured_expression_parsing_requires_real_human_evidence():
    rows = [source("s", purpose="observation", text="嗯，明天再说")]
    form = {
        "kind": "response",
        "pattern": "简短确认后结束话题，不追加新问题",
        "situation": "对方结束聊天",
        "avoid": "对方提出新问题",
        "intent": "closing",
        "keywords": [],
    }
    item = {"form": form, "evidence": [{"source_id": "s", "quote": "嗯，明天再说"}]}
    valid = parse_learning_response(
        "observation", rows, json.dumps({"proposals": [item]}, ensure_ascii=False)
    )
    assert valid[0]["form"]["kind"] == "response"
    item["evidence"][0]["quote"] = "机器人自己编造的赞美"
    assert (
        parse_learning_response(
            "observation", rows, json.dumps({"proposals": [item]}, ensure_ascii=False)
        )
        == []
    )
    item["evidence"][0] = {"source_id": "assistant", "quote": "嗯，明天再说"}
    assert (
        parse_learning_response(
            "observation", rows, json.dumps({"proposals": [item]}, ensure_ascii=False)
        )
        == []
    )


def test_new_grammar_and_old_phrases_have_specific_eligibility(store):
    rule_id, _, _ = add_rule(
        store, pattern="短应一声后停住", kind="response", intent="acknowledge"
    )
    row = store.get_style(rule_id)
    assert eligibility(row, "嗯") > 0
    assert eligibility(row, "明天怎么练？") == 0
    assert eligibility(row, "今天很难受") == 0


def test_selection_without_send_never_spends_cooldown(store):
    rule_id, origin, _ = add_rule(store)
    settings = Settings(persona_id="cocoa", observation_groups=frozenset({"g1"}))
    query = {**origin, "id": "turn", "text": "闲聊练习进展"}
    for _ in range(3):
        assert (
            store.select_styles(query, settings, now=store.clock())[0]["id"] == rule_id
        )
    assert store.get_style(rule_id)["injections"] == 0
    store.record_delivery(query, "这局稳住了", [rule_id], keep_dialogue=False)
    assert store.select_styles(query, settings, now=store.clock()) == []
    assert store.get_style(rule_id)["uses"] == 1
    store.record_delivery(query, "这局稳住了", [rule_id], keep_dialogue=False)
    assert store.get_style(rule_id)["injections"] == 1


def test_grammar_injection_is_not_claimed_as_observed_use(store):
    sid, row, _ = add_rule(store, kind="grammar", pattern="先确认，再省略主语接一句")
    store.record_delivery(row, "好，明天再来", [sid])
    assert store.get_style(sid)["injections"] == 1
    assert store.get_style(sid)["uses"] == 0


def test_feedback_requires_same_person_and_explicit_evaluation(store):
    sid, _, _ = add_rule(store)
    turn = source("turn")
    store.add_source(turn)
    store.record_delivery(turn, "稳住了", [sid])
    at = store.clock() + 10
    assert (
        store.consume_feedback({**source("laugh"), "text": "哈哈", "sent_at": at}) == {}
    )
    assert (
        store.consume_feedback(
            {**source("other", user="456"), "text": "别这么说", "sent_at": at}
        )
        == {}
    )
    assert (
        store.consume_feedback(
            {
                **source("quote"),
                "text": "别这么说",
                "sent_at": at,
                "reply_to": "some-other-message",
            }
        )
        == {}
    )
    feedback = {**source("feedback"), "text": "别这么说", "sent_at": at}
    assert store.consume_feedback(feedback)["style_ids"] == [sid]
    assert store.consume_feedback(feedback) == {}
    assert store.get_style(sid)["negative"] == 1


def test_ambiguous_feedback_does_not_blame_every_injected_rule(store):
    first, _, _ = add_rule(store)
    second, _, _ = add_rule(store, pattern="慢点来", identity="s2")
    turn = source("turn")
    store.add_source(turn)
    store.record_delivery(turn, "稳住了，慢点来", [first, second])
    result = store.consume_feedback(
        {**source("feedback"), "text": "别这么说", "sent_at": store.clock() + 10}
    )
    assert result["style_ids"] == []
    assert (
        store.get_style(first)["negative"] == store.get_style(second)["negative"] == 0
    )


def test_repeated_negative_feedback_requires_review_and_cannot_auto_reactivate(store):
    sid, _, _ = add_rule(store)
    for i in range(2):
        turn = source(f"turn{i}")
        store.add_source(turn)
        store.record_delivery(turn, "稳住了", [sid])
        store.consume_feedback(
            {**source(f"f{i}"), "text": "别这么说", "sent_at": store.clock() + 10}
        )
    row = store.get_style(sid)
    assert row["status"] == "candidate" and row["review_reason"]
    assert (
        store.auto_activate_styles(
            row["scope_key"], min_authors=1, min_days=1, min_evidence=1
        )
        == []
    )


def test_expression_edit_version_restores_content_not_only_enabled_ids(store):
    sid, row, original = add_rule(store)
    version = store.style_versions(row["scope_key"])[0]["id"]
    current = store.get_style(sid)
    form = {**original["form"], "pattern": "稳了一点"}
    store.edit_style(sid, form, revision=current["revision"])
    assert store.get_style(sid)["status"] == "candidate"
    with pytest.raises(ValueError):
        store.edit_style(sid, form, revision=current["revision"])
    store.rollback_styles(row["scope_key"], version)
    assert store.get_style(sid)["expression"] == "稳住了"
    assert store.get_style(sid)["status"] == "active"


def test_forget_removes_index_dialogue_feedback_and_version_payloads(store):
    sid, row, _ = add_rule(store)
    turn = source("turn")
    store.add_source(turn)
    store.apply_memories([proposal("turn")], store.epoch())
    store.record_delivery(turn, "稳住了", [sid])
    store.consume_feedback(
        {**source("feedback"), "text": "别这么说", "sent_at": store.clock() + 10}
    )
    store.forget("cocoa", "123")
    assert store.recall("cocoa", "123", "幽玄") == []
    assert store.dialogue_history("cocoa") == []
    assert store.db.execute("SELECT count(*) FROM feedback_events").fetchone()[0] == 0
    assert all(
        "稳住了" not in row[0]
        for row in store.db.execute("SELECT snapshot_json FROM style_versions")
    )
    assert not store.add_source(turn)


def test_episode_material_preserves_sent_reply_but_does_not_make_it_evidence(store):
    turn = source("turn", text="我最近在练幽玄")
    store.add_source(turn)
    store.record_delivery(turn, "明天再试试", [])
    snippets = store.conversation_material([turn])
    assert snippets[0][0]["bot_sent_reply"] == "明天再试试"
    prompt = build_prompt(
        "interaction",
        [turn],
        episodes=snippets,
        existing=[{"handle": "m1:r1", "content": "原记忆"}],
    )
    assert "m1:r1" in prompt and "明天再试试" in prompt
    invalid = {
        "source_id": "turn",
        "evidence": "明天再试试",
        "kind": "goal",
        "key": "目标",
    }
    assert (
        parse_learning_response(
            "interaction",
            [turn],
            json.dumps({"proposals": [invalid]}, ensure_ascii=False),
        )
        == []
    )


def test_runtime_sent_turn_queues_both_domains_without_new_foreground_calls(tmp_path):
    from astrbot_plugin_persona_manager.runtime import Runtime
    from astrbot_plugin_persona_manager.tests.test_runtime import (
        FakeContext,
        FakeLogger,
    )

    async def run():
        store = Store(
            tmp_path / "runtime.sqlite3", clock=lambda: ts("2026-10-29T12:00")
        )
        config = Settings(
            persona_id="cocoa",
            memory_groups=frozenset({"g1"}),
            observation_groups=frozenset({"g1"}),
        )
        context = FakeContext()
        runtime = Runtime(store, context, lambda: config, FakeLogger())
        await runtime.record_delivery(
            source(text="我喜欢喝温水"), "嗯，温的", {"style_ids": []}
        )
        assert {x["purpose"] for x in store.pending_learning(persona_id="cocoa")} == {
            "interaction",
            "observation",
        }
        assert store.dialogue_history("cocoa")[0]["reply_text"] == "嗯，温的"
        assert not context.calls
        await runtime.close()

    asyncio.run(run())


def test_empty_context_and_disabled_expression_are_zero_extra_calls(tmp_path):
    from astrbot_plugin_persona_manager.runtime import Runtime
    from astrbot_plugin_persona_manager.tests.test_runtime import (
        FakeContext,
        FakeLogger,
    )

    async def run():
        store = Store(tmp_path / "runtime.sqlite3")
        config = Settings(persona_id="cocoa", observation_groups=frozenset({"g1"}))
        runtime = Runtime(store, FakeContext(), lambda: config, FakeLogger())
        assert (await runtime.chat_context(source()))[0] == ""
        add_rule(store)
        config = replace(config, expression_per_turn=0)
        assert (await runtime.chat_context(source()))[0] == ""
        assert not runtime.context.calls
        await runtime.close()

    asyncio.run(run())


def test_pinned_preference_survives_many_unrelated_attributes(store):
    store.add_source(source("name"))
    first = store.apply_memories(
        [
            proposal("name", "叫我阿青")
            | {"kind": "preference", "key": "preferred_name"}
        ],
        store.epoch(),
    )[0]
    for i in range(80):
        store.add_source(source(f"new{i}"))
        store.apply_memories([proposal(f"new{i}") | {"key": str(i)}], store.epoch())
    assert store.profile("cocoa", "123", limit=3)[0]["id"] == first


def test_forgotten_expression_cannot_be_restored_by_stale_editor(store):
    sid, _, original = add_rule(store)
    revision = store.get_style(sid)["revision"]
    store.forget("cocoa", "123")
    with pytest.raises(ValueError):
        store.edit_style(sid, original["form"], revision=revision)
    assert store.get_style(sid) is None


def test_shared_group_does_not_merge_personas_or_include_foreign_context(store):
    sid, origin, rule = add_rule(store)
    foreign = {**origin, "id": "foreign", "persona_id": "another", "text": "这局稳住了"}
    store.add_source(foreign)
    other_rule = {
        **rule,
        "source_ids": ["foreign"],
        "evidence": [{"source_id": "foreign", "quote": "这局稳住了"}],
    }
    other_id = store.apply_styles([other_rule], store.epoch())[0]
    assert other_id != sid
    store.set_style_status(other_id, "active")
    settings = Settings(persona_id="another", observation_groups=frozenset({"g1"}))
    selected = store.select_styles(
        {**foreign, "text": "闲聊练习进展"}, settings, now=store.clock()
    )
    assert [x["id"] for x in selected] == [other_id]
    assert [
        x["handle"].split(":")[0]
        for x in store.style_references(origin["scope_key"], persona_id="another")
    ] == [f"s{other_id}"]
    assert all(
        item["id"] != "s1"
        for episode in store.conversation_material([foreign])
        for item in episode
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("我不喜欢你这样说", "negative"),
        ("这个语气不太自然", "negative"),
        ("这样说很自然", "positive"),
        ("不是不喜欢你这样说", ""),
        ("哈哈", ""),
        ("他说这个语气很自然", ""),
    ],
)
def test_feedback_never_turns_explicit_negation_into_praise(text, expected):
    from astrbot_plugin_persona_manager.dialogue import feedback_signal

    assert feedback_signal(text) == expected


def test_learning_material_uses_consistent_anonymous_speakers():
    rows = [source("a"), source("b"), source("c", user="456")]
    prompt = build_prompt("interaction", rows)
    records = json.loads(prompt.split("原始消息 JSON：\n")[1])
    assert records[0]["speaker"] == records[1]["speaker"] != records[2]["speaker"]
