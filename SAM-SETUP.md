# Sam's fork setup notes

This is your fork of [RichardAtCT/claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram), with one local feature on `feat/session-id-routing`:

- **`/sessions [N]`** — list recent Claude sessions (any source: bot, CLI, IDE, scheduled tasks)
- **`/use <id-prefix>`** — resume a specific session by 4+ char id prefix

Both filter to `APPROVED_DIRECTORY` (your `Claude Homebase`) for safety.

## Prerequisites

- **Python 3.11+** (the project requires it; system Python on your Mac is 3.9). Install via:
  ```bash
  brew install python@3.11
  ```
- **Poetry** for dependency management:
  ```bash
  curl -sSL https://install.python-poetry.org | python3.11 -
  ```
- **Telegram bot token** — talk to [@BotFather](https://t.me/BotFather), `/newbot`, copy token. Different bot from the one your existing `notify.sh`/`ask.sh` use, OR reuse — see "Two bots vs one" below.
- **Your Telegram user ID** — message [@userinfobot](https://t.me/userinfobot) once, copy the numeric ID.

## Install the fork

```bash
cd "/Users/samgobrail/Desktop/Claude Homebase/Projects/forks/claude-code-telegram"
git checkout feat/session-id-routing
poetry env use python3.11
poetry install
```

## Configure

```bash
cp .env.example .env
$EDITOR .env
```

Minimum settings:
```env
TELEGRAM_BOT_TOKEN=<from BotFather>
TELEGRAM_BOT_USERNAME=<your bot username>
APPROVED_DIRECTORY=/Users/samgobrail/Desktop/Claude Homebase
ALLOWED_USERS=<your Telegram user id>
AGENTIC_MODE=true
```

Optional but useful:
```env
ENABLE_API_SERVER=true            # webhook receiver for routine notifications
ENABLE_SCHEDULER=true              # bot-side cron (not needed, your MCP routines stay)
NOTIFICATION_CHAT_IDS=<your chat>  # push notifications go here
VERBOSE_LEVEL=1                    # 0=quiet, 1=normal, 2=detailed
```

## Run it

```bash
# Foreground (to validate)
poetry run make run

# Or persistent via launchd — copy this and load it
```

Persistent launchd plist template (save to `~/Library/LaunchAgents/com.sam.claude-code-telegram.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.sam.claude-code-telegram</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/poetry</string>
        <string>run</string>
        <string>python</string>
        <string>-m</string>
        <string>src.main</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/samgobrail/Desktop/Claude Homebase/Projects/forks/claude-code-telegram</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key>
    <string>/Users/samgobrail/Desktop/Claude Homebase/Projects/Claude Routines/logs/claude-code-telegram.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/samgobrail/Desktop/Claude Homebase/Projects/Claude Routines/logs/claude-code-telegram.err.log</string>
</dict>
</plist>
```

Load with:
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sam.claude-code-telegram.plist
```

## Validate the new commands

In your Telegram chat with the bot:

```
/sessions                  → 10 most recent transcripts visible
/sessions 25               → 25 most recent
/use da21                  → resume session da21e144-...
```

The next message after `/use` continues that session via `claude --resume`.

## Two bots vs one

You already have `notify.sh` + `ask.sh` driving a Telegram bot for one-way routine notifications and Q&A. Options:

- **Two bots, one chat**: easiest. Have RichardAtCT's bot post in the same chat, but as a different bot. You can see both. Each does what it's best at.
- **One bot, shared token**: harder to wire — `notify.sh`/`ask.sh` use a Keychain-stored token, RichardAtCT reads from `.env`. Doable: just set `TELEGRAM_BOT_TOKEN` in `.env` to the same value `notify.sh` uses. But the bot's long-polling and your `ask.sh` polling will fight over `getUpdates`, which uses an offset cursor — `getUpdates` consumes events. They cannot coexist on the same token without one missing messages.

**Recommendation:** create a separate bot for RichardAtCT (5 min in BotFather). Keep your existing bot for one-way `notify.sh` pushes.

## Upstream PR

The `/sessions` + `/use` commands are useful beyond your setup. Consider opening a PR back to upstream:

```bash
gh pr create --repo RichardAtCT/claude-code-telegram \
  --title "Add /sessions and /use for arbitrary session resume" \
  --body-file <(echo "See CHANGELOG entry under Unreleased.")
```

Do NOT push this without reading the diff yourself first — `gh pr create` posts publicly.

## What's NOT changed in the fork

- All existing behavior (auto-resume per project, `/repo`, `/new`, `/status`, etc.) is intact
- No new env vars required
- Security model untouched: same `APPROVED_DIRECTORY` sandbox, same auth/audit/rate-limit pipeline
- `/use` only resumes sessions under `APPROVED_DIRECTORY`; sessions in other directories are silently invisible to the command

## Files I touched

- `src/claude/session_discovery.py` (new, 170 lines)
- `src/bot/orchestrator.py` (+139 lines)
- `tests/unit/test_claude/test_session_discovery.py` (new, 280 lines)
- `CHANGELOG.md` (added Unreleased entry)
