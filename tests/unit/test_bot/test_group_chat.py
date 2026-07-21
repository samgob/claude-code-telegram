"""Tests for shared group-chat support.

Covers:
- Group allowlist gating in the middleware wrapper (non-allowlisted groups
  are silently dropped before any middleware can reply)
- Auth middleware group behavior (unauthorized member gets one lifetime
  notice with their user id; authorized members pass through)
- Shared session keying (group session state lives in chat_data and is owned
  by the chat id, so all members share one Claude session)
- Sender attribution ("[Name]: ..." prefix in groups, never in DMs)
- Group model override (GROUP_CHAT_MODEL applies to groups only)
"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import ApplicationHandlerStop

import src.bot.middleware.auth as auth_module
from src.bot.core import ClaudeCodeBot
from src.bot.middleware.auth import auth_middleware
from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config

GROUP_ID = -1001234567890
OTHER_GROUP_ID = -1009999999999
GROUP_MODEL = "claude-opus-4-8"


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


OWNER_ID = 123  # Sam
MEMBER_ID = 456  # V
RESTRICTED_DEFAULT = ["Edit", "Write", "NotebookEdit", "Bash"]


@pytest.fixture
def group_settings(tmp_dir):
    return create_test_config(
        approved_directory=str(tmp_dir),
        agentic_mode=True,
        group_chat_ids=[GROUP_ID],
        group_chat_model=GROUP_MODEL,
        group_chat_owner_id=OWNER_ID,
    )


@pytest.fixture
def deps():
    return {
        "claude_integration": MagicMock(),
        "storage": MagicMock(),
        "security_validator": MagicMock(),
        "rate_limiter": MagicMock(),
        "audit_logger": MagicMock(),
    }


@pytest.fixture(autouse=True)
def _reset_group_notice_cache():
    """Isolate the once-per-lifetime unauthorized-notice cache between tests."""
    auth_module._notified_unauthorized_group_users.clear()
    yield
    auth_module._notified_unauthorized_group_users.clear()


def make_group_update(
    chat_id: int = GROUP_ID,
    user_id: int = 123,
    first_name: str = "Sam",
    text: str = "hello",
):
    """Build a mock Update originating from a group chat."""
    update = MagicMock()
    chat = MagicMock()
    chat.id = chat_id
    chat.type = "supergroup"
    chat.send_action = AsyncMock()
    update.effective_chat = chat
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.first_name = first_name
    update.effective_user.username = "sam_tg"
    update.effective_user.is_bot = False
    update.message.chat = chat
    update.message.text = text
    update.message.message_id = 1
    update.message.reply_text = AsyncMock()
    update.effective_message = update.message
    return update


def make_dm_update(user_id: int = 123, text: str = "hello"):
    """Build a mock Update originating from a private chat."""
    update = make_group_update(user_id=user_id, text=text)
    update.effective_chat.type = "private"
    update.effective_chat.id = user_id
    return update


# --- Middleware wrapper: group allowlist gate ---------------------------------


class TestGroupAllowlistGate:
    """Non-allowlisted groups are dropped silently before any middleware."""

    def _bot(self, group_settings):
        return ClaudeCodeBot(group_settings, {})

    async def test_non_allowlisted_group_dropped_silently(self, group_settings):
        bot = self._bot(group_settings)
        middleware_called = False

        async def middleware(handler, event, data):
            nonlocal middleware_called
            middleware_called = True
            return await handler(event, data)

        wrapper = bot._create_middleware_handler(middleware)
        update = make_group_update(chat_id=OTHER_GROUP_ID)
        context = MagicMock()
        context.bot_data = {}

        with pytest.raises(ApplicationHandlerStop):
            await wrapper(update, context)

        assert middleware_called is False
        update.message.reply_text.assert_not_called()

    async def test_allowlisted_group_passes_through(self, group_settings):
        bot = self._bot(group_settings)
        middleware_called = False

        async def middleware(handler, event, data):
            nonlocal middleware_called
            middleware_called = True
            return await handler(event, data)

        wrapper = bot._create_middleware_handler(middleware)
        update = make_group_update(chat_id=GROUP_ID)
        context = MagicMock()
        context.bot_data = {}

        await wrapper(update, context)

        assert middleware_called is True

    async def test_group_dropped_when_no_groups_configured(self, tmp_dir):
        settings = create_test_config(
            approved_directory=str(tmp_dir), agentic_mode=True
        )
        bot = ClaudeCodeBot(settings, {})

        async def middleware(handler, event, data):
            return await handler(event, data)

        wrapper = bot._create_middleware_handler(middleware)
        update = make_group_update(chat_id=GROUP_ID)
        context = MagicMock()
        context.bot_data = {}

        with pytest.raises(ApplicationHandlerStop):
            await wrapper(update, context)

    async def test_private_chat_unaffected_by_group_gate(self, group_settings):
        bot = self._bot(group_settings)
        middleware_called = False

        async def middleware(handler, event, data):
            nonlocal middleware_called
            middleware_called = True
            return await handler(event, data)

        wrapper = bot._create_middleware_handler(middleware)
        update = make_dm_update()
        context = MagicMock()
        context.bot_data = {}

        await wrapper(update, context)

        assert middleware_called is True


# --- Auth middleware: per-user gating inside allowlisted groups ---------------


class TestGroupAuth:
    """Group members must be individually authorized via allowed_users."""

    async def test_unauthorized_group_user_gets_one_notice(self):
        auth_manager = MagicMock()
        auth_manager.is_authenticated.return_value = False
        auth_manager.authenticate_user = AsyncMock(return_value=False)
        data = {"auth_manager": auth_manager, "audit_logger": None}

        handler = AsyncMock()
        update = make_group_update(user_id=555001, first_name="V")

        await auth_middleware(handler, update, data)

        handler.assert_not_called()
        update.effective_message.reply_text.assert_called_once()
        notice = update.effective_message.reply_text.call_args.args[0]
        assert "Not authorized yet" in notice
        assert "555001" in notice
        assert "ask Sam" in notice

        # Second message from the same user: silent (no second notice)
        update2 = make_group_update(user_id=555001, first_name="V")
        await auth_middleware(handler, update2, data)
        update2.effective_message.reply_text.assert_not_called()

    async def test_unauthorized_notice_is_per_user(self):
        auth_manager = MagicMock()
        auth_manager.is_authenticated.return_value = False
        auth_manager.authenticate_user = AsyncMock(return_value=False)
        data = {"auth_manager": auth_manager, "audit_logger": None}
        handler = AsyncMock()

        first = make_group_update(user_id=111)
        await auth_middleware(handler, first, data)
        first.effective_message.reply_text.assert_called_once()

        second = make_group_update(user_id=222)
        await auth_middleware(handler, second, data)
        second.effective_message.reply_text.assert_called_once()

    async def test_authorized_group_user_passes(self):
        auth_manager = MagicMock()
        auth_manager.is_authenticated.return_value = True
        auth_manager.refresh_session.return_value = True
        auth_manager.get_session.return_value = MagicMock(auth_provider="whitelist")
        data = {"auth_manager": auth_manager, "audit_logger": None}

        handler = AsyncMock()
        update = make_group_update(user_id=123)

        await auth_middleware(handler, update, data)

        handler.assert_called_once()
        update.effective_message.reply_text.assert_not_called()

    async def test_group_auth_success_suppresses_welcome_message(self):
        auth_manager = MagicMock()
        auth_manager.is_authenticated.return_value = False
        auth_manager.authenticate_user = AsyncMock(return_value=True)
        auth_manager.get_session.return_value = MagicMock(auth_provider="whitelist")
        data = {"auth_manager": auth_manager, "audit_logger": None}

        handler = AsyncMock()
        update = make_group_update(user_id=123)

        await auth_middleware(handler, update, data)

        handler.assert_called_once()
        update.effective_message.reply_text.assert_not_called()

    async def test_dm_auth_failure_message_unchanged(self):
        auth_manager = MagicMock()
        auth_manager.is_authenticated.return_value = False
        auth_manager.authenticate_user = AsyncMock(return_value=False)
        data = {"auth_manager": auth_manager, "audit_logger": None}

        handler = AsyncMock()
        update = make_dm_update(user_id=999)

        await auth_middleware(handler, update, data)

        handler.assert_not_called()
        update.effective_message.reply_text.assert_called_once()
        text = update.effective_message.reply_text.call_args.args[0]
        assert "Authentication Required" in text


# --- Orchestrator: shared session keying, attribution, model override ---------


def make_claude_response(session_id="session-group-1"):
    response = MagicMock()
    response.session_id = session_id
    response.content = "Hi both!"
    response.tools_used = []
    response.interrupted = False
    return response


def make_context(settings, claude_integration, chat_data=None):
    context = MagicMock()
    context.user_data = {}
    context.chat_data = chat_data if chat_data is not None else {}
    context.bot_data = {
        "settings": settings,
        "claude_integration": claude_integration,
        "storage": None,
        "rate_limiter": None,
        "audit_logger": None,
    }
    return context


class TestGroupSessionKeying:
    """One shared Claude session per group, keyed by chat id + chat_data."""

    async def test_group_text_uses_chat_scoped_session(self, group_settings, deps):
        orchestrator = MessageOrchestrator(group_settings, deps)
        claude_integration = AsyncMock()
        claude_integration.run_command = AsyncMock(return_value=make_claude_response())

        update = make_group_update(user_id=123, first_name="Sam", text="do a thing")
        progress_msg = AsyncMock()
        update.message.reply_text.return_value = progress_msg
        context = make_context(group_settings, claude_integration)
        context.chat_data["claude_session_id"] = "existing-shared-session"

        await orchestrator.agentic_text(update, context)

        kwargs = claude_integration.run_command.call_args.kwargs
        # Session owner is the (negative) group chat id — shared by all members
        assert kwargs["user_id"] == GROUP_ID
        # Resumes the session stored in chat_data (shared), not user_data
        assert kwargs["session_id"] == "existing-shared-session"
        # New session id written back to chat_data, never user_data
        assert context.chat_data["claude_session_id"] == "session-group-1"
        assert "claude_session_id" not in context.user_data

    async def test_two_members_share_the_same_session(self, group_settings, deps):
        orchestrator = MessageOrchestrator(group_settings, deps)
        claude_integration = AsyncMock()
        claude_integration.run_command = AsyncMock(
            return_value=make_claude_response("shared-1")
        )

        shared_chat_data = {}

        # Sam speaks first
        sam = make_group_update(user_id=123, first_name="Sam", text="hi")
        sam.message.reply_text.return_value = AsyncMock()
        ctx_sam = make_context(group_settings, claude_integration, shared_chat_data)
        await orchestrator.agentic_text(sam, ctx_sam)

        # V speaks next — different user, same chat_data (same group)
        v = make_group_update(user_id=456, first_name="V", text="hello")
        v.message.reply_text.return_value = AsyncMock()
        ctx_v = make_context(group_settings, claude_integration, shared_chat_data)
        await orchestrator.agentic_text(v, ctx_v)

        second_kwargs = claude_integration.run_command.call_args.kwargs
        # V resumes the session Sam's turn created
        assert second_kwargs["session_id"] == "shared-1"
        assert second_kwargs["user_id"] == GROUP_ID

    async def test_dm_session_keying_unchanged(self, group_settings, deps):
        orchestrator = MessageOrchestrator(group_settings, deps)
        claude_integration = AsyncMock()
        claude_integration.run_command = AsyncMock(
            return_value=make_claude_response("dm-session")
        )

        update = make_dm_update(user_id=123, text="hi")
        update.message.reply_text.return_value = AsyncMock()
        context = make_context(group_settings, claude_integration)

        await orchestrator.agentic_text(update, context)

        kwargs = claude_integration.run_command.call_args.kwargs
        assert kwargs["user_id"] == 123
        assert context.user_data["claude_session_id"] == "dm-session"
        assert "claude_session_id" not in context.chat_data

    async def test_group_new_resets_shared_session(self, group_settings, deps):
        orchestrator = MessageOrchestrator(group_settings, deps)
        update = make_group_update()
        context = MagicMock()
        context.user_data = {}
        context.chat_data = {"claude_session_id": "old-shared"}

        await orchestrator.agentic_new(update, context)

        assert context.chat_data["claude_session_id"] is None
        assert context.chat_data["force_new_session"] is True
        assert "claude_session_id" not in context.user_data


class TestSenderAttribution:
    """Group prompts carry a [Name]: prefix; DM prompts do not."""

    async def test_group_prompt_prefixed_with_first_name(self, group_settings, deps):
        orchestrator = MessageOrchestrator(group_settings, deps)
        claude_integration = AsyncMock()
        claude_integration.run_command = AsyncMock(return_value=make_claude_response())

        update = make_group_update(first_name="Sam", text="fix the bug")
        update.message.reply_text.return_value = AsyncMock()
        context = make_context(group_settings, claude_integration)

        await orchestrator.agentic_text(update, context)

        kwargs = claude_integration.run_command.call_args.kwargs
        assert kwargs["prompt"] == "[Sam]: fix the bug"

    async def test_dm_prompt_not_prefixed(self, group_settings, deps):
        orchestrator = MessageOrchestrator(group_settings, deps)
        claude_integration = AsyncMock()
        claude_integration.run_command = AsyncMock(return_value=make_claude_response())

        update = make_dm_update(text="fix the bug")
        update.message.reply_text.return_value = AsyncMock()
        context = make_context(group_settings, claude_integration)

        await orchestrator.agentic_text(update, context)

        kwargs = claude_integration.run_command.call_args.kwargs
        assert kwargs["prompt"] == "fix the bug"

    def test_attribute_sender_falls_back_to_username_then_id(
        self, group_settings, deps
    ):
        orchestrator = MessageOrchestrator(group_settings, deps)
        update = make_group_update()
        update.effective_user.first_name = None
        assert orchestrator._attribute_sender(update, "hi") == "[sam_tg]: hi"
        update.effective_user.username = None
        assert orchestrator._attribute_sender(update, "hi") == "[123]: hi"


class TestGroupModelOverride:
    """GROUP_CHAT_MODEL applies to group sessions only."""

    async def test_group_uses_model_override(self, group_settings, deps):
        orchestrator = MessageOrchestrator(group_settings, deps)
        claude_integration = AsyncMock()
        claude_integration.run_command = AsyncMock(return_value=make_claude_response())

        update = make_group_update()
        update.message.reply_text.return_value = AsyncMock()
        context = make_context(group_settings, claude_integration)

        await orchestrator.agentic_text(update, context)

        kwargs = claude_integration.run_command.call_args.kwargs
        assert kwargs["model"] == GROUP_MODEL

    async def test_dm_does_not_use_group_model(self, group_settings, deps):
        orchestrator = MessageOrchestrator(group_settings, deps)
        claude_integration = AsyncMock()
        claude_integration.run_command = AsyncMock(return_value=make_claude_response())

        update = make_dm_update()
        update.message.reply_text.return_value = AsyncMock()
        context = make_context(group_settings, claude_integration)

        await orchestrator.agentic_text(update, context)

        kwargs = claude_integration.run_command.call_args.kwargs
        assert kwargs["model"] is None

    async def test_no_override_when_unset(self, tmp_dir, deps):
        settings = create_test_config(
            approved_directory=str(tmp_dir),
            agentic_mode=True,
            group_chat_ids=[GROUP_ID],
        )
        orchestrator = MessageOrchestrator(settings, deps)
        claude_integration = AsyncMock()
        claude_integration.run_command = AsyncMock(return_value=make_claude_response())

        update = make_group_update()
        update.message.reply_text.return_value = AsyncMock()
        context = make_context(settings, claude_integration)

        await orchestrator.agentic_text(update, context)

        kwargs = claude_integration.run_command.call_args.kwargs
        assert kwargs["model"] is None


# --- Phase 2: per-sender tiered permissions -----------------------------------


async def _run_text_and_get_kwargs(orchestrator, settings, update):
    """Run agentic_text with a mocked Claude and return run_command kwargs."""
    claude_integration = AsyncMock()
    claude_integration.run_command = AsyncMock(return_value=make_claude_response())
    update.message.reply_text.return_value = AsyncMock()
    context = make_context(settings, claude_integration)
    await orchestrator.agentic_text(update, context)
    return claude_integration.run_command.call_args.kwargs


class TestPerSenderToolGating:
    """Non-owner group turns get restricted tools disallowed; owner/DMs don't."""

    async def test_owner_group_turn_unrestricted(self, group_settings, deps):
        orchestrator = MessageOrchestrator(group_settings, deps)
        update = make_group_update(user_id=OWNER_ID, first_name="Sam")

        kwargs = await _run_text_and_get_kwargs(orchestrator, group_settings, update)

        assert kwargs["disallowed_tools"] is None

    async def test_non_owner_group_turn_restricted(self, group_settings, deps):
        orchestrator = MessageOrchestrator(group_settings, deps)
        update = make_group_update(user_id=MEMBER_ID, first_name="V")

        kwargs = await _run_text_and_get_kwargs(orchestrator, group_settings, update)

        assert kwargs["disallowed_tools"] == RESTRICTED_DEFAULT

    async def test_dm_turn_never_restricted(self, group_settings, deps):
        """Even a non-owner user id is unrestricted in a DM."""
        orchestrator = MessageOrchestrator(group_settings, deps)
        update = make_dm_update(user_id=MEMBER_ID)

        kwargs = await _run_text_and_get_kwargs(orchestrator, group_settings, update)

        assert kwargs["disallowed_tools"] is None

    async def test_owner_unset_restricts_everyone(self, tmp_dir, deps):
        """Fail closed: without GROUP_CHAT_OWNER_ID all group turns restrict."""
        settings = create_test_config(
            approved_directory=str(tmp_dir),
            agentic_mode=True,
            group_chat_ids=[GROUP_ID],
        )
        orchestrator = MessageOrchestrator(settings, deps)
        update = make_group_update(user_id=OWNER_ID, first_name="Sam")

        kwargs = await _run_text_and_get_kwargs(orchestrator, settings, update)

        assert kwargs["disallowed_tools"] == RESTRICTED_DEFAULT

    async def test_custom_restricted_tools(self, tmp_dir, deps):
        settings = create_test_config(
            approved_directory=str(tmp_dir),
            agentic_mode=True,
            group_chat_ids=[GROUP_ID],
            group_chat_owner_id=OWNER_ID,
            group_chat_restricted_tools=["Bash"],
        )
        orchestrator = MessageOrchestrator(settings, deps)
        update = make_group_update(user_id=MEMBER_ID, first_name="V")

        kwargs = await _run_text_and_get_kwargs(orchestrator, settings, update)

        assert kwargs["disallowed_tools"] == ["Bash"]


class TestGroupPolicyPrompt:
    """Policy appendix is sent on group turns only."""

    async def test_group_turn_appends_default_policy(self, group_settings, deps):
        orchestrator = MessageOrchestrator(group_settings, deps)
        update = make_group_update(user_id=OWNER_ID)

        kwargs = await _run_text_and_get_kwargs(orchestrator, group_settings, update)

        policy = kwargs["append_system_prompt"]
        assert policy is not None
        assert "shared family chat" in policy
        assert "[Sam] is the account owner" in policy

    async def test_dm_turn_has_no_policy(self, group_settings, deps):
        orchestrator = MessageOrchestrator(group_settings, deps)
        update = make_dm_update(user_id=OWNER_ID)

        kwargs = await _run_text_and_get_kwargs(orchestrator, group_settings, update)

        assert kwargs["append_system_prompt"] is None

    async def test_policy_override(self, tmp_dir, deps):
        settings = create_test_config(
            approved_directory=str(tmp_dir),
            agentic_mode=True,
            group_chat_ids=[GROUP_ID],
            group_chat_owner_id=OWNER_ID,
            group_chat_policy="Custom house rules.",
        )
        orchestrator = MessageOrchestrator(settings, deps)
        update = make_group_update(user_id=MEMBER_ID)

        kwargs = await _run_text_and_get_kwargs(orchestrator, settings, update)

        assert kwargs["append_system_prompt"] == "Custom house rules."

    async def test_empty_policy_disables_appendix(self, tmp_dir, deps):
        settings = create_test_config(
            approved_directory=str(tmp_dir),
            agentic_mode=True,
            group_chat_ids=[GROUP_ID],
            group_chat_owner_id=OWNER_ID,
            group_chat_policy="",
        )
        orchestrator = MessageOrchestrator(settings, deps)
        update = make_group_update(user_id=MEMBER_ID)

        kwargs = await _run_text_and_get_kwargs(orchestrator, settings, update)

        assert kwargs["append_system_prompt"] is None
