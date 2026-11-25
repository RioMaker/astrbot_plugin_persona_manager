"""Observe only an event's private Bailian adapter, including tool continuations."""

from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial


class TrialProvider(ProviderOpenAIOfficial):
    async def _query(self, payloads, tools, **kwargs):
        with self.turn.model_call(payloads) as step:
            response = await super()._query(payloads, tools, **kwargs)
            step.response(response)
            return response

    async def _query_stream(self, payloads, tools, **kwargs):
        with self.turn.model_call(payloads) as step:
            async for response in super()._query_stream(payloads, tools, **kwargs):
                # AstrBot emits one assembled completion after its deltas. Its
                # usage covers the whole call, including a usage-only tail.
                if response.is_chunk:
                    step.chunk(response)
                else:
                    step.response(response)
                yield response
