"""Independent cleanup boundaries: ownership, confirmed forwarding, and state.

No Telegram client or network is used. Files live only in per-test temporary
directories. Basic deletion/state-migration cases are covered elsewhere.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tgmover.cleanup import cleanup_forwarded
from tgmover.state import State


def value(result, name):
    return result[name] if isinstance(result, dict) else getattr(result, name)


class CleanupContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "downloads"
        self.state = State(self.base / "state.sqlite3")
        self.addCleanup(self.state.close)
        self.route = self.new_route()

    def new_route(self, *, mode="download_then_forward", comment_mode="download_then_forward", target=2):
        return self.state.route({
            "account": 42, "source": -1000000000001, "target": -(10**12 + target),
            "mode": mode, "comment_mode": comment_mode,
            "source_link": -1000000000003, "target_link": -1000000000004,
        })

    def download(self, identifier, *, route=None, scope="post", root=None, data=b"DATA", path=None):
        route = route or self.route
        root = root or self.root
        path = path or (root / route / scope / str(identifier) / f"{identifier}_media.bin")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.state.save_download(route, scope, identifier, path, len(data), root=root)
        return path

    def confirmed(self, identifier, *, route=None, scope="post", target_id=None, grouped_id=None, status="done"):
        route = route or self.route
        self.state.save_messages(route, scope, [{
            "source_id": identifier, "target_id": target_id if target_id is not None else identifier + 100,
            "grouped_id": grouped_id, "source_chat_id": -1000000000001 if scope == "post" else -1000000000003,
            "target_chat_id": -1000000000002 if scope == "post" else -1000000000004,
            "source_post_id": 1 if scope == "comment" else None,
            "source_root_id": 10 if scope == "comment" else None,
            "target_root_id": 10010 if scope == "comment" else None,
            "status": status, "text": "retained audit text", "metadata": {"fixture": "cleanup-contract"},
        }], cursor=identifier, thread_id=10 if scope == "comment" else 0)

    def protected_state(self):
        # File cleanup must preserve all sync, discussion, and uncertain-write
        # facts. download_files/downloads audit columns may legitimately change.
        return {table: [tuple(row) for row in self.state.conn.execute(f"SELECT * FROM {table} ORDER BY 1,2")]
                for table in ("routes", "messages", "cursors", "threads", "operations")}

    def test_scope_mode_does_not_clean_reuploaded_comments_with_forwarded_posts(self):
        route = self.new_route(comment_mode="reupload")
        post = self.download(1, route=route)
        comment = self.download(1, route=route, scope="comment")
        self.confirmed(1, route=route)
        self.confirmed(1, route=route, scope="comment")
        self.state.save_thread(route, 1, 10, 101, 10010, -1000000000003, -1000000000004)
        before = self.protected_state()

        result = cleanup_forwarded(self.state, roots=[self.root], route_id=route)

        self.assertEqual(value(result, "deleted_files"), 1)
        self.assertFalse(post.exists())
        self.assertTrue(comment.exists())
        self.assertEqual(self.protected_state(), before)
        self.assertIsNotNone(self.state.download(route, "post", 1)["cleaned_at"])
        self.assertIsNone(self.state.download(route, "comment", 1)["cleaned_at"])

    def test_shared_path_unconfirmed_other_route_protects_file_even_with_source_filter(self):
        path = self.download(1)
        self.confirmed(1)
        other = self.new_route(mode="download_only", comment_mode="download_only", target=9)
        self.state.save_download(other, "post", 9, path, path.stat().st_size, root=self.root)

        result = cleanup_forwarded(self.state, roots=[self.root], route_id=self.route, scope="post", source_ids=[1])

        self.assertEqual(value(result, "deleted_files"), 0)
        self.assertEqual(path.read_bytes(), b"DATA")

    def test_shared_path_successful_reupload_reference_is_not_a_forwarded_file(self):
        path = self.download(1)
        self.confirmed(1)
        other = self.new_route(mode="reupload", target=9)
        self.state.save_download(other, "post", 9, path, path.stat().st_size, root=self.root)
        self.confirmed(9, route=other)

        result = cleanup_forwarded(self.state, roots=[self.root], route_id=self.route)

        self.assertEqual(value(result, "deleted_files"), 0)
        self.assertTrue(path.exists())

    def test_same_size_replacement_is_not_deleted(self):
        path = self.download(1)
        self.confirmed(1)
        original = path.stat()
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(b"NEWW")
        os.replace(replacement, path)
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns + 2_000_000_000))

        result = cleanup_forwarded(self.state, roots=[self.root], route_id=self.route)

        self.assertEqual(value(result, "deleted_files"), 0)
        self.assertEqual(path.read_bytes(), b"NEWW")

    def test_directory_change_only_cleans_explicitly_authorized_root_and_retains_history(self):
        old_root, new_root = self.base / "old-downloads", self.base / "new-downloads"
        old = self.download(1, root=old_root)
        new = self.download(1, root=new_root)
        self.confirmed(1)
        before = self.protected_state()

        result = cleanup_forwarded(self.state, roots=[new_root], route_id=self.route)

        self.assertEqual(value(result, "deleted_files"), 1)
        self.assertFalse(new.exists())
        self.assertTrue(old.exists())
        self.assertEqual(self.protected_state(), before)
        later = cleanup_forwarded(self.state, roots=[old_root], route_id=self.route)
        self.assertEqual(value(later, "deleted_files"), 1)
        self.assertFalse(old.exists())
        self.assertEqual(self.protected_state(), before)

    def test_sibling_with_same_prefix_is_not_inside_authorized_root(self):
        authorized = self.base / "archive"
        authorized.mkdir()
        sibling = self.base / "archive-other"
        path = self.download(1, root=sibling)
        self.confirmed(1)

        result = cleanup_forwarded(self.state, roots=[authorized], route_id=self.route)

        self.assertEqual(value(result, "deleted_files"), 0)
        self.assertTrue(path.exists())

    def test_untracked_user_file_and_partial_download_are_not_deleted(self):
        path = self.download(1)
        self.confirmed(1)
        user_file = self.root / "personal-note.bin"
        user_file.write_bytes(b"USER")
        partial = path.with_suffix(".bin.part")
        partial.write_bytes(b"unfinished")

        cleanup_forwarded(self.state, roots=[self.root], route_id=self.route)

        self.assertEqual(user_file.read_bytes(), b"USER")
        self.assertEqual(partial.read_bytes(), b"unfinished")

    def test_symlink_replacement_does_not_delete_link_or_outside_user_file(self):
        path = self.download(1)
        self.confirmed(1)
        outside = self.base / "outside-user-file.bin"
        outside.write_bytes(b"USER")
        path.unlink()
        try:
            os.symlink(outside, path)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Creating symlinks is unavailable on this host: {type(exc).__name__}")

        result = cleanup_forwarded(self.state, roots=[self.root], route_id=self.route)

        self.assertEqual(value(result, "deleted_files"), 0)
        self.assertTrue(path.is_symlink())
        self.assertEqual(outside.read_bytes(), b"USER")

    def test_symlink_parent_replacement_cannot_move_cleanup_outside_root(self):
        path = self.download(1)
        self.confirmed(1)
        original_parent = path.parent
        moved = self.base / "moved-user-directory"
        original_parent.rename(moved)
        try:
            os.symlink(moved, original_parent, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Creating directory symlinks is unavailable: {type(exc).__name__}")

        result = cleanup_forwarded(self.state, roots=[self.root], route_id=self.route)

        self.assertEqual(value(result, "deleted_files"), 0)
        self.assertEqual((moved / path.name).read_bytes(), b"DATA")

    def test_pending_after_mapping_commit_preserves_media_and_journal(self):
        path = self.download(1)
        self.confirmed(1)
        self.state.begin_operation(self.route, "post", "post:0:1", [1])
        before = self.protected_state()

        result = cleanup_forwarded(self.state, roots=[self.root], route_id=self.route)

        self.assertEqual(value(result, "deleted_files"), 0)
        self.assertTrue(path.exists())
        self.assertEqual(self.protected_state(), before)

    def test_partial_album_confirmation_protects_whole_group_with_narrow_filter(self):
        first, second = self.download(1), self.download(2)
        self.confirmed(1, grouped_id=777)
        self.state.save_messages(self.route, "post", [{"source_id": 2, "target_id": None,
                                  "grouped_id": 777, "status": "done"}])

        result = cleanup_forwarded(self.state, roots=[self.root], route_id=self.route, source_ids=[1])

        self.assertEqual(value(result, "deleted_files"), 0)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_pending_album_member_protects_other_member_outside_requested_id(self):
        first, second = self.download(1), self.download(2)
        self.confirmed(1, grouped_id=777)
        self.confirmed(2, grouped_id=777)
        self.state.begin_operation(self.route, "post", "post:0:2", [2])

        result = cleanup_forwarded(self.state, roots=[self.root], route_id=self.route, source_ids=[1])

        self.assertEqual(value(result, "deleted_files"), 0)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_cleanup_audit_failure_cannot_roll_back_or_remove_sync_facts(self):
        self.download(1)
        self.confirmed(1)
        before = self.protected_state()
        self.state.conn.executescript("""
            CREATE TRIGGER refuse_cleanup_audit BEFORE UPDATE ON download_files
            WHEN NEW.cleaned_at IS NOT NULL
            BEGIN SELECT RAISE(FAIL, 'offline test audit failure'); END;
        """)

        try:
            cleanup_forwarded(self.state, roots=[self.root], route_id=self.route)
        except sqlite3.Error:
            pass

        self.assertEqual(self.protected_state(), before)


if __name__ == "__main__":
    unittest.main()
