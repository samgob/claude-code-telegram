# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Feedback mailboxes + contextual routing (phase 2 of the no-watchers architecture, 2026-08-25).** Closes the return path: a Desktop session that wants feedback (e.g. a grocery-order draft awaiting V's review) can now *hear* replies, from the owner's DM or the family group. `POST /webhooks/routine` gains `chat` (`dm` default | `family` → first `GROUP_CHAT_IDS` entry) plus optional mailbox registration: `mailbox_path` (a JSONL file the waiting session watches), `topic` (router-facing description), `scope` (`private` = DM-routable only | `family`; defaults by chat target), `ttl_minutes` (default 240). Mailboxes live in a new `mailboxes` table (migration 6; `routine_notifications` gains `mailbox_id`); re-registration replaces, so re-notifying extends TTL instead of duplicating router candidates. Two capture paths, both appending attributed JSONL (`ts`/`sender`/`text`/`via`) in **Python daemon code — deliberately outside the per-turn tool gate**, so V's turns (which have Write/Bash stripped) still land feedback: (1) *strong binding* — a swipe-reply to a mailbox-linked notification is appended and acked (`📨 → <routine>`) with **no model run** (the waiting session owns the response); (2) *contextual routing* — a bare message is classified against the chat's live mailboxes (family chat sees `scope='family'` only; DM sees all) by a one-shot tool-less SDK call, and only a `high`-confidence verdict routes — the message then **tees**: appended + acked + still answered conversationally with a directive not to duplicate the routed work. Fail-open everywhere: expired mailbox, append failure, classifier timeout (20s), or parse error → normal chat handling. Mailbox paths are confined to `APPROVED_DIRECTORY`. `notify-routine.sh` gains `--chat/--mailbox/--topic/--scope/--ttl-min` (fallback rail unchanged — legacy `notify.sh` always lands in Sam's chat, even for `--chat family`). Router model pinned `claude-sonnet-5`: logged Sonnet carve-out — high-frequency per-message classification, low-stakes by the fail-open + visible-ack design, verifiable via the ack and the mailbox line. New unit tests (`tests/unit/test_bot/test_mailbox_router.py`): append/attribution/traversal-refusal, scope filtering, TTL expiry, replace-on-reregister. *Fix (2026-08-26, first live router test):* the classifier originally used the SDK's high-level `query()` helper, which raises `MessageParseError` on any message type its parser doesn't know — a `rate_limit_event` from the CLI killed the whole classification (fail-open caught it; the message just didn't route). Rewritten to the same tolerant pattern as `ClaudeSDKManager`: `ClaudeSDKClient` + raw `receive_messages()` + per-message `parse_message` with unparseable messages skipped. Verified in-process with the daemon's auth env: grocery-ish → routes, workout-ish → routes to the other mailbox, unrelated → null.
- **Routine notification relay + reply routing (phase 1 of the no-watchers architecture, 2026-07-26).** Scheduled routines used to notify via a dumb one-way bot and optionally *wait* for a reply (`ask.sh` long-poll with a timeout) — replies after the window, or to any but the most recent notification, went nowhere. Now: `POST /webhooks/routine` (Bearer-auth, registered before the `/webhooks/{provider}` catch-all, deliberately does NOT publish to the event bus so no Claude run fires on notification) sends the headline to the owner's chat via the bot and records `message_id → {routine, output_path, status_path, session_id}` in a new `routine_notifications` table (migration 5). When the user replies: a Telegram reply-to on a relayed notification injects a directive block pointing Claude at that routine's output/status files (`MessageOrchestrator._routine_reply_context`); a bare message gets a light listing of the last 3 notifications (48h window) so references like "the digest you sent" resolve. No timers, no windows — a reply days later still routes. Routines call `~/.claude/scripts/notify-routine.sh` (falls back to legacy `notify.sh` if the bot is down). Pilot conversions: daily-news-digest + proactive-comms (which also lost its `ask.sh` usage). API server now binds `API_SERVER_HOST` (default `127.0.0.1`, was hardcoded `0.0.0.0`) and receives the `Bot` instance. Tool-asymmetry bridge: actions needing work Gmail/Attio/Slack/Fireflies are queued to `.memory/pending-actions/queue.jsonl` (system-prompt rule + proactive-comms Step 0.6 executes them ≤15 min later); Apple Calendar — including the work calendar, which EventKit sees — is handled directly via mcp__apple-events.
- **MCP tool access for bot sessions** (NAD-calendar incident, 2026-07-24/25): bot sessions previously ran with `ENABLE_MCP=false` (upstream default, never revisited) — no calendar/email tools at all. Subagents asked to fix a calendar improvised access (OAuth credential-store raids, GUI Keychain prompts, `launchctl` kickstarts) and never succeeded. Now enabled via `mcp-config.json`: `personal-gmail` (HTTP singleton on :8001) + `apple-events` (`mcp-server-apple-events@1.4.0`, the same npm package Claude Desktop runs, installed locally under `Projects/Claude Routines/apple-events-mcp/` — pinned install because `npx -y` stalls minutes on registry checks). `CLAUDE_ALLOWED_TOOLS` gained server-level entries `mcp__personal-gmail,mcp__apple-events`; `GROUP_CHAT_RESTRICTED_TOOLS` now set explicitly in `.env` and extended with both servers so non-owner (V) turns cannot touch mail/calendar. NOTE: macOS grants node "add events only" calendar permission by default — reads fail with a clear error until Full Calendar Access is granted to node in System Settings > Privacy & Security > Calendars.
- **Standing prompt rules: tool boundaries + synchronous side-effect subagents** (`src/claude/sdk_integration.py`): bot sessions are now told (1) Apple Calendar / personal Google are reached ONLY via their MCP tools — if a needed tool is missing, say so and stop, never improvise via credential stores/Keychain/launchctl; (2) the Apple recurring-event truncate-don't-except gotcha + verify-after-write; (3) side-effect subagents must run with `run_in_background: false` and a turn must never end with a background task in flight — in the 7/24 incident three background `Task` subagents were killed mid-flight when their parent run completed, their queued notifications dropped, and on resume the dangling tool_use blocks were stripped from history, so the model couldn't even see that it had tried.
- **Two-layer poller self-healing** (zombie-poller incident, 2026-06-24): the bot now auto-recovers from a silent-down failure mode that previously needed a manual restart.
  - *Root cause:* a second `getUpdates` consumer briefly stole the bot's polling slot, raising `telegram.error.Conflict`. In python-telegram-bot 22.6 the updater retries this forever **without** exiting and **without** flipping `updater.running` to `False`, so the polling task is effectively dead while the process stays alive — a zombie that delivers no messages. launchd's `KeepAlive` never fired (the process never exited). Bot was silently unresponsive ~2.5h.
  - *Layer 1 — in-process Conflict guard* (`src/bot/core.py`): `start_polling()` now passes an `error_callback` (`_on_polling_error`) that counts consecutive `Conflict` errors and, after 3 within a 120s window, stops the run loop so the process exits cleanly (DB closes via `run_application()`'s `finally`) and launchd `KeepAlive` restarts a fresh poller. Recovery ~10–15s. Fires on the real failure signal (the Conflict) rather than the `updater.running` proxy, which does not change in this mode. Verified with a live second-instance Conflict.
  - *Layer 2 — external heartbeat watchdog* (`Projects/Claude Routines/telegram-bot-watchdog.sh` + `com.sam.claude.telegram-bot-watchdog.plist`, `StartInterval=60`): the run loop stamps `/tmp/telegram-bot-heartbeat` each cycle; the watchdog `launchctl kickstart -k`s the bot if the heartbeat goes stale past 180s. Catches a *fully hung event loop* (the case Layer 1 can't see, since it needs the loop running). Deliberately does **not** probe Telegram's `getUpdates` — that would steal the polling slot and cause the very Conflict this system prevents — so it needs no token. Has a job-loaded check (respects a deliberate stop) and a 180s post-kickstart cooldown. Verified by SIGSTOP-freezing the bot and confirming recovery.
- **`/sessions` and `/use` commands**: List recent Claude sessions across all sources (bot, CLI, IDE, scheduled tasks) and resume any of them. Sessions are discovered by scanning `~/.claude/projects/` JSONL transcripts and filtered to `APPROVED_DIRECTORY` for safety. `/use` switches both the active session id and the working directory atomically, so the next message resumes via Claude SDK `--resume`. Closes the gap where the bot's auto-resume was scoped to one session per project directory; you can now jump into any past session, including those started by scheduled tasks or external `claude` invocations.
- **`/use` accepts natural-language queries** in addition to id prefixes. `/use wesco` runs keyword search across recent transcripts, tokenized with stopword stripping (`the`, `session`, `latest`, `recent`, etc.). Search ranks by recency (newest first) with a relevance floor: when any match has 2+ token hits, 1-hit incidental mentions are dropped — prevents a routine that name-dropped the keyword from outranking a session focused on it. Single match → resumes immediately. Multiple → short disambiguation list. The scan reads the whole transcript (message content + tool inputs/outputs + file paths) so a session that never literally mentions "wesco" but edits `Projects/Wesco POC/schema.json` still matches.
- **Routine (scheduled-task) sessions are excluded by default** from `/sessions` and `/use` search results. They're rarely useful resume targets — they're snapshots of completed automation runs whose outputs live in canonical files. Pass `--all` (or `--routines` / `-a`) to include them: `/sessions --all`, `/use --all latest wesco`. Exact id-prefix lookup always includes routines (if you typed the id, you meant that session). The disambiguation list and `/sessions --all` listing tag routines with `⚙` so they're visually distinct.

## [1.6.0] - 2026-03-30

### Added
- **Image/screenshot analysis**: Images sent to the bot are now passed as multimodal content blocks via the SDK, enabling Claude to actually see and analyze them (#168, closes #137)
- **Exponential backoff retry**: Transient `CLIConnectionError` failures are automatically retried with exponential backoff (1s → 3s → 9s, capped at 30s). MCP config errors and timeouts are correctly excluded (#170, closes #60)
- **Local whisper.cpp voice transcription**: New `VOICE_PROVIDER=local` option for offline voice transcription via whisper.cpp + ffmpeg. No API keys required (#158)
- **`make run-watch`**: Auto-restart during development via watchfiles (#158)
- **Inline Stop button**: Cancel running Claude requests with a ⏹ button in the progress message (#122)
- **Slash command passthrough**: Unknown `/commands` in agentic mode are forwarded to Claude as prompts (#131)
- **Proxy support**: Explicit proxy configuration for httpx client via `HTTPS_PROXY`/`HTTP_PROXY` env vars (#166)

### Fixed
- **Empty responses**: "(No content to display)" after tool-heavy tasks — added missing `StreamUpdate` helper methods, fixed `ConversationEnhancer` call signature, and added fallback for tool-only responses (#136, closes #135)
- **ThinkingBlock raw output**: `ThinkingBlock` objects no longer print as raw Python objects — proper `isinstance` checks extract `.thinking` text (#162, closes #161)

## [1.5.0] - 2026-03-04

### Added
- **Voice Message Transcription**: Send voice messages for automatic transcription and Claude processing. Dual provider support: Mistral Voxtral (default) and OpenAI Whisper (#106)
- **`/restart` command**: Restart bot process from Telegram, plus `set_my_commands` timing fix for reliable command registration on startup (#112)
- **Streaming partial responses**: Stream Claude's output in real-time via Telegram `sendMessageDraft` API. Enable with `ENABLE_STREAM_DRAFTS=true` (#123)

### Fixed
- **`/actions` crash**: Corrected `SessionModel` constructor argument in `get_suggestions` (#125, closes #119)
- **Model config ignored**: `claude_model` setting now passed to SDK `ClaudeAgentOptions`. Default deferred to CLI instead of hardcoded sonnet (#121)

### Documentation
- Linux `aiolimiter` DBus installation workaround (#124)

## [1.4.0] - 2026-02-27

### Added
- **Outbound image support**: Claude can now auto-detect and send images to Telegram, plus MCP `send_image_to_user` tool (#99)
- **CLAUDE.md loading**: Project-level CLAUDE.md files are loaded from the working directory and appended to the system prompt
- **Configurable reply quoting**: `REPLY_QUOTE` setting controls message quoting behavior, centralized via PTB Defaults (#111)
- **`max_budget_usd` cost cap**: Per-request cost limit passed to SDK via `ClaudeAgentOptions` (#95)
- **`Skill` and `AskUserQuestion`** added to default allowed tools (#85, #87)
- **Documentation site**: Docs index and README linking (#92)

### Changed
- **ToolMonitor replaced with SDK `can_use_tool` callback**: Security validation now uses the native SDK hook instead of a custom wrapper. `SecurityValidator` wired directly into `ClaudeAgentOptions.can_use_tool` (#62)
- **`DISABLE_TOOL_VALIDATION=true`** now passes `allowed_tools=None` to the SDK, fully bypassing tool name validation
- **Phase 5 cleanup**: `src/claude/` reduced from 2,774 to 1,316 lines (#96)
- **PTB `AIORateLimiter`** replaces manual sync-local `RetryAfter` retry (#86)
- **Project thread sync throttling**: Configurable `PROJECT_THREADS_SYNC_ACTION_INTERVAL_SECONDS` to avoid Telegram API rate limits (#84)
- **GitHub Actions upgraded** to latest versions for Node 24 compatibility (#67, #68)

### Fixed
- **Empty `CLAUDE_CLI_PATH` causing Permission denied**: Empty string coerced to `None` so SDK auto-discovers the CLI
- **Session resume failing** with generic exit code 1 (#94)
- **Progress message deletion crash**: Bot no longer stops mid-response when progress message deletion fails (#107)
- **General topic routing**: Messages in the General topic of forum supergroups now route correctly (#110)
- **Session ownership enforcement**: `load_session` and `get_or_create_session` now validate ownership (#83)
- **Bash boundary enforcement**: `cd` and chained commands checked against directory boundary (#69)
- **Handler robustness**: Potential `UnboundLocalError` resolved in message handlers (#66)
- **Claude Code internal paths**: `~/.claude/plans/` and `todos/` allowed in tool validation (#89)
- **`Topic_not_modified` treated as success** in topic sync instead of raising an error
- **Test fixes**: `is_forum=False` set on MagicMock chats to prevent test failures (#110)

### Previously Added
- **Agentic Mode** (default interaction model):
  - `MessageOrchestrator` routes messages to agentic (3 commands) or classic (13 commands) handlers based on `AGENTIC_MODE` setting
  - Natural language conversation with Claude -- no terminal commands needed
  - Automatic session persistence per user/project directory
- **Event-Driven Platform**:
  - `EventBus` -- async pub/sub system with typed event subscriptions (UserMessage, Webhook, Scheduled, AgentResponse)
  - `AgentHandler` -- bridges events to `ClaudeIntegration.run_command()` for webhook and scheduled event processing
  - `EventSecurityMiddleware` -- validates events before handler processing
- **Webhook API Server** (FastAPI):
  - `POST /webhooks/{provider}` endpoint for GitHub, Notion, and generic providers
  - GitHub HMAC-SHA256 signature verification
  - Generic Bearer token authentication
  - Atomic deduplication via `webhook_events` table
  - Health check at `GET /health`
- **Job Scheduler** (APScheduler):
  - Cron-based job scheduling with persistent storage in `scheduled_jobs` table
  - Jobs publish `ScheduledEvent` to event bus on trigger
  - Add, remove, and list jobs programmatically
- **Notification Service**:
  - Subscribes to `AgentResponseEvent` for Telegram delivery
  - Per-chat rate limiting (1 msg/sec) to respect Telegram limits
  - Message splitting at 4096 char boundary
  - Broadcast to configurable default chat IDs
- **Database Migration 3**: `scheduled_jobs` and `webhook_events` tables, WAL mode enabled
- **Automatic Session Resumption**: Sessions are now automatically resumed per user+directory
  - SDK integration passes `resume` parameter to Claude Code for real session continuity
  - Session IDs extracted from Claude's `ResultMessage` instead of generated locally
  - `/cd` looks up and resumes existing sessions for the target directory
  - Auto-resume from SQLite database survives bot restarts
  - Graceful fallback to fresh session when resume fails
  - `/new` and `/end` are the only ways to explicitly clear session context

### Recently Completed

#### Storage Layer Implementation (TODO-6) - 2025-06-06
- **SQLite Database with Complete Schema**:
  - 7 core tables: users, sessions, messages, tool_usage, audit_log, user_tokens, cost_tracking
  - Foreign key relationships and proper indexing for performance
  - Migration system with schema versioning and automatic upgrades
  - Connection pooling for efficient database resource management
- **Repository Pattern Data Access Layer**:
  - UserRepository, SessionRepository, MessageRepository, ToolUsageRepository
  - AuditLogRepository, CostTrackingRepository, AnalyticsRepository
- **Persistent Session Management**:
  - SQLiteSessionStorage replacing in-memory storage
  - Session persistence across bot restarts and deployments
- **Analytics and Reporting System**:
  - User dashboards with usage statistics and cost tracking
  - Admin dashboards with system-wide analytics

#### Telegram Bot Core (TODO-4) - 2025-06-06
- Complete Telegram bot with command routing, message parsing, inline keyboards
- Navigation commands: /cd, /ls, /pwd for directory management
- Session commands: /new, /continue, /status for Claude sessions
- File upload support, progress indicators, response formatting

#### Claude Code Integration (TODO-5) - 2025-06-06
- Async process execution with timeout handling
- Session state management and cross-conversation continuity
- Streaming JSON output parsing, tool call extraction
- Cost tracking and usage monitoring

#### Authentication & Security Framework (TODO-3) - 2025-06-05
- Multi-provider authentication (whitelist + token)
- Rate limiting with token bucket algorithm
- Input validation, path traversal prevention
- Security audit logging with risk assessment
- Bot middleware framework (auth, rate limit, security, burst protection)

## [0.1.0] - 2025-06-05

### Added

#### Project Foundation (TODO-1)
- Complete project structure with Poetry dependency management
- Exception hierarchy, structured logging, testing framework
- Code quality tools: Black, isort, flake8, mypy with strict settings

#### Configuration System (TODO-2)
- Pydantic Settings v2 with environment variable loading
- Environment-specific overrides (development, testing, production)
- Feature flags system for dynamic functionality control
- Comprehensive validation with cross-field dependencies

## Development Status

- **TODO-1**: Project Structure & Core Setup -- Complete
- **TODO-2**: Configuration Management -- Complete
- **TODO-3**: Authentication & Security Framework -- Complete
- **TODO-4**: Telegram Bot Core -- Complete
- **TODO-5**: Claude Code Integration -- Complete
- **TODO-6**: Storage & Persistence -- Complete
- **TODO-7**: Advanced Features -- Complete (agentic platform, webhooks, scheduler, notifications)
- **TODO-8**: Complete Testing Suite -- In progress
- **TODO-9**: Deployment & Documentation -- In progress
