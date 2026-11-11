"""Model correction proposals must remain scoped and revision checked."""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from astrbot_plugin_persona_manager.memory_types import parse_memory_response
from astrbot_plugin_persona_manager.runtime import Runtime
from astrbot_plugin_persona_manager.settings import Settings
from astrbot_plugin_persona_manager.storage import Store
from astrbot_plugin_persona_manager.tests.test_runtime import FakeContext, FakeLogger


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "corrections.sqlite3", clock=lambda: 1794355200)
    yield result
    result.close()


def source(
    store,
    sid,
    *,
    user="123456",
    scope="qq:bot:group",
    persona="cocoa",
    content="我最近在练双手交替",
    purpose="interaction",
):
    row = dict(
        id=sid,
        persona_id=persona,
        user_id=user,
        platform_id="qq",
        bot_id="bot",
        scope_key=scope,
        group_id=scope.split(":")[-1],
        umo="qq:GroupMessage:" + scope.split(":")[-1],
        sent_at=1794355100,
        text=content,
        purpose=purpose,
    )
    store.add_source(row)
    return row


def proposal(row, **overrides):
    return dict(
        class_id="relationship",
        key="手法",
        content=row["text"],
        subject_source_id=row["id"],
        evidence=[dict(source_id=row["id"], quote=row["text"], role="user")],
        provenance="self_report",
        confidence=0.9,
        **overrides,
    )


def parse(store, row, value, existing=None):
    return parse_memory_response(
        [row],
        json.dumps({"proposals": [value]}),
        store.classifiers(row["persona_id"]),
        [],
        existing=existing,
    )


def seed(store):
    row = source(store, "old")
    mid = store.apply_classified(parse(store, row, proposal(row)), store.epoch())[0]
    return mid, [{"handle": f"m{mid}:r1"}]


def test_legacy_correction_keeps_existing_handle(store):
    row = source(store, "legacy")
    item = dict(
        source_id=row["id"],
        evidence=row["text"],
        kind="goal",
        key="practice",
        operation="correct",
        target="m3:r2",
    )
    result = parse(store, row, item, [{"handle": "m3:r2"}])
    assert result[0]["corrects_id"] == 3
    assert result[0]["expected_revision"] == 2
    with pytest.raises(ValueError):
        parse(store, row, item)


def test_classified_correction_retires_exact_record_and_preserves_key(store):
    old, refs = seed(store)
    row = source(store, "new")
    item = proposal(row, operation="correct", target=refs[0]["handle"])
    item["key"] = "不允许模型换主题"
    parsed = parse(store, row, item, refs)
    new = store.apply_classified(parsed, store.epoch())[0]
    assert store.get_memory(new)["corrects_id"] == old
    assert store.get_memory(new)["key"] == "手法"
    assert store.get_memory(old)["status"] == "corrected"
    assert store.get_source("old")["status"] == "reviewed"
    assert old not in {m["id"] for m in store.recall("cocoa", "123456", "双手交替")}


@pytest.mark.parametrize(
    "changes",
    [
        {"user": "654321"},
        {"scope": "qq:bot:other"},
        {"persona": "other"},
    ],
)
def test_classified_correction_cannot_cross_owner_scope_or_persona(store, changes):
    old, refs = seed(store)
    row = source(store, "foreign", **changes)
    item = proposal(row, operation="correct", target=refs[0]["handle"])
    assert store.apply_classified(parse(store, row, item, refs), store.epoch()) == []
    assert store.get_memory(old)["status"] == "active"


def test_stale_target_cannot_be_overwritten(store):
    old, refs = seed(store)
    row = source(store, "new")
    parsed = parse(
        store, row, proposal(row, operation="correct", target=refs[0]["handle"]), refs
    )
    store.manage_memory(old, action="pin", revision=1)
    assert store.apply_classified(parsed, store.epoch()) == []
    assert store.get_memory(old)["status"] == "active"


@pytest.mark.parametrize("stale", [False, True])
def test_unknown_long_correction_waits_for_class_approval_and_rechecks_target(
    store, stale
):
    old, refs = seed(store)
    row = source(store, "new")
    definition = dict(
        id="technique",
        name="太鼓手法",
        category="relationship",
        description="练习习惯",
        keywords=["手法"],
        ttl_seconds=1800,
    )
    item = proposal(
        row, operation="correct", target=refs[0]["handle"], new_class=definition
    )
    item["class_id"] = "technique"
    new = store.apply_classified(parse(store, row, item, refs), store.epoch())[0]
    assert store.get_memory(new)["status"] == "pending"
    assert store.get_memory(old)["status"] == "active"
    if stale:
        store.manage_memory(old, action="pin", revision=1)
    store.save_classifier("cocoa", definition, revision=1, confirm=True)
    assert store.get_memory(old)["status"] == ("active" if stale else "corrected")
    assert store.get_memory(new)["status"] == ("pending" if stale else "active")
    if stale:
        with pytest.raises(ValueError, match="被纠正"):
            store.manage_memory(
                new, action="approve", revision=store.get_memory(new)["revision"]
            )


def test_low_confidence_correction_needs_individual_review(store):
    old, refs = seed(store)
    row = source(store, "new")
    item = proposal(row, operation="correct", target=refs[0]["handle"])
    item["confidence"] = 0.6
    new = store.apply_classified(parse(store, row, item, refs), store.epoch())[0]
    assert store.get_memory(old)["status"] == "active"
    assert store.get_memory(new)["status"] == "pending"
    store.manage_memory(
        new, action="approve", revision=store.get_memory(new)["revision"]
    )
    assert store.get_memory(old)["status"] == "corrected"
    assert store.get_memory(new)["status"] == "active"


@pytest.mark.parametrize(
    "operation,target", [("erase", None), ("add", "m1:r1"), ("correct", "m999:r1")]
)
def test_invalid_operation_or_unoffered_handle_cannot_fall_back_to_add(
    store, operation, target
):
    row = source(store, "new")
    with pytest.raises(ValueError):
        parse(
            store,
            row,
            proposal(row, operation=operation, target=target),
            [{"handle": "m1:r1"}],
        )


def test_scoped_consolidation_does_not_process_other_groups_or_dream(store):
    daily_source = source(store, "daily")
    daily = proposal(daily_source)
    daily["class_id"] = "daily"
    mid = store.apply_classified(parse(store, daily_source, daily), store.epoch())[0]
    store.set_meta("last_dream:cocoa", 1)
    runtime = Runtime(
        store,
        FakeContext(),
        lambda: Settings(persona_id="cocoa", memory_batch_threshold=1),
        FakeLogger(),
    )
    asyncio.run(runtime.run_once(scope_key="qq:bot:not-present"))
    assert runtime.context.calls == []
    assert store.get_memory(mid)["status"] == "active"
    assert store.get_meta("last_dream:cocoa") == 1


def test_knowledge_correction_waits_for_independent_confirmations(store):
    ids = []
    for sid, user in [("before-a", "123456"), ("before-b", "654321")]:
        row = source(store, sid, user=user)
        item = proposal(row)
        item["class_id"] = "knowledge"
        ids.extend(
            store.apply_classified(
                parse(store, row, item), store.epoch(), knowledge_confirmations=2
            )
        )
    old = ids[0]
    refs = [{"handle": f"m{old}:r1"}]
    row = source(store, "after-a", user="345678", content="我最近在练滚奏")
    item = proposal(row, operation="correct", target=refs[0]["handle"])
    item["class_id"] = "knowledge"
    corrected = store.apply_classified(
        parse(store, row, item, refs), store.epoch(), knowledge_confirmations=2
    )[0]
    assert store.get_memory(old)["status"] == "active"
    assert store.get_memory(corrected)["hold_reason"] == "knowledge"
    row2 = source(store, "after-b", user="876543", content=row["text"])
    item2 = proposal(row2)
    item2["class_id"] = "knowledge"
    store.apply_classified(
        parse(store, row2, item2), store.epoch(), knowledge_confirmations=2
    )
    assert store.get_memory(old)["status"] == "corrected"
    assert store.get_memory(corrected)["status"] == "active"


def test_model_cannot_overwrite_admin_forced_knowledge(store):
    mid = store.force_knowledge("cocoa", "交替击打", "手法", actor="admin")
    row = source(store, "replacement")
    item = proposal(row, operation="correct", target=f"m{mid}:r1")
    item["class_id"] = "knowledge"
    parsed = parse(store, row, item, [{"handle": item["target"]}])
    assert store.apply_classified(parsed, store.epoch()) == []
    assert store.get_memory(mid)["content"] == "交替击打"


def test_new_memory_keeps_the_preexisting_deduplication_fingerprint(store):
    old, _ = seed(store)
    row = store.get_memory(old)
    expected = hashlib.sha256(
        json.dumps(
            ["cocoa", "123456", ["old"], "relationship", "手法", "我最近在练双手交替"],
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    assert row["fingerprint"] == expected


def test_class_maintenance_cannot_approve_low_confidence_legacy_correction(store):
    old, refs = seed(store)
    row = source(store, "legacy")
    item = dict(
        source_id=row["id"],
        evidence=row["text"],
        kind="goal",
        key="practice",
        operation="correct",
        target=refs[0]["handle"],
        confidence=0.6,
    )
    new = store.apply_memories(parse(store, row, item, refs), store.epoch())[0]
    cls = store._class("cocoa", "relationship")
    store.save_classifier("cocoa", cls, revision=cls["revision"], confirm=True)
    assert store.get_memory(new)["status"] == "pending"
    assert store.get_memory(old)["status"] == "active"


def test_scoped_consolidation_filters_before_limit_and_keeps_background_cursor(
    store, monkeypatch
):
    from astrbot_plugin_persona_manager import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "time", SimpleNamespace(time=store.clock))
    with store.transaction():
        for index in range(501):
            source(store, f"o{index:04}", scope="qq:bot:other")
            source(store, f"a{index:04}", purpose="observation")
        row = source(store, "z-target", content="我喜欢喝茶")
    before = [
        tuple(item)
        for item in store.db.execute(
            "SELECT source_id,lane,status FROM learning_queue WHERE source_id<>'z-target' ORDER BY source_id,lane"
        )
    ]
    settings = Settings(
        persona_id="cocoa",
        memory_batch_threshold=1,
        observation_groups=frozenset({"group"}),
        thinking_notice=False,
    )
    context = FakeContext()
    runtime = Runtime(store, context, lambda: settings, FakeLogger())
    cursor = (store.clock() + 86400, "past-target")
    runtime._scan_after = cursor
    asyncio.run(runtime.run_once(scope_key=row["scope_key"]))
    assert len(context.calls) == 1
    records = json.loads(context.calls[0]["prompt"].split("原始消息 JSON：\n")[1])
    assert [record["id"] for record in records] == ["z-target"]
    assert runtime._scan_after == cursor
    assert before == [
        tuple(item)
        for item in store.db.execute(
            "SELECT source_id,lane,status FROM learning_queue WHERE source_id<>'z-target' ORDER BY source_id,lane"
        )
    ]
    assert (
        store.db.execute(
            "SELECT status FROM learning_queue WHERE source_id='z-target' AND lane='interaction'"
        ).fetchone()[0]
        == "done"
    )
