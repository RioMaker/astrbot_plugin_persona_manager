"""Expose a Bailian adapter to AstrBot's native runner for one QQ event."""

import asyncio
from uuid import uuid4

from . import simulation_bailian
from .trial_trace import TrialHistory


class GroupTrialProviders:
    def __init__(self, context, logger):
        self.context = context
        self.logger = logger
        self.active = {}
        self.closing = set()
        self.closed = False
        self.history = TrialHistory()

    def bind(self, config, workspace, model, turn):
        if self.closed:
            raise RuntimeError("插件正在卸载")
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("缺少处理本次群消息的任务")
        registry = self.context.provider_manager.inst_map
        # The native runner resolves selected_provider by ID. Publish a private
        # lookup for this event only, outside the default/fallback provider lists.
        # Keep the deployment ID stable in the model's own usage metadata.
        deployment = f"bailian:{workspace}:{model}"
        alias = f"persona-trial/{model}/{uuid4().hex}"
        provider = simulation_bailian.create_agent_provider(
            config, workspace, model, deployment, turn=turn
        )
        registry[alias] = provider
        self.active[alias] = registry, provider, turn
        # Both supported pipelines await the hook in the message scheduler task.
        # Its completion also covers stops, tool-only replies and cancellations.
        task.add_done_callback(
            lambda done: self.release(alias, cancelled=done.cancelled())
        )
        return alias

    def release(self, alias, *, cancelled=False):
        lease = self.active.pop(alias, None)
        if lease is None:
            return
        registry, provider, turn = lease
        turn.close("cancelled" if cancelled else None)
        if registry.get(alias) is provider:
            del registry[alias]
        closing = asyncio.create_task(self._terminate(provider))
        self.closing.add(closing)
        closing.add_done_callback(self.closing.discard)

    async def _terminate(self, provider):
        try:
            await provider.terminate()
        except Exception:
            self.logger.warning("[可可子人格] 回收百炼测试模型连接失败。")

    async def close(self):
        self.closed = True
        for alias in list(self.active):
            self.release(alias, cancelled=True)
        await asyncio.gather(*self.closing)
        self.history.turns.clear()
