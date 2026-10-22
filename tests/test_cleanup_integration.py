"""Verify real download/send/commit ordering and the local GUI cleanup worker."""
from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from telethon import errors

import test_engine as engine_fixture
import test_gui_worker as worker_fixture
from tgmover import gui_worker
from tgmover.cleanup import cleanup_forwarded
from tgmover.engine import SyncError
from tgmover.state import State


class CleanupEngineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    setUp = engine_fixture.EngineTests.setUp
    mover = engine_fixture.EngineTests.mover

    def album(self):
        self.client.add(1, [engine_fixture.message(1, media=True, grouped_id=77),
                            engine_fixture.message(2, media=True, grouped_id=77)])

    def registered_paths(self, mover):
        return [Path(self.state.download(mover.route_id, "post", identifier)["path"])
                for identifier in (1, 2)]

    async def test_selected_folder_contains_complete_album_before_forward_and_defaults_to_keep(self):
        self.album()
        selected = self.base / "other-disk" / "chosen-folder"
        original = self.client.forward_messages

        async def inspect_then_forward(destination, batch, **kwargs):
            paths = self.registered_paths(mover)
            self.assertTrue(all(path.is_relative_to(selected) for path in paths))
            self.assertEqual([path.read_bytes() for path in paths], [b"DATA", b"DATA"])
            self.assertEqual(self.state.messages(mover.route_id, "post"), [])
            return await original(destination, batch, **kwargs)

        self.client.forward_messages = inspect_then_forward
        mover = await self.mover(mode="download_then_forward", sync_comments=False, download_dir=selected)
        await mover.run_once()
        self.assertTrue(all(path.is_file() for path in self.registered_paths(mover)))
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 2)
        self.assertEqual(len(self.client.sends()), 1)

    async def test_auto_cleanup_runs_after_album_commit_and_preserves_incremental_state(self):
        self.album()
        mover = await self.mover(mode="download_then_forward", sync_comments=False,
                                 auto_cleanup_forwarded=True)
        await mover.run_once()
        paths = self.registered_paths(mover)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(mover.cleanup_counts, {"deleted_files": 2, "freed_bytes": 8})
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 2)
        self.assertEqual([row["target_id"] for row in self.state.messages(mover.route_id, "post")], [101, 102])
        self.assertEqual(self.state.pending(mover.route_id), [])
        self.assertEqual(self.state.download(mover.route_id, "post", 1)["cleanup_status"], "deleted")
        await mover.run_once()
        self.assertEqual(len(self.client.sends()), 1)

    async def test_auto_checkbox_cannot_delete_backup_or_reupload_files(self):
        self.album()
        for mode in ("download_only", "reupload"):
            with self.subTest(mode=mode):
                mover = await self.mover(mode=mode, sync_comments=False, auto_cleanup_forwarded=True)
                await mover.run_once()
                self.assertTrue(all(path.is_file() for path in self.registered_paths(mover)))
                self.assertEqual(mover.cleanup_counts["deleted_files"], 0)

    async def test_failed_or_uncertain_forward_keeps_archives_and_does_not_advance(self):
        self.album()
        mover = await self.mover(mode="download_then_forward", sync_comments=False,
                                 auto_cleanup_forwarded=True)
        self.client.send_failure = errors.ChatWriteForbiddenError(request=None)
        with self.assertRaises(SyncError):
            await mover.run_once()
        self.assertTrue(all(path.is_file() for path in self.registered_paths(mover)))
        self.assertEqual(self.state.pending(mover.route_id), [])
        self.client.send_failure = ConnectionError("lost send result")
        with self.assertRaises(ConnectionError):
            await mover.run_once()
        self.assertTrue(all(path.is_file() for path in self.registered_paths(mover)))
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)
        self.assertEqual(self.state.messages(mover.route_id, "post"), [])
        self.assertEqual(len(self.state.pending(mover.route_id)), 1)
        self.assertEqual(cleanup_forwarded(self.state, roots=[mover.config.download_dir])["deleted_files"], 0)

    async def test_incomplete_album_result_keeps_all_archives(self):
        self.album()
        original = self.client.forward_messages

        async def incomplete(*args, **kwargs):
            return (await original(*args, **kwargs))[:1]

        self.client.forward_messages = incomplete
        mover = await self.mover(mode="download_then_forward", sync_comments=False,
                                 auto_cleanup_forwarded=True)
        with self.assertRaises(SyncError):
            await mover.run_once()
        self.assertTrue(all(path.is_file() for path in self.registered_paths(mover)))
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)
        self.assertEqual(self.state.pending(mover.route_id)[0]["source_ids"], [1, 2])

    async def test_cleanup_failure_never_turns_confirmed_forward_into_a_resend(self):
        self.album()
        mover = await self.mover(mode="download_then_forward", sync_comments=False,
                                 auto_cleanup_forwarded=True)
        with patch("tgmover.engine.cleanup_forwarded", side_effect=OSError("disk busy")):
            await mover.run_once()
        self.assertTrue(all(path.is_file() for path in self.registered_paths(mover)))
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 2)
        self.assertEqual(self.state.pending(mover.route_id), [])
        await mover.run_once()
        self.assertEqual(len(self.client.sends()), 1)
        result = cleanup_forwarded(self.state, roots=[mover.config.download_dir])
        self.assertEqual(result["deleted_files"], 2)

    async def test_changed_folder_redownloads_unfinished_batch_without_deleting_old_folder(self):
        self.album()
        mover = await self.mover(mode="download_then_forward", sync_comments=False,
                                 auto_cleanup_forwarded=True)
        old_root = mover.config.download_dir
        self.client.send_failure = errors.ChatWriteForbiddenError(request=None)
        with self.assertRaises(SyncError):
            await mover.run_once()
        old_paths = self.registered_paths(mover)
        self.client.send_failure = None
        mover = await self.mover(mode="download_then_forward", sync_comments=False,
                                 auto_cleanup_forwarded=True, download_dir=self.base / "second-disk")
        await mover.run_once()
        self.assertTrue(all(path.is_file() for path in old_paths))
        self.assertTrue(all(not path.exists() for path in self.registered_paths(mover)))
        self.assertEqual(len([call for call in self.client.calls if call[0] == "download"]), 4)
        self.assertEqual(self.state.conn.execute("SELECT COUNT(*) FROM download_files").fetchone()[0], 4)
        self.assertEqual(cleanup_forwarded(self.state, roots=[old_root])["deleted_files"], 2)

    async def test_text_only_forward_does_not_run_media_cleanup(self):
        self.client.add(1, [engine_fixture.message(1)])
        mover = await self.mover(mode="download_then_forward", sync_comments=False,
                                 auto_cleanup_forwarded=True)
        with patch("tgmover.engine.cleanup_forwarded", side_effect=AssertionError("no media")) as clean:
            await mover.run_once()
        clean.assert_not_called()
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 1)


class CleanupWorkerIntegrationTests(unittest.TestCase):
    setUp = worker_fixture.GuiWorkerTests.setUp
    client_factory = worker_fixture.GuiWorkerTests.client_factory
    wait_for = worker_fixture.GuiWorkerTests.wait_for
    perform = worker_fixture.GuiWorkerTests.perform
    dispose = worker_fixture.GuiWorkerTests.dispose

    @classmethod
    def setUpClass(cls):
        cls.application = worker_fixture.QApplication.instance() or worker_fixture.QApplication([])

    def archive(self):
        with_state = State(self.config.database)
        try:
            route = with_state.route({"mode": "download_then_forward", "comment_mode": "reupload"})
            path = self.config.download_dir / route / "post" / "1" / "1_media.bin"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"DATA")
            with_state.save_download(route, "post", 1, path, 4, root=self.config.download_dir)
            with_state.save_messages(route, "post", [{"source_id": 1, "target_id": 101}], cursor=1)
            return route, path
        finally:
            with_state.close()

    def test_manual_cleanup_uses_background_worker_without_account_or_network(self):
        route, path = self.archive()
        results = []
        self.controller.cleanup_ready.connect(results.append)
        config = replace(self.config, source=None, target=None, api_id=0, api_hash="")
        self.assertEqual(self.perform(lambda: self.controller.cleanup_downloads(config)), "cleaned")
        self.assertEqual(self.failures, [])
        self.assertEqual(self.clients, [])
        self.assertFalse(path.exists())
        self.assertEqual((results[0]["deleted_files"], results[0]["freed_bytes"]), (1, 4))
        self.assertEqual(self.phases[-1]["phase"], "cleaned")
        state = State(self.config.database)
        try:
            self.assertEqual(state.cursor(route, "post"), 1)
            self.assertEqual(state.get_message(route, "post", 1)["target_id"], 101)
        finally:
            state.close()
        self.assertTrue(worker_fixture.TrackedState.instances)
        self.assertTrue(all(item.closed and item.created_thread == item.closed_thread != self.main_thread
                            for item in worker_fixture.TrackedState.instances))

    def test_manual_cleanup_without_database_creates_no_database_and_no_login(self):
        self.assertEqual(self.perform(lambda: self.controller.cleanup_downloads(self.config)), "cleaned")
        self.assertFalse(self.config.database.exists())
        self.assertEqual(self.clients, [])
        self.assertEqual(worker_fixture.TrackedState.instances, [])
        self.assertEqual(self.failures, [])

    def test_manual_cleanup_reports_occupied_file_and_can_retry(self):
        _, path = self.archive()
        results = []
        self.controller.cleanup_ready.connect(results.append)
        with patch("tgmover.cleanup._unlink_verified", side_effect=PermissionError(13, "busy")):
            self.perform(lambda: self.controller.cleanup_downloads(self.config))
        self.assertTrue(path.is_file())
        self.assertEqual(results[-1]["failed_files"], 1)
        self.assertIn("重试", results[-1]["message"])
        self.perform(lambda: self.controller.cleanup_downloads(self.config))
        self.assertFalse(path.exists())
        self.assertEqual(results[-1]["deleted_files"], 1)
        self.assertEqual(self.clients, [])


if __name__ == "__main__":
    unittest.main()
