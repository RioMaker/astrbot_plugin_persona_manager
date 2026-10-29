"""Regression cases for mutations concurrent with context assembly."""

import asyncio
import logging
import time

import pytest
from astrbot_plugin_persona_manager.runtime import Runtime
from astrbot_plugin_persona_manager.settings import Settings
from astrbot_plugin_persona_manager.storage import Store


@pytest.mark.parametrize("mutation", ["forget", "correct"])
def test_context_drops_snapshot_invalidated_after_profile_read(tmp_path, mutation):
    store = Store(tmp_path / "review.sqlite3")
    source = {
        "id": "source",
        "persona_id": "cocoa",
        "platform_id": "qq1",
        "bot_id": "9999",
        "group_id": "8888",
        "user_id": "1234",
        "text": "I prefer tea",
        "sent_at": time.time(),
        "purpose": "interaction",
        "scope_key": "qq1:9999:8888",
    }
    store.add_source(source)
    memory_id = store.apply_memories(
        [
            {
                "source_ids": [source["id"]],
                "kind": "preference",
                "key": "drink",
                "content": "WITHDRAWN_PRIVATE_RECOLLECTION",
                "state": "active",
            }
        ],
        store.epoch(),
    )[0]
    runtime = Runtime(
        store,
        object(),
        lambda: Settings(persona_id="cocoa", memory_groups=frozenset({"8888"})),
        logging.getLogger(__name__),
    )
    original_db = runtime.db

    async def mutate_after_read(method, *args, **kwargs):
        result = await original_db(method, *args, **kwargs)
        if method == "profile":
            if mutation == "forget":
                store.forget("cocoa", "1234")
            else:
                store.correct(memory_id, "Corrected preference")
        return result

    runtime.db = mutate_after_read
    try:
        context, trace = asyncio.run(runtime.chat_context(source))
        assert "WITHDRAWN_PRIVATE_RECOLLECTION" not in context
        assert memory_id not in trace["memory_ids"]
        assert (
            store.get_memory(memory_id) is None
            if mutation == "forget"
            else (store.get_memory(memory_id)["status"] == "corrected")
        )
    finally:
        store.close()
