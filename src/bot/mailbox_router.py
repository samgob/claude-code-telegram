"""Mailbox routing: deterministic feedback capture for waiting sessions.

Phase 2 of the no-watchers notification architecture. A Desktop session
(or routine) that wants feedback registers a *mailbox* when it relays a
notification through /webhooks/routine: a file path to append replies to,
a topic string for the router, a scope ("private" = owner DM only,
"family" = also routable from the family group chat), and a TTL.

Two capture paths, both landing in the mailbox file as JSONL:

1. Strong binding (deterministic, no model): a Telegram swipe-reply to a
   relayed notification whose row links a live mailbox. The daemon appends
   the reply directly in Python — this deliberately bypasses the per-turn
   tool gate (GROUP_CHAT_RESTRICTED_TOOLS strips Write/Bash on non-owner
   turns, so a model-mediated write could never work for V's turns).
2. Contextual routing (classifier): a bare message is classified against
   the live mailboxes for the chat's scope. Only a "high" confidence
   verdict routes; anything else falls through to normal chat. Routed
   messages are ALSO answered conversationally by the chat session (tee).

The waiting session watches its mailbox file (Monitor / ScheduleWakeup)
and picks up appended feedback.
"""

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

# Router model: Sonnet carve-out (logged per operating principles) —
# high-frequency per-message classification, low-stakes (any failure or
# low confidence falls through to normal chat handling, and every route
# is surfaced with a visible ack), verifiable by the ack + mailbox line.
ROUTER_MODEL = "claude-sonnet-5"
ROUTER_TIMEOUT_SECONDS = 20

_ROUTER_SYSTEM_PROMPT = (
    "You are a message router. You will be given a chat message and a "
    "numbered list of waiting sessions, each with a topic description. "
    "Decide whether the message is clearly feedback or input intended "
    "for one of those sessions, or just ordinary conversation.\n"
    "Respond with ONLY a JSON object, no other text:\n"
    '{"target": "<routine name or null>", "confidence": "high|medium|low"}\n'
    'Use "high" ONLY when the message unmistakably belongs to that '
    "session's topic. Greetings, questions to the assistant, and "
    "ambiguous messages are target null. When in doubt: null."
)


def append_to_mailbox(
    mailbox_path: str,
    sender: str,
    text: str,
    approved_directory: Path,
    via: str,
) -> bool:
    """Append one feedback line to a mailbox file. Returns success.

    Pure-Python append (JSONL) so it works on any sender's turn regardless
    of the per-turn tool gate. The path must live under the approved
    directory — same isolation boundary as the rest of the bot.
    """
    try:
        path = Path(mailbox_path).resolve()
        approved = approved_directory.resolve()
        if not path.is_relative_to(approved):
            logger.warning(
                "Mailbox path outside approved directory — refusing",
                mailbox_path=mailbox_path,
            )
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                "sender": sender,
                "text": text,
                "via": via,  # "reply" (swipe-reply) or "router"
            },
            ensure_ascii=False,
        )
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except Exception:
        logger.warning(
            "Mailbox append failed", mailbox_path=mailbox_path, exc_info=True
        )
        return False


async def classify_route(
    message_text: str,
    mailboxes: List[Dict[str, Any]],
    cli_path: Optional[str] = None,
) -> Optional[str]:
    """Classify a bare message against live mailboxes.

    Returns the routine name to route to, or None (which always means:
    handle as normal chat). Fails open — timeout, parse error, or any
    exception is a None.
    """
    if not mailboxes:
        return None
    try:
        candidates = "\n".join(
            f"{i}. {b['routine']} — {b['topic']}" for i, b in enumerate(mailboxes, 1)
        )
        prompt = (
            f"Waiting sessions:\n{candidates}\n\n"
            f"Chat message:\n{message_text[:2000]}"
        )
        result_text = await asyncio.wait_for(
            _one_shot(prompt, cli_path), timeout=ROUTER_TIMEOUT_SECONDS
        )
        if not result_text:
            return None
        match = re.search(r"\{.*\}", result_text, re.DOTALL)
        if not match:
            return None
        verdict = json.loads(match.group(0))
        target = verdict.get("target")
        confidence = verdict.get("confidence")
        valid_names = {b["routine"] for b in mailboxes}
        if confidence == "high" and target in valid_names:
            logger.info("Mailbox router matched", target=target)
            return str(target)
        return None
    except asyncio.TimeoutError:
        logger.warning("Mailbox router timed out — falling through to chat")
        return None
    except Exception:
        logger.warning("Mailbox router failed — falling through to chat", exc_info=True)
        return None


async def _one_shot(prompt: str, cli_path: Optional[str]) -> Optional[str]:
    """Single tool-less SDK call; returns the result text.

    Iterates raw messages with per-message tolerant parsing (same pattern
    as ClaudeSDKManager) instead of the high-level query() helper, which
    raises MessageParseError on any message type its parser doesn't know
    (e.g. rate_limit_event from a newer CLI) and killed the whole
    classification — the 8/26 live-test failure.
    """
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
    from claude_agent_sdk._errors import MessageParseError
    from claude_agent_sdk._internal.message_parser import parse_message

    options = ClaudeAgentOptions(
        max_turns=1,
        model=ROUTER_MODEL,
        allowed_tools=[],
        disallowed_tools=["Bash", "Write", "Edit", "NotebookEdit", "WebFetch"],
        system_prompt=_ROUTER_SYSTEM_PROMPT,
        cli_path=cli_path or None,
    )
    client = ClaudeSDKClient(options)
    await client.connect()
    try:
        await client.query(prompt)
        if client._query is None:  # connect() always sets this; narrow for mypy
            return None
        async for raw_data in client._query.receive_messages():
            try:
                message = parse_message(raw_data)
            except MessageParseError:
                continue
            if isinstance(message, ResultMessage):
                text = getattr(message, "result", None)
                if isinstance(text, str) and text.strip():
                    return text
                return None
        return None
    finally:
        await client.disconnect()
