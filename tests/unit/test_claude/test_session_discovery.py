"""Tests for session_discovery: scanning ~/.claude/projects/ for transcripts."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from src.claude.session_discovery import (
    SessionInfo,
    _tokenize_query,
    discover_sessions,
    find_by_prefix,
    search_sessions,
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


# --- search_sessions / tokenizer -------------------------------------------


def test_tokenize_drops_stopwords_and_recency_hints():
    tokens = _tokenize_query("the latest wesco session about a poc")
    # Drops: the, latest (recency hint, now a stopword), session, about, a
    assert tokens == ["wesco", "poc"]


def test_tokenize_dedups_and_lowercases():
    tokens = _tokenize_query("Wesco WESCO wesco POC poc")
    assert tokens == ["wesco", "poc"]


def test_tokenize_empty_returns_no_tokens():
    tokens = _tokenize_query("the a session")
    assert tokens == []


def test_search_finds_keyword_in_first_message(tmp_path):
    root = tmp_path / "projects"
    _write_session(
        root,
        "-a",
        "11111111-aaaa-1111-1111-111111111111",
        cwd="/Users/sam/work",
        first_user_msg="Help me on the Wesco POC scoring",
    )
    _write_session(
        root,
        "-a",
        "22222222-bbbb-2222-2222-222222222222",
        cwd="/Users/sam/work",
        first_user_msg="Unrelated question about LexisNexis",
    )
    out = search_sessions("wesco", projects_root=root)
    assert len(out) == 1
    assert out[0].session_id.startswith("1111")


def test_search_requires_all_tokens(tmp_path):
    """Multi-token query is AND, not OR."""
    root = tmp_path / "projects"
    _write_session(
        root,
        "-a",
        "aaaaaaaa-aaaa-1111-1111-111111111111",
        cwd="/x",
        first_user_msg="wesco only",
    )
    _write_session(
        root,
        "-a",
        "bbbbbbbb-bbbb-2222-2222-222222222222",
        cwd="/x",
        first_user_msg="wesco and poc together",
    )
    out = search_sessions("wesco poc", projects_root=root)
    assert len(out) == 1
    assert out[0].session_id.startswith("bbbb")


def test_search_sorts_by_recency_when_hits_comparable(tmp_path):
    """Default: recency-first when no hit-count winner."""
    root = tmp_path / "projects"
    _write_session(
        root,
        "-a",
        "aaaaaaaa-aaaa-1111-1111-111111111111",
        cwd="/x",
        first_user_msg="wesco mentioned once here",  # older
        mtime_offset=-1000,
    )
    _write_session(
        root,
        "-a",
        "bbbbbbbb-bbbb-2222-2222-222222222222",
        cwd="/x",
        first_user_msg="wesco mentioned once here too",  # newer
    )
    out = search_sessions("wesco", projects_root=root)
    assert out[0].session_id.startswith("bbbb")
    assert out[1].session_id.startswith("aaaa")


def test_search_relevance_floor_drops_incidental_mentions(tmp_path):
    """A multi-hit older session beats a 1-hit newer one because the
    relevance floor drops 1-hit incidentals when stronger matches exist."""
    root = tmp_path / "projects"
    _write_session(
        root,
        "-a",
        "aaaaaaaa-aaaa-1111-1111-111111111111",
        cwd="/x",
        first_user_msg="wesco wesco wesco mentioned many times",
        mtime_offset=-1000,
    )
    _write_session(
        root,
        "-a",
        "bbbbbbbb-bbbb-2222-2222-222222222222",
        cwd="/x",
        first_user_msg="wesco mentioned once",  # newer but 1 hit
    )
    out = search_sessions("wesco", projects_root=root)
    assert len(out) == 1
    assert out[0].session_id.startswith("aaaa")


def test_search_keeps_all_when_all_singleton_hits(tmp_path):
    """If every match is a 1-hit, none get filtered — recency wins."""
    root = tmp_path / "projects"
    _write_session(
        root,
        "-a",
        "aaaaaaaa-aaaa-1111-1111-111111111111",
        cwd="/x",
        first_user_msg="wesco mentioned once",
        mtime_offset=-1000,
    )
    _write_session(
        root,
        "-a",
        "bbbbbbbb-bbbb-2222-2222-222222222222",
        cwd="/x",
        first_user_msg="wesco only once here too",
    )
    out = search_sessions("wesco", projects_root=root)
    assert len(out) == 2
    assert out[0].session_id.startswith("bbbb")


def test_search_respects_within_cwd(tmp_path):
    root = tmp_path / "projects"
    _write_session(
        root,
        "-a",
        "aaaaaaaa-aaaa-1111-1111-111111111111",
        cwd="/users/sam/work",
        first_user_msg="wesco in work",
    )
    _write_session(
        root,
        "-b",
        "bbbbbbbb-bbbb-2222-2222-222222222222",
        cwd="/users/sam/personal",
        first_user_msg="wesco in personal",
    )
    out = search_sessions(
        "wesco", projects_root=root, within_cwd=Path("/users/sam/work")
    )
    assert len(out) == 1
    assert out[0].session_id.startswith("aaaa")


def test_search_empty_query_returns_empty(tmp_path):
    root = tmp_path / "projects"
    _write_session(
        root,
        "-a",
        "aaaaaaaa-aaaa-1111-1111-111111111111",
        cwd="/x",
        first_user_msg="anything",
    )
    # Only stopwords + recency hint → no searchable tokens → empty result
    assert search_sessions("the latest session", projects_root=root) == []


def test_search_scans_deep_content_not_just_first_message(tmp_path):
    """Keyword in a later record should still match (transcript-wide scan)."""
    root = tmp_path / "projects"
    project = root / "-a"
    project.mkdir(parents=True)
    path = project / "cccccccc-1111-1111-1111-111111111111.jsonl"
    # First user message is generic; the keyword lives in a later record.
    with path.open("w") as f:
        f.write(json.dumps({"type": "system", "cwd": "/x"}) + "\n")
        f.write(json.dumps({"message": {"role": "user", "content": "hello"}}) + "\n")
        f.write(
            json.dumps(
                {"type": "assistant", "content": "Sure, I'll check Wesco POC accuracy"}
            )
            + "\n"
        )
    out = search_sessions("wesco", projects_root=root)
    assert len(out) == 1


def test_search_limit_caps_results(tmp_path):
    root = tmp_path / "projects"
    for i in range(8):
        sid = f"{i:08d}-aaaa-1111-1111-111111111111"
        _write_session(root, "-a", sid, cwd="/x", first_user_msg=f"wesco run {i}")
    out = search_sessions("wesco", projects_root=root, limit=3)
    assert len(out) == 3


# --- routine detection + filtering -----------------------------------------


def _write_routine_session(
    root: Path,
    session_id: str,
    cwd: str,
    routine_name: str,
    mtime_offset: int = 0,
) -> Path:
    """A scheduled-task transcript: first user message is the <scheduled-task> envelope."""
    return _write_session(
        root,
        "-x",
        session_id,
        cwd=cwd,
        first_user_msg=f'<scheduled-task name="{routine_name}" file="/skill.md">\nrun the thing\n</scheduled-task>',
        mtime_offset=mtime_offset,
    )


def test_discover_excludes_routines_by_default(tmp_path):
    root = tmp_path / "projects"
    _write_routine_session(
        root, "11111111-1111-1111-1111-111111111111", "/x", "email-slack-triage-morning"
    )
    _write_session(
        root,
        "-x",
        "22222222-2222-2222-2222-222222222222",
        cwd="/x",
        first_user_msg="my own work",
    )
    out = discover_sessions(limit=10, projects_root=root)
    ids = [s.session_id for s in out]
    assert any(i.startswith("2222") for i in ids)
    assert not any(i.startswith("1111") for i in ids)


def test_discover_includes_routines_when_asked(tmp_path):
    root = tmp_path / "projects"
    _write_routine_session(
        root, "11111111-1111-1111-1111-111111111111", "/x", "personal-email-triage"
    )
    _write_session(
        root,
        "-x",
        "22222222-2222-2222-2222-222222222222",
        cwd="/x",
        first_user_msg="my own work",
    )
    out = discover_sessions(limit=10, projects_root=root, include_routines=True)
    assert len(out) == 2
    routines = [s for s in out if s.is_routine]
    assert len(routines) == 1
    assert routines[0].session_id.startswith("1111")


def test_search_excludes_routines_by_default(tmp_path):
    """`/use personal email` should not return personal-email-triage routine runs."""
    root = tmp_path / "projects"
    _write_routine_session(
        root,
        "11111111-1111-1111-1111-111111111111",
        "/x",
        "personal-email-triage",
    )
    _write_session(
        root,
        "-x",
        "22222222-2222-2222-2222-222222222222",
        cwd="/x",
        first_user_msg="Help me draft a personal email to my brother",
    )
    out = search_sessions("personal email", projects_root=root)
    ids = [s.session_id for s in out]
    assert any(i.startswith("2222") for i in ids)
    assert not any(i.startswith("1111") for i in ids)


def test_search_includes_routines_when_asked(tmp_path):
    root = tmp_path / "projects"
    _write_routine_session(
        root,
        "11111111-1111-1111-1111-111111111111",
        "/x",
        "personal-email-triage",
    )
    out = search_sessions("personal email", projects_root=root, include_routines=True)
    assert len(out) == 1
    assert out[0].is_routine is True


def test_find_by_prefix_includes_routines_by_default(tmp_path):
    """Prefix lookup is exact-intent: include routines so the typed id resolves
    even when it points at a scheduled-task transcript."""
    root = tmp_path / "projects"
    _write_routine_session(
        root, "11111111-1111-1111-1111-111111111111", "/x", "system-architect"
    )
    out = find_by_prefix("11111111", projects_root=root)
    assert len(out) == 1
    assert out[0].is_routine is True
