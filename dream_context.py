"""Apply each dream once per conversation and prevent stale history writeback."""

import hashlib
import json

REQUEST_KEY = "astrbot_plugin_persona_manager:dream_request"
# AstrBot 4.29.x's own group-history block, already injected before our hook.
GROUP_HISTORY_PREFIX = (
    "<system_reminder>You are in a group chat. "
    "Belows are group chat context after your last reply:\n--- BEGIN CONTEXT---\n"
)


class DreamContext:
    def __init__(self, runtime):
        self.runtime = runtime

    async def generation(self, persona_id):
        return await self.runtime.db("get_meta", "dream_context:" + persona_id, 0)

    @staticmethod
    def applied_key(persona_id, umo, cid):
        digest = hashlib.sha256(repr((persona_id, umo, cid)).encode()).hexdigest()
        return "dream_context_applied:" + digest

    @staticmethod
    def clear_loaded_history(req, conversation):
        history = getattr(conversation, "history", None)
        if isinstance(history, str):
            try:
                history = json.loads(history)
            except ValueError:
                history = None
        contexts = req.contexts
        if isinstance(history, list) and isinstance(contexts, list):
            for index in range(len(contexts) - len(history) + 1):
                if contexts[index : index + len(history)] == history:
                    # Persona opening examples are prepended and current file
                    # extracts are appended before this hook. Keep both.
                    del contexts[index : index + len(history)]
                    break
            else:
                req.contexts = []
        else:
            req.contexts = []
        conversation.history = "[]"

    async def prepare(self, event, req, persona_id):
        """Called only for authorized main chat requests, under runtime.lock."""
        generation = await self.generation(persona_id)
        conversation = getattr(req, "conversation", None)
        cid = getattr(conversation, "cid", None)
        if generation and conversation is not None and not cid:
            cid = await self.runtime.context.conversation_manager.get_curr_conversation_id(
                event.unified_msg_origin
            )
        state = {
            "request": req,
            "persona_id": persona_id,
            "generation": generation,
        }
        event.set_extra(REQUEST_KEY, state)
        if not generation or conversation is None or not cid:
            return
        applied_key = self.applied_key(persona_id, event.unified_msg_origin, cid)
        if await self.runtime.db("get_meta", applied_key, 0) >= generation:
            return
        # Clear the loaded request as well as storage: the core already read its
        # history before this hook. Never acknowledge a failed storage update.
        self.clear_loaded_history(req, conversation)
        parts = req.extra_user_content_parts
        parts[:] = [
            part
            for part in parts
            if not getattr(part, "text", "").startswith(GROUP_HISTORY_PREFIX)
        ]
        try:
            await self.runtime.context.conversation_manager.update_conversation(
                event.unified_msg_origin, cid, history=[]
            )
        except Exception:
            req.conversation = None
            raise
        await self.runtime.db("set_meta", applied_key, generation)

    async def discard_stale(self, event):
        """A reply started before a dream must not save its old message buffer."""
        state = event.get_extra(REQUEST_KEY, None)
        if not state:
            return
        generation = await self.generation(state["persona_id"])
        if state["generation"] == generation:
            return
        req = state["request"]
        # AstrBot skips saving a request without a bound conversation. This also
        # covers a dream tool called during the current agent's tool loop.
        req.contexts = []
        req.conversation = None
        # Storage is reset by the next fresh request. Never erase it here: a
        # newer turn or a different persona may already be using this dialogue.
