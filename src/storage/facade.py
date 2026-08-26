"""Unified storage interface.

Provides simple API for the rest of the application.
"""

from datetime import UTC, datetime
from typing import Any, Dict, Optional

import structlog

from ..claude.sdk_integration import ClaudeResponse
from .database import DatabaseManager
from .models import (
    AuditLogModel,
    MessageModel,
    SessionModel,
    ToolUsageModel,
    UserModel,
)
from .repositories import (
    AnalyticsRepository,
    AuditLogRepository,
    CostTrackingRepository,
    MessageRepository,
    ProjectThreadRepository,
    SessionRepository,
    ToolUsageRepository,
    UserRepository,
)

logger = structlog.get_logger()


class Storage:
    """Main storage interface."""

    def __init__(self, database_url: str):
        """Initialize storage with database URL."""
        self.db_manager = DatabaseManager(database_url)
        self.users = UserRepository(self.db_manager)
        self.sessions = SessionRepository(self.db_manager)
        self.project_threads = ProjectThreadRepository(self.db_manager)
        self.messages = MessageRepository(self.db_manager)
        self.tools = ToolUsageRepository(self.db_manager)
        self.audit = AuditLogRepository(self.db_manager)
        self.costs = CostTrackingRepository(self.db_manager)
        self.analytics = AnalyticsRepository(self.db_manager)

    async def initialize(self):
        """Initialize storage system."""
        logger.info("Initializing storage system")
        await self.db_manager.initialize()
        logger.info("Storage system initialized")

    async def close(self):
        """Close storage connections."""
        logger.info("Closing storage system")
        await self.db_manager.close()

    async def get_routine_notification(self, message_id: int):
        """Look up a relayed routine notification by its Telegram message_id."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT message_id, chat_id, routine, headline, output_path,
                       status_path, session_id, mailbox_id, created_at
                FROM routine_notifications WHERE message_id = ?
                """,
                (message_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def register_mailbox(
        self,
        routine: str,
        mailbox_path: str,
        topic: str,
        scope: str = "private",
        ttl_minutes: int = 240,
    ) -> int:
        """Register (or refresh) a feedback mailbox; returns its id.

        Re-registering the same routine+path replaces the old row so a
        session that re-notifies just extends its TTL instead of creating
        duplicate router candidates.
        """
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                "DELETE FROM mailboxes WHERE routine = ? AND mailbox_path = ?",
                (routine, mailbox_path),
            )
            cursor = await conn.execute(
                """
                INSERT INTO mailboxes
                    (routine, mailbox_path, topic, scope, expires_at)
                VALUES (?, ?, ?, ?, datetime('now', ?))
                """,
                (routine, mailbox_path, topic, scope, f"{int(ttl_minutes):+d} minutes"),
            )
            mailbox_id = cursor.lastrowid
            await conn.commit()
            return int(mailbox_id or 0)

    async def get_live_mailbox(self, mailbox_id: int):
        """Fetch one mailbox if it has not expired."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT id, routine, mailbox_path, topic, scope, expires_at
                FROM mailboxes
                WHERE id = ? AND expires_at > datetime('now')
                """,
                (mailbox_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_live_mailboxes(self, family_only: bool = False):
        """All unexpired mailboxes; family chats see family-scoped only."""
        query = """
            SELECT id, routine, mailbox_path, topic, scope, expires_at
            FROM mailboxes
            WHERE expires_at > datetime('now')
        """
        if family_only:
            query += " AND scope = 'family'"
        query += " ORDER BY created_at DESC"
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(query)
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_recent_routine_notifications(self, chat_id: int, limit: int = 3):
        """Most recent routine notifications relayed to a chat (last 48h)."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT message_id, chat_id, routine, headline, output_path,
                       status_path, session_id, created_at
                FROM routine_notifications
                WHERE chat_id = ?
                  AND created_at >= datetime('now', '-2 days')
                ORDER BY created_at DESC LIMIT ?
                """,
                (chat_id, limit),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def health_check(self) -> bool:
        """Check storage system health."""
        return await self.db_manager.health_check()

    # High-level operations

    async def save_claude_interaction(
        self,
        user_id: int,
        session_id: str,
        prompt: str,
        response: ClaudeResponse,
        ip_address: Optional[str] = None,
    ):
        """Save complete Claude interaction."""
        logger.info(
            "Saving Claude interaction",
            user_id=user_id,
            session_id=session_id,
            cost=response.cost,
        )

        # Ensure the sender has a users row (messages/cost tables FK to users).
        # In group chats the session is owned by the chat id, so a member's
        # first interaction may arrive before any user record exists for them.
        if not await self.users.get_user(user_id):
            await self.users.create_user(
                UserModel(
                    user_id=user_id,
                    telegram_username=None,
                    first_seen=datetime.now(UTC),
                    last_active=datetime.now(UTC),
                    is_allowed=True,
                )
            )

        # Save message
        message = MessageModel(
            message_id=None,
            session_id=session_id,
            user_id=user_id,
            timestamp=datetime.now(UTC),
            prompt=prompt,
            response=response.content,
            cost=response.cost,
            duration_ms=response.duration_ms,
            error=response.error_type if response.is_error else None,
        )

        message_id = await self.messages.save_message(message)

        # Save tool usage
        if response.tools_used:
            for tool in response.tools_used:
                tool_usage = ToolUsageModel(
                    id=None,
                    session_id=session_id,
                    message_id=message_id,
                    tool_name=tool["name"],
                    tool_input=tool.get("input", {}),
                    timestamp=datetime.now(UTC),
                    success=not response.is_error,
                    error_message=response.error_type if response.is_error else None,
                )
                await self.tools.save_tool_usage(tool_usage)

        # Update cost tracking
        await self.costs.update_daily_cost(user_id, response.cost)

        # Update user stats
        user = await self.users.get_user(user_id)
        if user:
            user.total_cost += response.cost
            user.message_count += 1
            user.last_active = datetime.now(UTC)
            await self.users.update_user(user)

        # Update session stats
        session = await self.sessions.get_session(session_id)
        if session:
            session.total_cost += response.cost
            session.total_turns += response.num_turns
            session.message_count += 1
            session.last_used = datetime.now(UTC)
            await self.sessions.update_session(session)

        # Log audit event
        audit_event = AuditLogModel(
            id=None,
            user_id=user_id,
            event_type="claude_interaction",
            event_data={
                "session_id": session_id,
                "cost": response.cost,
                "duration_ms": response.duration_ms,
                "num_turns": response.num_turns,
                "is_error": response.is_error,
                "tools_used": [t["name"] for t in response.tools_used],
            },
            success=not response.is_error,
            timestamp=datetime.now(UTC),
            ip_address=ip_address,
        )
        await self.audit.log_event(audit_event)

    async def get_or_create_user(
        self, user_id: int, username: Optional[str] = None
    ) -> UserModel:
        """Get or create user."""
        user = await self.users.get_user(user_id)

        if not user:
            logger.info("Creating new user", user_id=user_id, username=username)
            user = UserModel(
                user_id=user_id,
                telegram_username=username,
                first_seen=datetime.now(UTC),
                last_active=datetime.now(UTC),
                is_allowed=False,  # Default to not allowed
            )
            await self.users.create_user(user)

        return user

    async def create_session(
        self, user_id: int, project_path: str, session_id: str
    ) -> SessionModel:
        """Create new session."""
        session = SessionModel(
            session_id=session_id,
            user_id=user_id,
            project_path=project_path,
            created_at=datetime.now(UTC),
            last_used=datetime.now(UTC),
        )

        await self.sessions.create_session(session)

        # Update user session count
        user = await self.users.get_user(user_id)
        if user:
            user.session_count += 1
            await self.users.update_user(user)

        return session

    async def log_security_event(
        self,
        user_id: int,
        event_type: str,
        event_data: Dict[str, Any],
        success: bool = True,
        ip_address: Optional[str] = None,
    ):
        """Log security-related event."""
        audit_event = AuditLogModel(
            id=None,
            user_id=user_id,
            event_type=event_type,
            event_data=event_data,
            success=success,
            timestamp=datetime.now(UTC),
            ip_address=ip_address,
        )
        await self.audit.log_event(audit_event)

    async def log_bot_event(
        self,
        user_id: int,
        event_type: str,
        event_data: Dict[str, Any],
        success: bool = True,
    ):
        """Log bot-related event."""
        audit_event = AuditLogModel(
            id=None,
            user_id=user_id,
            event_type=event_type,
            event_data=event_data,
            success=success,
            timestamp=datetime.now(UTC),
        )
        await self.audit.log_event(audit_event)

    # Convenience methods

    async def is_user_allowed(self, user_id: int) -> bool:
        """Check if user is allowed."""
        user = await self.users.get_user(user_id)
        return user.is_allowed if user else False

    async def get_user_session_summary(self, user_id: int) -> Dict[str, Any]:
        """Get user session summary."""
        sessions = await self.sessions.get_user_sessions(user_id, active_only=False)
        active_sessions = [s for s in sessions if s.is_active]

        return {
            "total_sessions": len(sessions),
            "active_sessions": len(active_sessions),
            "total_cost": sum(s.total_cost for s in sessions),
            "total_messages": sum(s.message_count for s in sessions),
            "projects": list(set(s.project_path for s in sessions)),
        }

    async def get_session_history(
        self, session_id: str, limit: int = 50
    ) -> Dict[str, Any]:
        """Get session history with messages and tools."""
        session = await self.sessions.get_session(session_id)
        if not session:
            return None

        messages = await self.messages.get_session_messages(session_id, limit)
        tools = await self.tools.get_session_tool_usage(session_id)

        return {
            "session": session.to_dict(),
            "messages": [m.to_dict() for m in messages],
            "tool_usage": [t.to_dict() for t in tools],
        }

    async def cleanup_old_data(self, days: int = 30) -> Dict[str, int]:
        """Cleanup old data."""
        logger.info("Starting data cleanup", days=days)

        # Cleanup old sessions
        sessions_cleaned = await self.sessions.cleanup_old_sessions(days)

        logger.info("Data cleanup complete", sessions_cleaned=sessions_cleaned)

        return {"sessions_cleaned": sessions_cleaned}

    async def get_user_dashboard(self, user_id: int) -> Dict[str, Any]:
        """Get comprehensive user dashboard data."""
        # Get user info
        user = await self.users.get_user(user_id)
        if not user:
            return None

        # Get user stats
        stats = await self.analytics.get_user_stats(user_id)

        # Get recent sessions
        sessions = await self.sessions.get_user_sessions(user_id, active_only=True)

        # Get recent messages
        messages = await self.messages.get_user_messages(user_id, limit=10)

        # Get recent audit log
        audit_logs = await self.audit.get_user_audit_log(user_id, limit=20)

        # Get daily costs
        daily_costs = await self.costs.get_user_daily_costs(user_id, days=30)

        return {
            "user": user.to_dict(),
            "stats": stats,
            "recent_sessions": [s.to_dict() for s in sessions[:5]],
            "recent_messages": [m.to_dict() for m in messages],
            "recent_audit": [a.to_dict() for a in audit_logs],
            "daily_costs": [c.to_dict() for c in daily_costs],
        }

    async def get_admin_dashboard(self) -> Dict[str, Any]:
        """Get admin dashboard data."""
        # Get system stats
        system_stats = await self.analytics.get_system_stats()

        # Get all users
        users = await self.users.get_all_users()

        # Get recent audit log
        recent_audit = await self.audit.get_recent_audit_log(hours=24)

        # Get total costs
        total_costs = await self.costs.get_total_costs(days=30)

        # Get tool stats
        tool_stats = await self.tools.get_tool_stats()

        return {
            "system_stats": system_stats,
            "users": [u.to_dict() for u in users],
            "recent_audit": [a.to_dict() for a in recent_audit],
            "total_costs": total_costs,
            "tool_stats": tool_stats,
        }
