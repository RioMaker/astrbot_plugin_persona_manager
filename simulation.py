"""Short-lived, authenticated Dashboard chat jobs, isolated from QQ and learning."""

import asyncio
import json
import os
import secrets
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from .simulation_models import MODELS, ModelFiles

MAX_ROUNDS = 6
MAX_OUTPUT = 160
CONFIGURED = "configured"


class SimulationBusy(ValueError):
    pass


def chat_messages(payload):
    prompt = payload.get("message")
    history = payload.get("history", [])
    if not isinstance(prompt, str) or not 0 < len(prompt.strip()) <= 1200:
        raise ValueError("请输入 1～1200 字的消息")
    if (
        not isinstance(history, list)
        or len(history) > 2 * MAX_ROUNDS
        or len(history) % 2
    ):
        raise ValueError("模拟历史最多包含最近六轮完整对话")
    messages = []
    for i, row in enumerate(history):
        role = "assistant" if i % 2 else "user"
        if not isinstance(row, dict) or row.get("role") != role:
            raise ValueError("模拟历史的消息顺序无效")
        content = row.get("content")
        if not isinstance(content, str) or not 0 < len(content.strip()) <= (
            4000 if i % 2 else 1200
        ):
            raise ValueError("模拟历史包含无效正文")
        messages.append({"role": role, "content": content.strip()})
    return [*messages, {"role": "user", "content": prompt.strip()}]


@dataclass
class Job:
    owner: str
    model: str
    question: str
    persona: str
    prompt_source: str
    id: str = field(default_factory=lambda: secrets.token_hex(16))
    status: str = "running"
    detail: str = "准备模拟对话"
    answer: str = ""
    finished: float = 0
    stopping: bool = False

    def progress(self, detail):
        self.detail = detail

    def view(self):
        return {
            "id": self.id,
            "model": self.model,
            "persona": self.persona,
            "question": self.question,
            "prompt_source": self.prompt_source,
            "status": self.status,
            "detail": self.detail,
            "answer": self.answer,
        }


class Simulation:
    def __init__(self, directory, context):
        self.files = ModelFiles(directory)
        self.context = context
        self.jobs = {}
        self.active = None
        self.task = None
        self.closed = False

    def models(self, provider):
        choices = [
            {"id": name, "label": f"{name} · 原版 · {asset.size / 1e9:.2f} GB"}
            for name, asset in MODELS.items()
        ]
        if provider:
            choices.append({"id": CONFIGURED, "label": f"自定义模型 · {provider}"})
        return choices

    def find(self, owner, job_id):
        job = self.jobs.get(job_id) if isinstance(job_id, str) else None
        if (
            job is None
            or job.owner != owner
            or (job.finished and time.monotonic() - job.finished >= 900)
        ):
            raise ValueError("模拟任务已过期或不存在")
        return job

    def start(self, owner, model, messages, persona, system, source, provider):
        if model not in MODELS and not (model == CONFIGURED and provider):
            raise ValueError("请选择可用的模拟模型")
        if self.closed or self.active is not None:
            raise SimulationBusy("模拟引擎正在处理另一个请求，请稍后重试")
        now = time.monotonic()
        self.jobs = {k: v for k, v in self.jobs.items() if now - v.finished < 900}
        while len(self.jobs) >= 8:
            self.jobs.pop(next(iter(self.jobs)))
        job = Job(owner, model, messages[-1]["content"], persona, source)
        self.jobs[job.id] = self.active = job
        self.task = asyncio.create_task(self._run(job, messages, system, provider))
        return job.view()

    async def _run(self, job, messages, system, provider):
        try:
            if job.model == CONFIGURED:
                job.progress("自定义模型正在回复")
                result = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=provider,
                        system_prompt=system,
                        contexts=messages[:-1],
                        prompt=messages[-1]["content"],
                        tools=None,
                        max_tokens=MAX_OUTPUT,
                    ),
                    timeout=360,
                )
                job.answer = (getattr(result, "completion_text", "") or "").strip()
            else:
                binary, weights = await self.files.prepare(job.model, job.progress)
                await _native_reply(binary, weights, system, messages, job)
            if not job.answer.strip():
                raise ValueError("模型没有返回正文，请重试")
            job.status, job.detail = "done", "回复完成"
        except asyncio.CancelledError:
            job.status, job.detail = "cancelled", "已停止"
        except ValueError as exc:
            job.status, job.detail = (
                "error",
                str(exc)
                if job.model != CONFIGURED
                else "自定义模型调用失败，请检查已配置提供商的连接和模型名",
            )
        except Exception as exc:
            job.status, job.detail = (
                "error",
                f"模拟调用失败（{type(exc).__name__}），请检查模型服务、磁盘余量或网络后重试",
            )
        finally:
            job.finished = time.monotonic()
            self.active = None

    async def stop(self):
        task, job = self.task, self.active
        if task and job:
            if not job.stopping:
                job.stopping = True
                job.detail = "正在停止"
                task.cancel()
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
            # Cancellation can arrive before the coroutine's first instruction.
            if self.active is job:
                job.status, job.detail, job.finished = (
                    "cancelled",
                    "已停止",
                    time.monotonic(),
                )
                self.active = None

    async def close(self):
        self.closed = True
        await self.stop()
        self.jobs.clear()


async def _native_reply(binary, weights, system, messages, job):
    job.progress(f"加载 {job.model}")
    with tempfile.TemporaryDirectory(prefix="persona-sim-") as run_dir:
        socket = Path(run_dir) / "chat.sock"
        lib_path = os.pathsep.join(
            filter(None, (str(binary.parent), os.environ.get("LD_LIBRARY_PATH")))
        )
        async with _engine_process(
            str(binary),
            "--model",
            str(weights),
            "--host",
            str(socket),
            "--threads",
            "2",
            "--threads-batch",
            "2",
            "--ctx-size",
            "4096",
            "--parallel",
            "1",
            "--cache-ram",
            "0",
            "--n-predict",
            str(MAX_OUTPUT),
            "--no-webui",
            "--no-warmup",
            "--log-disable",
            "--jinja",
            "--chat-template-kwargs",
            '{"enable_thinking":false}',
            env={**os.environ, "LD_LIBRARY_PATH": lib_path},
            cwd=binary.parent,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        ) as proc:
            async with aiohttp.ClientSession(
                connector=aiohttp.UnixConnector(path=str(socket)),
                timeout=aiohttp.ClientTimeout(total=360),
            ) as client:
                deadline = time.monotonic() + 150
                while time.monotonic() < deadline:
                    if proc.returncode is not None:
                        raise ValueError(
                            f"CPU 引擎启动失败（退出码 {proc.returncode}），请检查可用内存及系统是否支持运行 Linux x86_64 程序"
                        )
                    try:
                        async with client.get(
                            "http://localhost/health", timeout=2
                        ) as check:
                            if check.status == 200:
                                break
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        pass
                    await asyncio.sleep(0.3)
                else:
                    raise ValueError("加载模型超时，请检查服务器内存及 CPU 占用")
                job.progress(f"{job.model} 正在回复")
                await _stream(client, system, messages, job)


@asynccontextmanager
async def _engine_process(*args, **kwargs):
    starting = asyncio.create_task(asyncio.create_subprocess_exec(*args, **kwargs))
    try:
        yield await asyncio.shield(starting)
    finally:
        # A stop can arrive while the OS is creating the child, before it has a handle.
        proc = await starting
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


async def _stream(client, system, messages, job):
    while True:
        body = {
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": True,
            "max_tokens": MAX_OUTPUT,
        }
        async with client.post(
            "http://localhost/v1/chat/completions", json=body
        ) as response:
            if response.status != 200:
                error = (await response.json()).get("error", {})
                if error.get("type") == "exceed_context_size_error":
                    if len(messages) > 1:
                        messages = messages[2:]
                        continue
                    raise ValueError("当前人设和消息超过 4096 tokens，请缩短后重试")
                raise ValueError(f"本地引擎返回 HTTP {response.status}")
            async for line in response.content:
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    return
                chunk = json.loads(data)
                if "error" in chunk:
                    raise ValueError("模型生成中断，请重试")
                for choice in chunk.get("choices", []):
                    job.answer += choice.get("delta", {}).get("content") or ""
            raise ValueError("服务在回复完成前断开，请重试")
