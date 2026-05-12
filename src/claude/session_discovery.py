"""Discover Claude Code sessions on disk for /sessions and /use commands.

Sessions are persisted by Claude Code as JSONL transcript files under
``~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl``. The bot's SQLite
session store only knows about sessions that flowed through the bot itself, so
to expose sessions started elsewhere (e.g. scheduled tasks, direct CLI, IDE),
we scan the transcript files directly.

Each transcript embeds its original ``cwd`` in early records, which we extract
authoritatively rather than trying to reverse-engineer the encoded directory
name (the encoding is lossy: both ``/`` and spaces become ``-``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import List, Optional

import structlog

logger = structlog.get_logger()

DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
_SNIPPET_MAX = 120
_SCAN_RECORDS = 80  # how many leading records to scan for cwd + first user msg


@dataclass
class SessionInfo:
    """Summary of one Claude Code session discovered on disk."""

    session_id: str
    cwd: Optional[Path]
    last_modified: datetime
    snippet: str
    file_path: Path
    size_bytes: int

    @property
    def short_id(self) -> str:
        return self.session_id[:8]

    @property
    def age_seconds(self) -> float:
        return (datetime.now(UTC) - self.last_modified).total_seconds()

    def relative_age(self) -> str:
        s = self.age_seconds
        if s < 60:
            return f"{int(s)}s ago"
        if s < 3600:
            return f"{int(s / 60)}m ago"
        if s < 86400:
            return f"{int(s / 3600)}h ago"
        return f"{int(s / 86400)}d ago"


def _extract_session_metadata(file_path: Path) -> tuple[Optional[Path], str]:
    """Read first records of a JSONL transcript to extract cwd + user-message snippet.

    Returns ``(cwd, snippet)`` — either may be empty/None if the file is malformed
    or too short to contain them. Bounded to ``_SCAN_RECORDS`` records to keep
    the scan cheap on huge transcripts.
    """
    cwd: Optional[Path] = None
    snippet = ""
    try:
        with file_path.open("r", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= _SCAN_RECORDS:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cwd is None:
                    candidate = rec.get("cwd")
                    if candidate:
                        cwd = Path(candidate)
                if not snippet:
                    snippet = _extract_user_text(rec)
                if cwd and snippet:
                    break
    except OSError as e:
        logger.debug("Failed to read session file", path=str(file_path), error=str(e))
    return cwd, snippet[:_SNIPPET_MAX].replace("\n", " ").strip()


def _extract_user_text(rec: dict) -> str:
    """Pull the first user-text content from a record, if any."""
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return ""
    if msg.get("role") != "user":
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for chunk in content:
            if isinstance(chunk, dict) and chunk.get("type") == "text":
                txt = chunk.get("text", "")
                if isinstance(txt, str) and txt:
                    return txt
    return ""


def discover_sessions(
    limit: int = 20,
    projects_root: Path = DEFAULT_PROJECTS_ROOT,
    within_cwd: Optional[Path] = None,
) -> List[SessionInfo]:
    """List recent Claude Code sessions, newest first.

    Parameters
    ----------
    limit:
        Maximum number of sessions to return.
    projects_root:
        Override the project root (useful for tests).
    within_cwd:
        If provided, only return sessions whose recorded ``cwd`` equals (or is a
        child of) this path. Used by `/use` to filter to a sandboxed workspace.
    """
    if not projects_root.exists():
        return []

    all_files: List[Path] = []
    for project_dir in projects_root.iterdir():
        if not project_dir.is_dir():
            continue
        all_files.extend(project_dir.glob("*.jsonl"))

    all_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    results: List[SessionInfo] = []
    # Scan more than `limit` because some sessions may be filtered out by within_cwd.
    scan_budget = max(limit * 4, 40) if within_cwd else limit
    for path in all_files[:scan_budget]:
        try:
            stat = path.stat()
        except OSError:
            continue
        cwd, snippet = _extract_session_metadata(path)
        if within_cwd is not None:
            if cwd is None:
                continue
            try:
                cwd.resolve().relative_to(within_cwd.resolve())
            except (ValueError, OSError):
                continue
        results.append(
            SessionInfo(
                session_id=path.stem,
                cwd=cwd,
                last_modified=datetime.fromtimestamp(stat.st_mtime, UTC),
                snippet=snippet,
                file_path=path,
                size_bytes=stat.st_size,
            )
        )
        if len(results) >= limit:
            break
    return results


def find_by_prefix(
    prefix: str,
    projects_root: Path = DEFAULT_PROJECTS_ROOT,
    within_cwd: Optional[Path] = None,
) -> List[SessionInfo]:
    """Return sessions whose id starts with ``prefix``.

    Empty prefix returns ``[]`` rather than every session (defensive — callers
    should never request "all sessions" via this function).
    """
    if not prefix:
        return []
    # Pull a generous set; prefix match is cheap.
    candidates = discover_sessions(
        limit=500, projects_root=projects_root, within_cwd=within_cwd
    )
    return [s for s in candidates if s.session_id.startswith(prefix)]
