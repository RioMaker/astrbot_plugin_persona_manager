"""Read Core's successful current-session send receipts without sending anything.

AstrBot v4.29.4 writes these receipts only after send_message succeeds. RespondStage
then skips duplicate plain replies before after_message_sent, and an empty final
reply skips on_decorating_result as well. The tool-end hook is needed for that case.

Contract sources (tag v4.29.4): core/tools/message_tools.py:339-364,
core/message/message_event_result.py:149-158, core/astr_agent_hooks.py:56-70,
core/pipeline/respond/stage.py:213-235. No tool argument is a delivery receipt.
"""

SENT_TEXTS_KEY = "_send_message_to_user_current_session_plain_texts"


def _confirmed_text(event, text):
    if not getattr(event, "_has_send_oper", False) or not text:
        return None
    receipts = event.get_extra(SENT_TEXTS_KEY, None)
    if not isinstance(receipts, list) or text not in receipts:
        return None
    return text


def confirmed_result_delivery(event, result):
    """Return only text Core has already delivered and would deduplicate."""
    chain = getattr(result, "chain", None)
    if not chain or any(
        type(part).__name__ not in {"Plain", "At", "Reply"} for part in chain
    ):
        return None
    # Core joins individual Plain components with spaces, including at dedupe.
    plain = [part.text for part in chain if type(part).__name__ == "Plain"]
    if not all(isinstance(part, str) for part in plain):
        return None
    return _confirmed_text(event, " ".join(plain).strip())


def confirmed_tool_delivery(event, tool, arguments, result):
    """Accept the built-in sender's successful, current-session plain-text call.

    The successful CallToolResult and exact matching receipt must both exist.
    Media-only/mixed sends, other sessions, errors, and guessed tool arguments
    cannot manufacture a user-dialogue record through this path.
    """
    if getattr(tool, "name", None) != "send_message_to_user":
        return None
    if not isinstance(arguments, dict) or result is None:
        return None
    if getattr(result, "isError", False):
        return None
    current = str(getattr(event, "unified_msg_origin", ""))
    if not current:
        return None
    target = arguments.get("session") or current
    if not isinstance(target, str) or not (
        target == current
        or (":" not in target and target == current.rsplit(":", 1)[-1])
    ):
        return None
    contents = getattr(result, "content", None)
    if not isinstance(contents, list) or len(contents) != 1:
        return None
    content = contents[0]
    if (
        getattr(content, "type", None) != "text"
        or getattr(content, "text", None) != f"Message sent to session {current}"
    ):
        return None
    messages = arguments.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    texts = []
    for message in messages:
        if (
            not isinstance(message, dict)
            or str(message.get("type", "")).lower() != "plain"
        ):
            return None
        # Match the sender's str(...).strip() conversion, then MessageChain join.
        value = str(message.get("text", "")).strip()
        if not value:
            return None
        texts.append(value)
    return _confirmed_text(event, " ".join(texts).strip())
