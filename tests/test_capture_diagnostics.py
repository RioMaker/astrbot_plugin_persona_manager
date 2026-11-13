"""Zero memories must be distinguishable from zero collection or pending review."""

import asyncio
import json

import pytest
from astrbot_plugin_persona_manager.ingress import sample
from astrbot_plugin_persona_manager.tests.astrbot_stub import MockEvent
from astrbot_plugin_persona_manager.tests.test_integration import entry as entry
from astrbot_plugin_persona_manager.tests.test_integration import request, route
from astrbot_plugin_persona_manager.tests.test_memory_tools import remember


@pytest.mark.parametrize(
    "case",
    [
        "missing_gate_marker",
        "persona_mismatch",
        "default_llm_suppressed",
        "auxiliary_request",
        "paused",
        "stored",
    ],
)
def test_capture_explains_missing_or_saved_dialogue_without_exposing_text(entry, case):
    async def run():
        async with entry.running() as env:
            body = "今天练了一会儿太鼓，现在休息"
            event = MockEvent(body, extras={"access_gate_original_text": body})
            req = request()
            if case == "missing_gate_marker":
                event.extras.clear()
            elif case == "persona_mismatch":
                req.conversation.persona_id = "其他人格"
            elif case == "paused":
                env.plugin.store.set_meta("paused", True)
            elif case == "default_llm_suppressed":
                event.call_llm = True
            elif case == "auxiliary_request":
                event.set_extra("provider_request", request())
            await env.plugin.on_request(event, req)
            if case in {"paused", "stored"}:
                assert env.plugin._capture["state"] == "awaiting_delivery"
            await env.plugin.after_sent(event)
            result = await route(env.context, "state")()
            status = result["status"]
            assert status["runtime_version"] == "2.2.2"
            assert status["capture"]["state"] == case
            assert body not in json.dumps(status["capture"], ensure_ascii=False)
            assert status["pending_interactions"] == (1 if case == "stored" else 0)
            assert status["sources"] == (1 if case == "stored" else 0)
            env.context.llm_generate.assert_not_called()

    asyncio.run(run())


def test_diagnostics_separate_pending_knowledge_from_queue_and_other_persona(entry):
    async def run():
        async with entry.running() as env:
            _, memory = await remember(env, category="knowledge")
            assert memory["status"] == "pending"
            foreign = sample(
                MockEvent("其他人格的原文", message_id="foreign"),
                env.plugin.settings(),
                "interaction",
            )
            foreign["persona_id"] = "其他人格"
            env.plugin.store.add_source(foreign)
            status = (await route(env.context, "state")())["status"]
            assert status["sources"] == 1
            assert status["pending_interactions"] == 1
            assert status["memories"] == 0
            assert status["pending_memories"] == 1
            assert env.plugin.store.stats()["sources"] == 2
            assert status["capture"]["state"] == "waiting_request"

    asyncio.run(run())
