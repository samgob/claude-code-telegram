"""Tests for mailbox routing (Phase 2 no-watchers architecture)."""

import json
import tempfile
from pathlib import Path

import pytest

from src.bot.mailbox_router import append_to_mailbox, classify_route
from src.storage.facade import Storage


@pytest.fixture
async def storage():
    """Create test storage."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test.db"
        storage = Storage(f"sqlite:///{db_path}")
        await storage.initialize()
        yield storage
        await storage.close()


class TestAppendToMailbox:
    """Deterministic Python-side mailbox append."""

    def test_append_creates_jsonl_line(self, tmp_path):
        box = tmp_path / "feedback.jsonl"
        ok = append_to_mailbox(str(box), "V", "add oat milk", tmp_path, via="reply")
        assert ok
        lines = box.read_text().strip().splitlines()
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["sender"] == "V"
        assert row["text"] == "add oat milk"
        assert row["via"] == "reply"
        assert "ts" in row

    def test_append_is_additive(self, tmp_path):
        box = tmp_path / "feedback.jsonl"
        append_to_mailbox(str(box), "V", "first", tmp_path, via="reply")
        append_to_mailbox(str(box), "Sam", "second", tmp_path, via="router")
        lines = box.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[1])["sender"] == "Sam"

    def test_refuses_path_outside_approved_directory(self, tmp_path):
        approved = tmp_path / "approved"
        approved.mkdir()
        outside = tmp_path / "outside.jsonl"
        ok = append_to_mailbox(str(outside), "V", "nope", approved, via="reply")
        assert not ok
        assert not outside.exists()

    def test_refuses_traversal_out_of_approved_directory(self, tmp_path):
        approved = tmp_path / "approved"
        approved.mkdir()
        sneaky = approved / ".." / "escape.jsonl"
        ok = append_to_mailbox(str(sneaky), "V", "nope", approved, via="reply")
        assert not ok

    def test_creates_missing_parent_dirs(self, tmp_path):
        box = tmp_path / "deep" / "nested" / "feedback.jsonl"
        ok = append_to_mailbox(str(box), "Sam", "hi", tmp_path, via="router")
        assert ok
        assert box.exists()


class TestClassifyRoute:
    """Router fail-open behavior (no SDK call paths)."""

    async def test_empty_mailboxes_short_circuits(self):
        assert await classify_route("anything", []) is None


class TestMailboxStorage:
    """Mailbox registration, scoping, and expiry."""

    async def test_register_and_fetch(self, storage):
        mailbox_id = await storage.register_mailbox(
            routine="grocery-order",
            mailbox_path="/tmp/box.jsonl",
            topic="grocery order draft — awaiting feedback",
            scope="family",
            ttl_minutes=60,
        )
        assert mailbox_id > 0
        box = await storage.get_live_mailbox(mailbox_id)
        assert box is not None
        assert box["routine"] == "grocery-order"
        assert box["scope"] == "family"

    async def test_expired_mailbox_is_dead(self, storage):
        mailbox_id = await storage.register_mailbox(
            routine="stale",
            mailbox_path="/tmp/stale.jsonl",
            topic="old",
            ttl_minutes=-1,
        )
        assert await storage.get_live_mailbox(mailbox_id) is None
        assert await storage.get_live_mailboxes() == []

    async def test_family_scope_filter(self, storage):
        await storage.register_mailbox(
            routine="work-thing",
            mailbox_path="/tmp/work.jsonl",
            topic="work",
            scope="private",
        )
        await storage.register_mailbox(
            routine="grocery-order",
            mailbox_path="/tmp/grocery.jsonl",
            topic="groceries",
            scope="family",
        )
        family = await storage.get_live_mailboxes(family_only=True)
        assert [b["routine"] for b in family] == ["grocery-order"]
        everything = await storage.get_live_mailboxes(family_only=False)
        assert {b["routine"] for b in everything} == {"work-thing", "grocery-order"}

    async def test_reregistration_replaces_not_duplicates(self, storage):
        await storage.register_mailbox(
            routine="grocery-order",
            mailbox_path="/tmp/box.jsonl",
            topic="v1",
            scope="family",
        )
        await storage.register_mailbox(
            routine="grocery-order",
            mailbox_path="/tmp/box.jsonl",
            topic="v2",
            scope="family",
        )
        boxes = await storage.get_live_mailboxes()
        assert len(boxes) == 1
        assert boxes[0]["topic"] == "v2"
