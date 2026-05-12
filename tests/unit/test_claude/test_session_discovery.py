"""Tests for session_discovery: scanning ~/.claude/projects/ for transcripts."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from src.claude.session_discovery import (
    SessionInfo,
    discover_sessions,
    find_by_prefix,
)


def _write_session(
    projects_root: Path,
    project_dirname: str,
    session_id: str,
    *,
    cwd: str | None,
    first_user_msg: str,
    mtime_offset: int = 0,
) -> Path:
    """Write a minimal JSONL transcript fixture and return its path."""
    project_dir = projects_root / project_dirname
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    records = []
    if cwd is not None:
        records.append({"type": "system", "cwd": cwd, "sessionId": session_id})
    records.append(
        {
            "type": "user",
            "message": {"role": "user", "content": first_user_msg},
            "sessionId": session_id,
        }
    )
    with path.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    if mtime_offset:
        target = time.time() + mtime_offset
        os.utime(path, (target, target))
    return path


def test_discover_sessions_returns_newest_first(tmp_path):
    root = tmp_path / "projects"
    _write_session(
        root,
        "-a",
        "11111111-aaaa-aaaa-aaaa-111111111111",
        cwd="/a",
        first_user_msg="oldest",
        mtime_offset=-100,
    )
    _write_session(
        root,
        "-a",
        "22222222-bbbb-bbbb-bbbb-222222222222",
        cwd="/a",
        first_user_msg="middle",
        mtime_offset=-50,
    )
    _write_session(
        root,
        "-a",
        "33333333-cccc-cccc-cccc-333333333333",
        cwd="/a",
        first_user_msg="newest",
    )
    out = discover_sessions(limit=10, projects_root=root)
    ids = [s.session_id for s in out]
    assert ids == [
        "33333333-cccc-cccc-cccc-333333333333",
        "22222222-bbbb-bbbb-bbbb-222222222222",
        "11111111-aaaa-aaaa-aaaa-111111111111",
    ]


def test_discover_sessions_extracts_cwd_and_snippet(tmp_path):
    root = tmp_path / "projects"
    _write_session(
        root,
        "-Users-sam-foo",
        "abcd1234-1111-2222-3333-444444444444",
        cwd="/Users/sam/foo",
        first_user_msg="Hello world from inside the session",
    )
    out = discover_sessions(limit=5, projects_root=root)
    assert len(out) == 1
    s = out[0]
    assert s.cwd == Path("/Users/sam/foo")
    assert s.snippet.startswith("Hello world")


def test_discover_sessions_extracts_content_list_text(tmp_path):
    """Snippet extraction must handle the 'content as list of typed chunks' format."""
    root = tmp_path / "projects"
    project = root / "-x"
    project.mkdir(parents=True)
    path = project / "deadbeef-1111-2222-3333-444444444444.jsonl"
    rec = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": "chunked content"},
                {"type": "image", "source": "..."},
            ],
        },
    }
    with path.open("w") as f:
        f.write(json.dumps({"type": "system", "cwd": "/x"}) + "\n")
        f.write(json.dumps(rec) + "\n")
    out = discover_sessions(limit=5, projects_root=root)
    assert out[0].snippet == "chunked content"


def test_discover_sessions_handles_missing_cwd(tmp_path):
    root = tmp_path / "projects"
    _write_session(
        root,
        "-x",
        "11111111-1111-1111-1111-111111111111",
        cwd=None,
        first_user_msg="hi",
    )
    out = discover_sessions(limit=5, projects_root=root)
    assert out[0].cwd is None
    assert out[0].snippet == "hi"


def test_discover_sessions_skips_malformed_lines(tmp_path):
    root = tmp_path / "projects"
    project = root / "-x"
    project.mkdir(parents=True)
    path = project / "feedcafe-1111-2222-3333-444444444444.jsonl"
    with path.open("w") as f:
        f.write("not json at all\n")
        f.write(json.dumps({"type": "system", "cwd": "/x"}) + "\n")
        f.write("also garbage\n")
        f.write(json.dumps({"message": {"role": "user", "content": "valid"}}) + "\n")
    out = discover_sessions(limit=5, projects_root=root)
    assert len(out) == 1
    assert out[0].cwd == Path("/x")
    assert out[0].snippet == "valid"


def test_within_cwd_filter(tmp_path):
    root = tmp_path / "projects"
    _write_session(
        root,
        "-a",
        "11111111-aaaa-1111-1111-111111111111",
        cwd="/Users/sam/work",
        first_user_msg="A",
    )
    _write_session(
        root,
        "-b",
        "22222222-bbbb-2222-2222-222222222222",
        cwd="/Users/sam/personal",
        first_user_msg="B",
    )
    _write_session(
        root,
        "-c",
        "33333333-cccc-3333-3333-333333333333",
        cwd="/Users/sam/work/sub",
        first_user_msg="C",
    )
    out = discover_sessions(
        limit=10, projects_root=root, within_cwd=Path("/Users/sam/work")
    )
    cwds = sorted(str(s.cwd) for s in out)
    assert cwds == ["/Users/sam/work", "/Users/sam/work/sub"]


def test_within_cwd_drops_unknown_cwd(tmp_path):
    """A session with no cwd in its transcript is excluded when within_cwd is set."""
    root = tmp_path / "projects"
    _write_session(
        root, "-x", "11111111-1111-1111-1111-111111111111", cwd=None, first_user_msg="x"
    )
    _write_session(
        root,
        "-y",
        "22222222-2222-2222-2222-222222222222",
        cwd="/Users/sam/work",
        first_user_msg="y",
    )
    out = discover_sessions(
        limit=10, projects_root=root, within_cwd=Path("/Users/sam/work")
    )
    assert len(out) == 1
    assert out[0].session_id.startswith("2222")


def test_find_by_prefix_empty_returns_empty(tmp_path):
    root = tmp_path / "projects"
    _write_session(
        root, "-a", "11111111-aaaa-1111-1111-111111111111", cwd="/a", first_user_msg="a"
    )
    assert find_by_prefix("", projects_root=root) == []


def test_find_by_prefix_single_match(tmp_path):
    root = tmp_path / "projects"
    _write_session(
        root, "-a", "11111111-aaaa-1111-1111-111111111111", cwd="/a", first_user_msg="a"
    )
    _write_session(
        root, "-a", "22222222-bbbb-2222-2222-222222222222", cwd="/a", first_user_msg="b"
    )
    out = find_by_prefix("11111111", projects_root=root)
    assert len(out) == 1
    assert out[0].session_id.startswith("11111111")


def test_find_by_prefix_multiple_matches(tmp_path):
    root = tmp_path / "projects"
    _write_session(
        root, "-a", "abcd1111-1111-1111-1111-111111111111", cwd="/a", first_user_msg="x"
    )
    _write_session(
        root, "-a", "abcd2222-2222-2222-2222-222222222222", cwd="/a", first_user_msg="y"
    )
    out = find_by_prefix("abcd", projects_root=root)
    assert len(out) == 2


def test_returns_empty_if_root_missing(tmp_path):
    assert discover_sessions(projects_root=tmp_path / "nope") == []
    assert find_by_prefix("abc", projects_root=tmp_path / "nope") == []


def test_session_info_relative_age_buckets():
    """Spot-check relative_age formatting across the size buckets."""
    from datetime import UTC, datetime, timedelta

    def make(delta_seconds):
        return SessionInfo(
            session_id="x",
            cwd=None,
            last_modified=datetime.now(UTC) - timedelta(seconds=delta_seconds),
            snippet="",
            file_path=Path("/tmp/x"),
            size_bytes=0,
        )

    assert make(5).relative_age().endswith("s ago")
    assert make(120).relative_age().endswith("m ago")
    assert make(3600 * 2).relative_age().endswith("h ago")
    assert make(86400 * 3).relative_age().endswith("d ago")
