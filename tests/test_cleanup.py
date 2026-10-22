from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tgmover.cleanup import cleanup_forwarded
from tgmover.state import State


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "downloads"
        self.state = State(self.directory / "state.sqlite3")
        self.route = self.state.route({"source": -1001, "target": -1002,
                                       "mode": "download_then_forward", "comment_mode": "reupload"})

    def tearDown(self):
        self.state.close()
        self.temporary.cleanup()

    def file(self, source_id=1, *, root=None, scope="post", content=b"archive", register=True):
        root = root or self.root
        path = root / self.route / scope / str(source_id) / f"{source_id}_media.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if register:
            self.state.save_download(self.route, scope, source_id, path, len(content), root=root)
        return path

    def done(self, source_id=1, *, scope="post", grouped_id=None, **kwargs):
        self.state.save_messages(self.route, scope, [{"source_id": source_id, "target_id": source_id + 100,
                                "grouped_id": grouped_id, **kwargs}], cursor=source_id)

    def clean(self, **kwargs):
        return cleanup_forwarded(self.state, roots=[self.root], **kwargs)

    def test_delete_confirmed_archive_retains_audit_progress_and_unregistered_files(self):
        path = self.file()
        self.done()
        extra = path.parent / "user-note.txt"
        extra.write_text("keep", encoding="utf-8")
        self.state.save_thread(self.route, 1, 2, 101, 102, -1003, -1004)
        before = self.state.get_message(self.route, "post", 1)
        result = self.clean()
        self.assertEqual((result["deleted_files"], result["freed_bytes"]), (1, 7))
        self.assertFalse(path.exists())
        self.assertEqual(extra.read_text(encoding="utf-8"), "keep")
        saved = self.state.download(self.route, "post", 1)
        self.assertEqual((saved["path"], saved["size"], saved["cleanup_status"]), (str(path), 7, "deleted"))
        self.assertTrue(saved["cleaned_at"])
        self.assertEqual(self.state.get_message(self.route, "post", 1), before)
        self.assertEqual(self.state.cursor(self.route, "post"), 1)
        self.assertEqual(self.state.get_thread(self.route, 1)["target_root_id"], 102)
        self.assertEqual(self.clean()["deleted_files"], 0)

    def test_prunes_only_empty_tool_directories_never_selected_root(self):
        path = self.file()
        self.done()
        self.clean()
        self.assertTrue(self.root.is_dir())
        self.assertFalse(path.parent.exists())
        self.assertFalse((self.root / self.route).exists())

    def test_failed_deletion_is_retryable_without_losing_mapping_or_file(self):
        path = self.file()
        self.done()
        with patch("tgmover.cleanup._unlink_verified", side_effect=PermissionError(13, "private filename")):
            result = self.clean()
        self.assertEqual(result["failed_files"], 1)
        self.assertNotIn("private filename", str(result))
        self.assertTrue(path.is_file())
        self.assertEqual(self.state.download(self.route, "post", 1)["cleanup_status"], "error")
        self.assertEqual(self.clean()["deleted_files"], 1)
        self.assertEqual(self.state.get_message(self.route, "post", 1)["target_id"], 101)

    def test_missing_crash_recovery_never_claims_freed_bytes(self):
        path = self.file()
        self.done()
        path.unlink()
        result = self.clean()
        self.assertEqual((result["deleted_files"], result["missing_files"], result["freed_bytes"]), (0, 1, 0))
        self.assertEqual(self.state.download(self.route, "post", 1)["cleanup_status"], "missing")
        self.assertEqual(self.clean()["missing_files"], 0)

    def test_current_scope_and_batch_only(self):
        first, second, comment = self.file(1), self.file(2), self.file(1, scope="comment")
        self.done(1)
        self.done(2)
        self.done(1, scope="comment")
        result = self.clean(route_id=self.route, scope="post", source_ids=[2])
        self.assertEqual(result["deleted_files"], 1)
        self.assertTrue(first.exists())
        self.assertFalse(second.exists())
        self.assertTrue(comment.exists())

    def test_pending_album_keeps_every_member_even_with_committed_mapping(self):
        first, second = self.file(1), self.file(2)
        self.done(1, grouped_id=900)
        self.done(2, grouped_id=900)
        self.state.begin_operation(self.route, "post", "album", [2])
        self.assertEqual(self.clean()["deleted_files"], 0)
        self.assertTrue(first.exists() and second.exists())
        self.state.finish_operation(self.route, "post", "album")
        self.assertEqual(self.clean()["deleted_files"], 2)

    def test_unknown_album_member_from_metadata_stops_cleanup(self):
        path = self.file()
        self.done(grouped_id=900, metadata={"album_ids": [1, 2]})
        self.assertEqual(self.clean()["deleted_files"], 0)
        self.assertTrue(path.exists())

    def test_new_download_at_same_path_resets_cleanup_identity_and_flags(self):
        path = self.file()
        self.done()
        self.assertEqual(self.clean()["deleted_files"], 1)
        path = self.file(content=b"new download")
        row = self.state.download(self.route, "post", 1)
        self.assertIsNone(row["cleaned_at"])
        self.assertIsNone(row["cleanup_error"])
        self.assertEqual(row["cleanup_status"], "")
        self.assertEqual(row["mtime_ns"], path.stat().st_mtime_ns)
        result = self.clean()
        self.assertEqual((result["deleted_files"], result["freed_bytes"]), (1, 12))

    def test_root_switch_preserves_history_and_requires_return_to_old_root(self):
        old = self.file()
        self.done()
        other_root = self.directory / "new-downloads"
        new = self.file(root=other_root)
        self.assertEqual(self.state.download(self.route, "post", 1)["path"], str(new))
        self.assertEqual(self.state.conn.execute("SELECT COUNT(*) FROM download_files").fetchone()[0], 2)
        result = cleanup_forwarded(self.state, roots=[other_root])
        self.assertEqual(result["deleted_files"], 1)
        self.assertTrue(old.exists())
        self.assertFalse(new.exists())
        self.assertEqual(self.clean()["deleted_files"], 1)
        self.assertFalse(old.exists())
        self.assertEqual(self.state.download(self.route, "post", 1)["path"], str(new))

    def test_replaced_same_size_file_is_not_the_original_download(self):
        path = self.file()
        self.done()
        old_time = path.stat().st_mtime_ns
        path.unlink()
        path.write_bytes(b"changed")
        os.utime(path, ns=(old_time + 1_000_000_000, old_time + 1_000_000_000))
        self.assertEqual(self.clean()["deleted_files"], 0)
        self.assertEqual(path.read_bytes(), b"changed")

    def test_no_authorized_roots_is_read_only_for_disk(self):
        path = self.file()
        self.done()
        result = cleanup_forwarded(self.state)
        self.assertEqual(result["deleted_files"], 0)
        self.assertEqual(result["skipped_files"], 1)
        self.assertTrue(path.exists())

    def test_should_stop_preserves_remaining_files(self):
        first, second = self.file(1), self.file(2)
        self.done(1)
        self.done(2)
        calls = []
        def stop():
            calls.append(True)
            return len(calls) > 1
        result = self.clean(should_stop=stop)
        self.assertTrue(result["stopped"])
        self.assertEqual(result["deleted_files"], 1)
        self.assertEqual(sum(path.exists() for path in (first, second)), 1)

    def test_legacy_without_identity_is_retained_even_when_path_and_size_match(self):
        path = self.file(register=False)
        self.done()
        self.state.save_download(self.route, "post", 1, path, 7)
        self.state.conn.execute("UPDATE downloads SET st_dev=NULL, st_ino=NULL, mtime_ns=NULL")
        self.state.conn.execute("UPDATE download_files SET st_dev=NULL, st_ino=NULL, mtime_ns=NULL")
        self.state.conn.commit()
        result = self.clean()
        self.assertEqual(result["deleted_files"], 0)
        self.assertTrue(path.is_file())
        self.assertEqual(result["skip_reasons"].get("旧版记录缺少文件身份，已保留，请人工处理"), 1)
        with self.assertRaises(ValueError):
            self.state.save_download(self.route, "post", 2, self.directory / "elsewhere.bin", 2, root=self.root)

    def test_legacy_same_size_user_replacement_is_never_adopted_or_deleted(self):
        path = self.file(register=False)
        self.done()
        self.state.save_download(self.route, "post", 1, path, 7)
        for table in ("downloads", "download_files"):
            self.state.conn.execute(f"UPDATE {table} SET st_dev=NULL, st_ino=NULL, mtime_ns=NULL")
        self.state.conn.commit()
        path.unlink()
        path.write_bytes(b"userdoc")
        result = self.clean()
        self.assertEqual(result["deleted_files"], 0)
        self.assertEqual(path.read_bytes(), b"userdoc")
        self.assertEqual(result["skip_reasons"].get("旧版记录缺少文件身份，已保留，请人工处理"), 1)
        self.assertIsNone(self.state.download(self.route, "post", 1)["st_ino"])
        self.assertIsNone(self.state.conn.execute("SELECT st_ino FROM download_files").fetchone()[0])

    def test_rootless_record_with_complete_original_identity_can_be_cleaned(self):
        path = self.file(register=False)
        self.done()
        self.state.save_download(self.route, "post", 1, path, 7)
        self.assertIsNone(self.state.download(self.route, "post", 1)["root"])
        self.assertEqual(self.clean()["deleted_files"], 1)
        self.assertFalse(path.exists())

    def test_incomplete_new_identity_cannot_authorize_a_later_file(self):
        path = self.root / self.route / "post" / "1" / "1_media.bin"
        self.state.save_download(self.route, "post", 1, path, 7, root=self.root)
        self.done()
        path.parent.mkdir(parents=True)
        path.write_bytes(b"archive")
        self.assertEqual(self.clean()["deleted_files"], 0)
        self.assertTrue(path.exists())

    def test_zero_or_negative_target_id_is_not_confirmation(self):
        path = self.file()
        for target_id in (0, -1):
            self.done(target_id=target_id)
            self.assertEqual(self.clean()["deleted_files"], 0)
            self.assertTrue(path.exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction contract")
    def test_junction_parent_never_redirects_cleanup_outside_authorized_root(self):
        path = self.file()
        self.done()
        outside = self.directory / "retained-user-directory"
        path.parent.rename(outside)
        process = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(path.parent), str(outside)],
                                 capture_output=True, text=True)
        if process.returncode:
            self.skipTest("Creating directory junctions is unavailable")
        try:
            self.assertTrue(path.exists())
            result = self.clean()
            self.assertEqual(result["deleted_files"], 0)
            self.assertEqual((outside / path.name).read_bytes(), b"archive")
            self.assertTrue(path.parent.is_dir())
        finally:
            path.parent.rmdir()  # Remove exactly the junction, never its target.

    def test_file_ids_larger_than_sqlite_integer_roundtrip_losslessly(self):
        path = self.file(register=False)
        actual = path.lstat()
        wide_id = (1 << 127) + 123456789
        from types import SimpleNamespace
        info = SimpleNamespace(st_mode=actual.st_mode, st_size=actual.st_size,
                               st_dev=actual.st_dev, st_ino=wide_id, st_mtime_ns=actual.st_mtime_ns)
        with patch.object(Path, "lstat", return_value=info):
            self.state.save_download(self.route, "post", 1, path, 7, root=self.root)
        row = self.state.download(self.route, "post", 1)
        self.assertEqual(int(row["st_ino"], 0), wide_id)


class StateMigrationTests(unittest.TestCase):
    def test_old_database_download_is_preserved_in_history_without_claiming_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "old.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript("""
                CREATE TABLE routes(id TEXT PRIMARY KEY,signature TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE downloads(route TEXT,scope TEXT,source_id INTEGER,path TEXT,size INTEGER,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(route,scope,source_id));
                INSERT INTO routes(id,signature) VALUES ('legacy','{}');
            """)
            path = str(Path(temp) / "legacy.bin")
            connection.execute("INSERT INTO downloads(route,scope,source_id,path,size) VALUES (?,?,?,?,?)",
                               ("legacy", "post", 1, path, 5))
            connection.commit()
            connection.close()
            state = State(database)
            try:
                row = state.download("legacy", "post", 1)
                self.assertEqual((row["path"], row["size"]), (path, 5))
                self.assertIsNone(row["root"])
                self.assertIsNone(row["st_ino"])
                self.assertEqual(state.conn.execute("SELECT COUNT(*) FROM download_files").fetchone()[0], 1)
            finally:
                state.close()
            reopened = State(database)
            try:
                self.assertEqual(reopened.conn.execute("SELECT COUNT(*) FROM download_files").fetchone()[0], 1)
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
