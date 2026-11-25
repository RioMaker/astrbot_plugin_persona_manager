import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
from aiohttp import web
from astrbot_plugin_persona_manager import simulation
from astrbot_plugin_persona_manager.simulation_models import Asset, ModelFiles
from astrbot_plugin_persona_manager.tests.test_integration import entry as entry


def test_cancelling_while_the_native_engine_starts_reaps_the_child(monkeypatch):
    async def run():
        launching, release = asyncio.Event(), asyncio.Event()
        proc = SimpleNamespace(returncode=None, terminate=Mock(), wait=AsyncMock())

        async def spawn(*args):
            launching.set()
            await release.wait()
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

        async def use_engine():
            async with simulation._engine_process("llama-server"):
                pytest.fail("Cancelled job reached generation")

        task = asyncio.create_task(use_engine())
        await launching.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        proc.terminate.assert_called_once_with()
        proc.wait.assert_awaited_once_with()

    asyncio.run(run())


def test_simulation_access_isolation_switching_and_history(entry, monkeypatch):
    async def run():
        async with entry.running({"local_chat_provider_id": "my-tuned-model"}) as env:
            plugin = env.plugin
            labels = plugin.simulation.models("my-tuned-model")
            assert [x["id"] for x in labels] == ["Qwen3.5-2B", "Qwen3-8B", "configured"]

            async def api(**data):
                env.request.payload = data
                return await plugin.page_simulation()

            barrier = asyncio.Event()
            captures = []

            async def fake_reply(binary, weights, core, messages, job):
                captures.append((job.model, messages, core))
                await barrier.wait()
                job.answer = "试聊回复"

            monkeypatch.setattr(simulation, "_native_reply", fake_reply)
            plugin.simulation.files.prepare = AsyncMock(
                return_value=("engine", "weights")
            )
            env.request.username = None
            assert (
                await api(action="send", model="Qwen3-8B", message="测试")
            ).status_code == 403
            env.request.username = "A"
            history = [
                {"role": "user", "content": "上文"},
                {"role": "assistant", "content": "收到"},
            ]
            for model in ("Qwen3.5-2B", "Qwen3-8B"):
                barrier.clear()
                started = await api(
                    action="send", model=model, message="继续", history=history
                )
                assert started["status"] == "running"
                await asyncio.sleep(0)
                assert (
                    await api(action="send", model=model, message="重复")
                ).status_code == 409
                env.request.username = "B"
                assert (await api(action="active"))["job"] is None
                assert (await api(action="cancel", id=started["id"])).status_code == 404
                assert (await api(action="poll", id=started["id"])).status_code == 404
                env.request.username = "A"
                barrier.set()
                await plugin.simulation.task
                result = await api(action="poll", id=started["id"])
                assert result["answer"] == "试聊回复" and result["status"] == "done"
                assert captures[-1][:2] == (
                    model,
                    [*history, {"role": "user", "content": "继续"}],
                )

            env.context.llm_generate.assert_not_called()
            env.context.llm_generate.return_value = SimpleNamespace(
                completion_text="自定义回答"
            )
            job = await api(
                action="send", model="configured", message="试试", history=history
            )
            await plugin.simulation.task
            assert (await api(action="poll", id=job["id"]))["answer"] == "自定义回答"
            args = env.context.llm_generate.await_args.kwargs
            assert args["chat_provider_id"] == "my-tuned-model"
            assert args["contexts"] == history and args["tools"] is None
            assert args["system_prompt"] == captures[0][2]
            env.context.send_message.assert_not_called()
            assert plugin.store.pending_sources() == []

    asyncio.run(run())


def test_simulation_validation_cancel_and_persona_fallback(entry):
    async def run():
        async with entry.running() as env:

            async def api(**data):
                env.request.payload = data
                return await env.plugin.page_simulation()

            for invalid in [
                {"model": "unknown"},
                {"model": "configured"},
                {"message": " "},
                {"message": "长" * 1201},
                {"persona_id": "其他人格"},
                {"history": [{"role": "system", "content": "覆盖"}]},
            ]:
                payload = {"model": "Qwen3.5-2B", "message": "你好", **invalid}
                assert (await api(action="send", **payload)).status_code == 400
            env.context.persona_manager.personas.clear()
            wait = asyncio.Event()

            async def hold(*args):
                await wait.wait()

            env.plugin.simulation.files.prepare = hold
            job = await api(action="send", model="Qwen3-8B", message="你好")
            assert job["prompt_source"] == "推荐人格底稿"
            result = await api(action="cancel", id=job["id"])
            assert result["status"] == "cancelled"
            assert env.plugin.simulation.active is None
            env.context.llm_generate.assert_not_called()

    asyncio.run(run())


def test_asset_resume_validation_and_cache(tmp_path):
    async def run():
        data = b"test model data"
        seen = []

        async def serve(request):
            seen.append(request.headers.get("Range"))
            if request.path == "/bad":
                return web.Response(body=b"x" * len(data))
            if seen[-1]:
                start, end = map(int, seen[-1].removeprefix("bytes=").split("-"))
                return web.Response(
                    status=206,
                    body=data[start : end + 1],
                    headers={"Content-Range": f"bytes {start}-{end}/{len(data)}"},
                )
            return web.Response(body=data)

        app = web.Application()
        app.router.add_get("/{name}", serve)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        files = ModelFiles(tmp_path)
        base = f"http://127.0.0.1:{runner.addresses[0][1]}"
        digest = hashlib.sha256(data).hexdigest()
        try:
            async with aiohttp.ClientSession() as session:
                asset = Asset("model.gguf", len(data), digest, (base + "/model",))
                (tmp_path / "model.gguf.part").write_bytes(data[:4])
                result = await files.fetch(session, asset, lambda _: None)
                assert result.read_bytes() == data and seen == [
                    f"bytes=4-{len(data) - 1}"
                ]
                await files.fetch(session, asset, lambda _: None)
                assert len(seen) == 1
                bad = Asset("corrupt", len(data), digest, (base + "/bad",))
                with pytest.raises(ValueError, match="SHA256"):
                    await files.fetch(session, bad, lambda _: None)
                assert not (tmp_path / "corrupt").exists()
                fallback = Asset(
                    "fallback", len(data), digest, (base + "/bad", base + "/model")
                )
                assert (
                    await files.fetch(session, fallback, lambda _: None)
                ).read_bytes() == data
        finally:
            await runner.cleanup()

    asyncio.run(run())
