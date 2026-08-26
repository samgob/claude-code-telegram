"""FastAPI webhook server.

Runs in the same process as the bot, sharing the event loop.
Receives external webhooks and publishes them as events on the bus.
"""

import uuid
from typing import Any, Dict, Optional

import structlog
from fastapi import FastAPI, Header, HTTPException, Request

from ..config.settings import Settings
from ..events.bus import EventBus
from ..events.types import WebhookEvent
from ..storage.database import DatabaseManager
from .auth import verify_github_signature, verify_shared_secret

logger = structlog.get_logger()


def create_api_app(
    event_bus: EventBus,
    settings: Settings,
    db_manager: Optional[DatabaseManager] = None,
    bot: Optional[Any] = None,
) -> FastAPI:
    """Create the FastAPI application."""

    app = FastAPI(
        title="Claude Code Telegram - Webhook API",
        version="0.1.0",
        docs_url="/docs" if settings.development_mode else None,
        redoc_url=None,
    )

    @app.get("/health")
    async def health_check() -> Dict[str, str]:
        return {"status": "ok"}

    # NOTE: must be registered BEFORE the catch-all /webhooks/{provider}
    # route below, or provider="routine" swallows it.
    @app.post("/webhooks/routine")
    async def receive_routine_notification(
        request: Request,
        authorization: Optional[str] = Header(None),
    ) -> Dict[str, Any]:
        """Relay a scheduled-routine completion to the owner's Telegram chat.

        Deliberately does NOT publish to the event bus — no Claude run fires
        on notification. The bot just forwards the headline and records
        message_id -> payload so a later user reply (Telegram reply-to) can
        be routed back to this routine's output files with full context.
        """
        secret = settings.webhook_api_secret
        if not secret:
            raise HTTPException(
                status_code=500,
                detail="Webhook API secret not configured.",
            )
        if not verify_shared_secret(authorization, secret):
            raise HTTPException(status_code=401, detail="Invalid authorization")
        if bot is None:
            raise HTTPException(status_code=503, detail="Bot not wired into API server")

        try:
            payload: Dict[str, Any] = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Body must be JSON")

        routine = str(payload.get("routine", "")).strip()
        headline = str(payload.get("headline", "")).strip()
        if not routine or not headline:
            raise HTTPException(
                status_code=400, detail="routine and headline are required"
            )

        # Chat targeting: "dm" (default) = owner notification chat;
        # "family" = the shared family group chat.
        chat_target = str(payload.get("chat", "dm")).strip() or "dm"
        if chat_target == "family":
            group_ids = settings.group_chat_ids or []
            if not group_ids:
                raise HTTPException(
                    status_code=400,
                    detail="chat=family requires GROUP_CHAT_IDS to be set",
                )
            chat_id = group_ids[0]
        elif chat_target == "dm":
            chat_ids = settings.notification_chat_ids or []
            if not chat_ids:
                raise HTTPException(
                    status_code=500, detail="NOTIFICATION_CHAT_IDS not configured"
                )
            chat_id = chat_ids[0]
        else:
            raise HTTPException(status_code=400, detail="chat must be 'dm' or 'family'")

        # Optional mailbox registration: the sending session declares a
        # feedback file + topic so replies (and confidently classified
        # bare messages) can be appended back to it while it waits.
        mailbox_id: Optional[int] = None
        mailbox_path = str(payload.get("mailbox_path") or "").strip()
        if mailbox_path and db_manager is not None:
            scope = str(payload.get("scope") or "").strip() or (
                "family" if chat_target == "family" else "private"
            )
            if scope not in ("private", "family"):
                raise HTTPException(
                    status_code=400, detail="scope must be 'private' or 'family'"
                )
            try:
                ttl_minutes = int(payload.get("ttl_minutes") or 240)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400, detail="ttl_minutes must be an integer"
                )
            mailbox_id = await _register_mailbox(
                db_manager,
                routine=routine,
                mailbox_path=mailbox_path,
                topic=str(payload.get("topic") or headline).strip(),
                scope=scope,
                ttl_minutes=ttl_minutes,
            )

        text = f"\U0001f916 {routine} — {headline}"
        message = await bot.send_message(chat_id=chat_id, text=text)

        if db_manager is not None:
            await _record_routine_notification(
                db_manager,
                message_id=message.message_id,
                chat_id=chat_id,
                routine=routine,
                headline=headline,
                output_path=payload.get("output_path"),
                status_path=payload.get("status_path"),
                session_id=payload.get("session_id"),
                mailbox_id=mailbox_id,
            )

        logger.info(
            "Routine notification relayed",
            routine=routine,
            message_id=message.message_id,
        )
        return {"status": "sent", "message_id": message.message_id}

    @app.post("/webhooks/{provider}")
    async def receive_webhook(
        provider: str,
        request: Request,
        x_hub_signature_256: Optional[str] = Header(None),
        x_github_event: Optional[str] = Header(None),
        x_github_delivery: Optional[str] = Header(None),
        authorization: Optional[str] = Header(None),
    ) -> Dict[str, str]:
        """Receive and validate webhook from an external provider."""
        body = await request.body()

        # Verify signature based on provider
        if provider == "github":
            secret = settings.github_webhook_secret
            if not secret:
                raise HTTPException(
                    status_code=500,
                    detail="GitHub webhook secret not configured",
                )
            if not verify_github_signature(body, x_hub_signature_256, secret):
                logger.warning(
                    "GitHub webhook signature verification failed",
                    delivery_id=x_github_delivery,
                )
                raise HTTPException(status_code=401, detail="Invalid signature")

            event_type_name = x_github_event or "unknown"
            delivery_id = x_github_delivery or str(uuid.uuid4())
        else:
            # Generic provider — require auth (fail-closed)
            secret = settings.webhook_api_secret
            if not secret:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "Webhook API secret not configured. "
                        "Set WEBHOOK_API_SECRET to accept "
                        "webhooks from this provider."
                    ),
                )
            if not verify_shared_secret(authorization, secret):
                raise HTTPException(status_code=401, detail="Invalid authorization")
            event_type_name = request.headers.get("X-Event-Type", "unknown")
            delivery_id = request.headers.get("X-Delivery-ID", str(uuid.uuid4()))

        # Parse JSON payload
        try:
            payload: Dict[str, Any] = await request.json()
        except Exception:
            payload = {"raw_body": body.decode("utf-8", errors="replace")[:5000]}

        # Atomic dedupe: attempt INSERT first, only publish if new
        if db_manager and delivery_id:
            is_new = await _try_record_webhook(
                db_manager,
                event_id=str(uuid.uuid4()),
                provider=provider,
                event_type=event_type_name,
                delivery_id=delivery_id,
                payload=payload,
            )
            if not is_new:
                logger.info(
                    "Duplicate webhook delivery ignored",
                    provider=provider,
                    delivery_id=delivery_id,
                )
                return {
                    "status": "duplicate",
                    "delivery_id": delivery_id,
                }

        # Publish event to the bus
        event = WebhookEvent(
            provider=provider,
            event_type_name=event_type_name,
            payload=payload,
            delivery_id=delivery_id,
        )

        await event_bus.publish(event)

        logger.info(
            "Webhook received and published",
            provider=provider,
            event_type=event_type_name,
            delivery_id=delivery_id,
            event_id=event.id,
        )

        return {"status": "accepted", "event_id": event.id}

    return app


async def _try_record_webhook(
    db_manager: DatabaseManager,
    event_id: str,
    provider: str,
    event_type: str,
    delivery_id: str,
    payload: Dict[str, Any],
) -> bool:
    """Atomically insert a webhook event, returning whether it was new.

    Uses INSERT OR IGNORE on the unique delivery_id column.
    If the row already exists the insert is a no-op and changes() == 0.
    Returns True if the event is new (inserted), False if duplicate.
    """
    import json

    async with db_manager.get_connection() as conn:
        await conn.execute(
            """
            INSERT OR IGNORE INTO webhook_events
            (event_id, provider, event_type, delivery_id, payload,
             processed)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (
                event_id,
                provider,
                event_type,
                delivery_id,
                json.dumps(payload),
            ),
        )
        cursor = await conn.execute("SELECT changes()")
        row = await cursor.fetchone()
        inserted = row[0] > 0 if row else False
        await conn.commit()
        return inserted


async def _record_routine_notification(
    db_manager: DatabaseManager,
    message_id: int,
    chat_id: int,
    routine: str,
    headline: str,
    output_path: Optional[str],
    status_path: Optional[str],
    session_id: Optional[str],
    mailbox_id: Optional[int] = None,
) -> None:
    """Persist the Telegram message_id -> routine payload mapping."""
    async with db_manager.get_connection() as conn:
        await conn.execute(
            """
            INSERT OR REPLACE INTO routine_notifications
            (message_id, chat_id, routine, headline, output_path,
             status_path, session_id, mailbox_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                chat_id,
                routine,
                headline,
                output_path,
                status_path,
                session_id,
                mailbox_id,
            ),
        )
        await conn.commit()


async def _register_mailbox(
    db_manager: DatabaseManager,
    routine: str,
    mailbox_path: str,
    topic: str,
    scope: str,
    ttl_minutes: int,
) -> int:
    """Register (or refresh) a feedback mailbox; returns its id.

    Re-registering the same routine+path replaces the old row so a session
    that re-notifies extends its TTL instead of duplicating router
    candidates.
    """
    async with db_manager.get_connection() as conn:
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


async def run_api_server(
    event_bus: EventBus,
    settings: Settings,
    db_manager: Optional[DatabaseManager] = None,
    bot: Optional[Any] = None,
) -> None:
    """Run the FastAPI server using uvicorn."""
    import uvicorn

    app = create_api_app(event_bus, settings, db_manager, bot=bot)

    config = uvicorn.Config(
        app=app,
        host=settings.api_server_host,
        port=settings.api_server_port,
        log_level="info" if not settings.debug else "debug",
    )
    server = uvicorn.Server(config)
    await server.serve()
