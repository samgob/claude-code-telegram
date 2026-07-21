"""Main Telegram bot class.

Features:
- Command registration
- Handler management
- Context injection
- Graceful shutdown
"""

import asyncio
import os
import time
from typing import Any, Callable, Dict, Optional

import structlog
from telegram import Update
from telegram.error import Conflict
from telegram.ext import (
    AIORateLimiter,
    Application,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

from ..config.settings import Settings
from ..exceptions import ClaudeCodeTelegramError
from .features.registry import FeatureRegistry
from .orchestrator import MessageOrchestrator

logger = structlog.get_logger()

# Where the polling loop stamps a liveness heartbeat. An external watchdog reads
# this file's age to detect a fully hung event loop (a failure the in-process
# Conflict guard below cannot see, because that guard relies on the loop running).
HEARTBEAT_FILE = os.environ.get(
    "TELEGRAM_BOT_HEARTBEAT_FILE", "/tmp/telegram-bot-heartbeat"
)

# A getUpdates Conflict means a second bot instance grabbed our polling slot.
# In python-telegram-bot the updater retries this forever WITHOUT exiting and
# WITHOUT flipping `updater.running` to False, so the process becomes a zombie
# (alive but never delivering messages). We count consecutive Conflicts and, past
# this threshold within the time window, exit so launchd's KeepAlive restarts us.
POLL_CONFLICT_EXIT_THRESHOLD = 3
POLL_CONFLICT_WINDOW_SECONDS = 120


class ClaudeCodeBot:
    """Main bot orchestrator."""

    def __init__(self, settings: Settings, dependencies: Dict[str, Any]):
        """Initialize bot with settings and dependencies."""
        self.settings = settings
        self.deps = dependencies
        self.app: Optional[Application] = None
        self.is_running = False
        self.feature_registry: Optional[FeatureRegistry] = None
        self.orchestrator = MessageOrchestrator(settings, dependencies)
        # Zombie-poller guard state (see POLL_CONFLICT_* constants).
        self._poll_conflict_count = 0
        self._last_poll_conflict = 0.0

    async def initialize(self) -> None:
        """Initialize bot application. Idempotent — safe to call multiple times."""
        if self.app is not None:
            return

        logger.info("Initializing Telegram bot")

        # Create application
        builder = Application.builder()
        builder.token(self.settings.telegram_token_str)
        builder.defaults(Defaults(do_quote=self.settings.reply_quote))
        builder.rate_limiter(AIORateLimiter(max_retries=1))

        from .update_processor import StopAwareUpdateProcessor

        builder.concurrent_updates(StopAwareUpdateProcessor())

        # Configure connection settings
        builder.connect_timeout(30)
        builder.read_timeout(30)
        builder.write_timeout(30)
        builder.pool_timeout(30)

        # Explicitly set proxy from environment variables.
        # This is necessary because python-telegram-bot's Application.builder()
        # does not automatically use HTTP_PROXY/HTTPS_PROXY environment variables.
        # Without this, the httpx connection pool can become corrupted when running
        # behind a proxy, causing the bot to stop responding to messages.
        import os

        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        if proxy_url:
            builder.proxy(proxy_url)
            logger.info("Proxy configured", proxy=proxy_url)

        self.app = builder.build()

        # Initialize feature registry
        self.feature_registry = FeatureRegistry(
            config=self.settings,
            storage=self.deps.get("storage"),
            security=self.deps.get("security"),
        )

        # Add feature registry to dependencies
        self.deps["features"] = self.feature_registry

        # Initialize the underlying Telegram Application so the bot's
        # HTTP client is ready before we make API calls.
        await self.app.initialize()

        # Set bot commands for menu (requires initialized HTTP client)
        await self._set_bot_commands()

        # Register handlers
        self._register_handlers()

        # Add middleware
        self._add_middleware()

        # Set error handler
        self.app.add_error_handler(self._error_handler)

        logger.info("Bot initialization complete")

    async def _set_bot_commands(self) -> None:
        """Set bot command menu via orchestrator."""
        commands = await self.orchestrator.get_bot_commands()
        await self.app.bot.set_my_commands(commands)
        logger.info("Bot commands set", commands=[cmd.command for cmd in commands])

    def _register_handlers(self) -> None:
        """Register handlers via orchestrator (mode-aware)."""
        self.orchestrator.register_handlers(self.app)

    def _add_middleware(self) -> None:
        """Add middleware to application."""
        from .middleware.auth import auth_middleware
        from .middleware.rate_limit import rate_limit_middleware
        from .middleware.security import security_middleware

        # Middleware runs in order of group numbers (lower = earlier)
        # Security middleware first (validate inputs)
        self.app.add_handler(
            MessageHandler(
                filters.ALL, self._create_middleware_handler(security_middleware)
            ),
            group=-3,
        )

        # Authentication second
        self.app.add_handler(
            MessageHandler(
                filters.ALL, self._create_middleware_handler(auth_middleware)
            ),
            group=-2,
        )

        # Rate limiting third
        self.app.add_handler(
            MessageHandler(
                filters.ALL, self._create_middleware_handler(rate_limit_middleware)
            ),
            group=-1,
        )

        logger.info("Middleware added to bot")

    def _create_middleware_handler(self, middleware_func: Callable) -> Callable:
        """Create middleware handler that injects dependencies.

        When middleware rejects a request (returns without calling the handler),
        ApplicationHandlerStop is raised to prevent subsequent handler groups
        from processing the update.
        """
        from telegram.ext import ApplicationHandlerStop

        async def middleware_wrapper(
            update: Update, context: ContextTypes.DEFAULT_TYPE
        ) -> None:
            # Ignore updates generated by bots (including this bot) to avoid
            # self-authentication loops and duplicate processing.
            if update.effective_user and getattr(
                update.effective_user, "is_bot", False
            ):
                logger.debug(
                    "Skipping bot-originated update in middleware",
                    user_id=update.effective_user.id,
                    middleware=middleware_func.__name__,
                )
                raise ApplicationHandlerStop

            # Silently ignore group-chat updates from groups that are not on
            # the allowlist. This runs before any middleware that could reply,
            # so the bot never responds in a group it wasn't invited to serve.
            chat = update.effective_chat
            if chat is not None and getattr(chat, "type", None) in (
                "group",
                "supergroup",
            ):
                allowed_groups = self.settings.group_chat_ids or []
                if chat.id not in allowed_groups:
                    logger.debug(
                        "Ignoring update from non-allowlisted group chat",
                        chat_id=chat.id,
                        middleware=middleware_func.__name__,
                    )
                    raise ApplicationHandlerStop

            # Inject dependencies into context
            for key, value in self.deps.items():
                context.bot_data[key] = value
            context.bot_data["settings"] = self.settings

            # Track whether the middleware allowed the request through
            handler_called = False

            async def dummy_handler(event: Any, data: Any) -> None:
                nonlocal handler_called
                handler_called = True

            # Call middleware with Telegram-style parameters
            await middleware_func(dummy_handler, update, context.bot_data)

            # If middleware didn't call the handler, it rejected the request.
            # Raise ApplicationHandlerStop to prevent subsequent handler groups
            # (including the main message handlers) from processing this update.
            if not handler_called:
                raise ApplicationHandlerStop()

        return middleware_wrapper

    async def start(self) -> None:
        """Start the bot."""
        if self.is_running:
            logger.warning("Bot is already running")
            return

        await self.initialize()

        logger.info(
            "Starting bot", mode="webhook" if self.settings.webhook_url else "polling"
        )

        try:
            self.is_running = True

            if self.settings.webhook_url:
                # Webhook mode
                await self.app.run_webhook(
                    listen="0.0.0.0",
                    port=self.settings.webhook_port,
                    url_path=self.settings.webhook_path,
                    webhook_url=self.settings.webhook_url,
                    drop_pending_updates=True,
                    allowed_updates=Update.ALL_TYPES,
                )
            else:
                # Polling mode - initialize and start polling manually
                await self.app.initialize()
                await self.app.start()
                await self.app.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True,
                    error_callback=self._on_polling_error,
                )

                # Keep running until manually stopped. Stamp a heartbeat each cycle
                # so an external watchdog can detect a fully hung event loop; the
                # _on_polling_error guard handles the poller-died-but-loop-alive case.
                while self.is_running:
                    self._write_heartbeat()
                    await asyncio.sleep(1)
        except Exception as e:
            logger.error("Error running bot", error=str(e))
            raise ClaudeCodeTelegramError(f"Failed to start bot: {str(e)}") from e
        finally:
            self.is_running = False

    async def stop(self) -> None:
        """Gracefully stop the bot."""
        if not self.is_running:
            logger.warning("Bot is not running")
            return

        logger.info("Stopping bot")

        try:
            self.is_running = False  # Stop the main loop first

            # Shutdown feature registry
            if self.feature_registry:
                self.feature_registry.shutdown()

            if self.app:
                # Stop the updater if it's running
                if self.app.updater.running:
                    await self.app.updater.stop()

                # Stop the application
                await self.app.stop()
                await self.app.shutdown()

            logger.info("Bot stopped successfully")
        except Exception as e:
            logger.error("Error stopping bot", error=str(e))
            raise ClaudeCodeTelegramError(f"Failed to stop bot: {str(e)}") from e

    def _write_heartbeat(self) -> None:
        """Stamp the heartbeat file with the current time.

        Best-effort: a heartbeat write must never take the bot down, so any
        filesystem error is logged at debug level and swallowed.
        """
        try:
            with open(HEARTBEAT_FILE, "w") as fh:
                fh.write(str(int(time.time())))
        except OSError as exc:  # pragma: no cover - defensive
            logger.debug("Heartbeat write failed", error=str(exc))

    def _on_polling_error(self, exc: Exception) -> None:
        """Updater error callback for get_updates failures.

        Fires on the actual failure signal (a Conflict from a competing
        getUpdates consumer) rather than on a proxy like ``updater.running``,
        which does NOT change in this failure mode. After repeated Conflicts
        within the window we stop the run loop so the process exits cleanly and
        launchd's KeepAlive restarts a fresh, healthy poller.
        """
        if not isinstance(exc, Conflict):
            # Any non-Conflict error means we are not in the zombie scenario;
            # let PTB's own retry logic handle it and reset the counter.
            self._poll_conflict_count = 0
            return

        now = time.monotonic()
        if now - self._last_poll_conflict > POLL_CONFLICT_WINDOW_SECONDS:
            # Too long since the last Conflict — treat this as a fresh streak.
            self._poll_conflict_count = 0
        self._last_poll_conflict = now
        self._poll_conflict_count += 1

        logger.error(
            "getUpdates Conflict — another bot instance is polling this token",
            consecutive_conflicts=self._poll_conflict_count,
            threshold=POLL_CONFLICT_EXIT_THRESHOLD,
        )

        if self._poll_conflict_count >= POLL_CONFLICT_EXIT_THRESHOLD:
            logger.critical(
                "Persistent polling Conflict — exiting for launchd restart",
                consecutive_conflicts=self._poll_conflict_count,
            )
            # Break the run loop in start(); run_application()'s finally block
            # then runs bot.stop() + storage.close() for a clean shutdown, and
            # the process exit triggers a KeepAlive restart.
            self.is_running = False

    async def _error_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle errors globally."""
        error = context.error
        logger.error(
            "Global error handler triggered",
            error=str(error),
            update_type=type(update).__name__ if update else None,
            user_id=(
                update.effective_user.id if update and update.effective_user else None
            ),
        )

        # Determine error message for user
        from ..exceptions import (
            AuthenticationError,
            ConfigurationError,
            RateLimitExceeded,
            SecurityError,
        )

        error_messages = {
            AuthenticationError: "🔒 Authentication required. Please contact the administrator.",
            SecurityError: "🛡️ Security violation detected. This incident has been logged.",
            RateLimitExceeded: "⏱️ Rate limit exceeded. Please wait before sending more messages.",
            ConfigurationError: "⚙️ Configuration error. Please contact the administrator.",
            asyncio.TimeoutError: "⏰ Operation timed out. Please try again with a simpler request.",
        }

        error_type = type(error)
        user_message = error_messages.get(
            error_type, "❌ An unexpected error occurred. Please try again."
        )

        # Try to notify user
        if update and update.effective_message:
            try:
                await update.effective_message.reply_text(user_message)
            except Exception:
                logger.exception("Failed to send error message to user")

        # Log to audit system if available
        from ..security.audit import AuditLogger

        audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
        if audit_logger and update and update.effective_user:
            try:
                await audit_logger.log_security_violation(
                    user_id=update.effective_user.id,
                    violation_type="system_error",
                    details=f"Error type: {error_type.__name__}, Message: {str(error)}",
                    severity="medium",
                )
            except Exception:
                logger.exception("Failed to log error to audit system")

    async def get_bot_info(self) -> Dict[str, Any]:
        """Get bot information."""
        if not self.app:
            return {"status": "not_initialized"}

        try:
            me = await self.app.bot.get_me()
            return {
                "status": "running" if self.is_running else "initialized",
                "username": me.username,
                "first_name": me.first_name,
                "id": me.id,
                "can_join_groups": me.can_join_groups,
                "can_read_all_group_messages": me.can_read_all_group_messages,
                "supports_inline_queries": me.supports_inline_queries,
                "webhook_url": self.settings.webhook_url,
                "webhook_port": (
                    self.settings.webhook_port if self.settings.webhook_url else None
                ),
            }
        except Exception as e:
            logger.error("Failed to get bot info", error=str(e))
            return {"status": "error", "error": str(e)}

    async def health_check(self) -> bool:
        """Perform health check."""
        try:
            if not self.app:
                return False

            # Try to get bot info
            await self.app.bot.get_me()
            return True
        except Exception as e:
            logger.error("Health check failed", error=str(e))
            return False
