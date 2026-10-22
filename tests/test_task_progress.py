"""Offline task timeouts, safe stopping, pending writes, and honest summaries."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from telethon import errors

from test_engine import FakeClient, message
from tgmover import engine
from tgmover.config import Config
from tgmover.engine import Mover, SyncError, UserStopped
from tgmover.filters import FilterSettings
from tgmover.limits import LimitSettings
from tgmover.state import State


class TaskProgressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.state = State(self.base / "state.sqlite3")
        self.addCleanup(self.state.close)
        self.config = Config(
            api_id=12345, api_hash="a" * 32, source="source", target="target",
            mode="reupload", comment_mode="reupload", sync_comments=False,
            session=self.base / "session", database=self.base / "state.sqlite3",
            download_dir=self.base / "downloads", start_id=0, poll_interval=0,
            settle_seconds=2, max_flood_wait=300, send_delay=0,
            limits=LimitSettings(enabled=False), filters=FilterSettings(),
        )
        self.client = FakeClient()
        self.stopped = False
        self.events = []
        for name, value in (("READ_TIMEOUT_SECONDS", 0.025), ("DOWNLOAD_IDLE_SECONDS", 0.025),
                            ("READ_POLL_SECONDS", 0.003), ("HEARTBEAT_SECONDS", 0.01)):
            patcher = patch.object(engine, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def mover(self, **overrides):
        mover = Mover(self.client, replace(self.config, **overrides), self.state,
                      should_stop=lambda: self.stopped, on_status=self.events.append)
        await mover.prepare()
        return mover

    def assert_not_committed(self, mover):
        self.assertIsNone(self.state.get_message(mover.route_id, "post", 1))
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)
        self.assertEqual(self.state.pending(mover.route_id), [])
        self.assertIsNone(mover.summary)

    async def test_unresponsive_read_times_out_and_cancels_only_read(self):
        mover = await self.mover()
        cancelled = asyncio.Event()

        async def hung():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with self.assertRaisesRegex(SyncError, "未收到响应"):
            # Allow several Windows timer ticks to observe the heartbeat
            # before the deliberately shortened read deadline expires.
            await asyncio.wait_for(mover.read(hung, timeout=0.1), 0.5)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(any("已等待" in event["message"] for event in self.events))
        self.assert_not_committed(mover)

    async def test_stop_during_read_cancels_promptly_without_advancing(self):
        mover = await self.mover()
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def hung():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = asyncio.create_task(mover.read(hung))
        await asyncio.wait_for(started.wait(), 0.5)
        self.stopped = True
        with self.assertRaises(UserStopped):
            await asyncio.wait_for(task, 0.5)
        self.assertTrue(cancelled.is_set())
        self.assert_not_committed(mover)

    async def test_intentional_limiter_wait_is_outside_network_timeout(self):
        mover = await self.mover()

        class SlowReadLimiter:
            async def read_wait(self):
                await asyncio.sleep(0.06)

            def check_runtime(self):
                pass

        mover.limiter = SlowReadLimiter()

        async def response():
            return "response"

        self.assertEqual(await mover.read(response), "response")

    async def test_hung_scan_page_times_out_and_preserves_cursor(self):
        mover = await self.mover()
        self.client.add(1, [message(1)])
        cancelled = asyncio.Event()

        async def hung_page():
            try:
                await asyncio.Event().wait()
                yield message(1)
            finally:
                cancelled.set()

        self.client.iter_messages = lambda *_args, **_kwargs: hung_page()
        with self.assertRaisesRegex(SyncError, "新增消息"):
            await asyncio.wait_for(mover.run_once(), 1.5)
        self.assertTrue(cancelled.is_set())
        self.assert_not_committed(mover)

    async def test_stop_during_scan_page_cancels_read_not_a_send(self):
        mover = await self.mover()
        self.client.add(1, [message(1)])
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def hung_page():
            started.set()
            try:
                await asyncio.Event().wait()
                yield message(1)
            finally:
                cancelled.set()

        self.client.iter_messages = lambda *_args, **_kwargs: hung_page()
        task = asyncio.create_task(mover.run_once())
        await asyncio.wait_for(started.wait(), 0.5)
        self.stopped = True
        with self.assertRaises(UserStopped):
            await asyncio.wait_for(task, 0.5)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.client.sends(), [])
        self.assert_not_committed(mover)

    async def test_local_page_wait_can_exceed_rpc_timeout_without_false_failure(self):
        mover = await self.mover()
        self.client.add(1, [message(1)])

        async def delayed_page():
            # A Telethon page includes its local wait_time as well as its RPC.
            await asyncio.sleep(0.06)
            yield message(1)

        self.client.iter_messages = lambda *_args, **_kwargs: delayed_page()
        summary = await asyncio.wait_for(mover.run_once(), 0.5)
        self.assertEqual(summary["round_counts"]["sent"], 1)

    async def test_idle_download_times_out_and_does_not_record_a_file_or_message(self):
        mover = await self.mover()
        cancelled = asyncio.Event()

        async def hung_download(_item, *, file, progress_callback=None):
            Path(file).write_bytes(b"P")
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.client.download_media = hung_download
        with self.assertRaisesRegex(SyncError, "未收到响应或下载进度"):
            await asyncio.wait_for(mover.process([message(1, media=True)], "post", "reupload"), 0.5)
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(self.state.download(mover.route_id, "post", 1))
        self.assertEqual(self.client.sends(), [])
        self.assert_not_committed(mover)

    async def test_stop_during_download_cancels_download_without_pending_send(self):
        mover = await self.mover()
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def hung_download(_item, *, file, progress_callback=None):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.client.download_media = hung_download
        task = asyncio.create_task(mover.process([message(1, media=True)], "post", "reupload"))
        await asyncio.wait_for(started.wait(), 0.5)
        self.stopped = True
        with self.assertRaises(UserStopped):
            await asyncio.wait_for(task, 0.5)
        self.assertTrue(cancelled.is_set())
        self.assert_not_committed(mover)

    async def test_download_progress_refreshes_idle_deadline_and_accepts_unknown_total(self):
        mover = await self.mover()

        async def active_download(_item, *, file, progress_callback):
            for current in range(1, 5):
                await asyncio.sleep(0.015)
                progress_callback(current, None)
            Path(file).write_bytes(b"DATA")
            return file

        self.client.download_media = active_download
        path = await asyncio.wait_for(mover.download(message(1, media=True), "post"), 0.5)
        self.assertEqual(path.read_bytes(), b"DATA")
        self.assertEqual(self.state.download(mover.route_id, "post", 1)["size"], 4)
        self.assertTrue(any(event.get("total_bytes", "absent") is None for event in self.events))

    async def test_limiter_delay_does_not_use_up_download_idle_allowance(self):
        mover = await self.mover()

        class SlowReadLimiter:
            async def read_wait(self):
                await asyncio.sleep(0.06)

            def check_runtime(self):
                pass

        mover.limiter = SlowReadLimiter()
        path = await asyncio.wait_for(mover.download(message(1, media=True), "post"), 0.5)
        self.assertEqual(path.read_bytes(), b"DATA")

    async def test_download_retry_after_flood_cooldown_gets_new_idle_allowance(self):
        mover = await self.mover()
        original_download = self.client.download_media
        attempts = []

        class CoolingReadLimiter:
            wait_calls = 0

            async def read_wait(self):
                self.wait_calls += 1
                if self.wait_calls > 1:
                    await asyncio.sleep(0.06)

            def check_runtime(self):
                pass

            def cooldown(self, *_args, **_kwargs):
                pass

        async def download(item, *, file, progress_callback):
            attempts.append(item.id)
            if len(attempts) == 1:
                raise errors.FloodWaitError(None, capture=1)
            return await original_download(item, file=file, progress_callback=progress_callback)

        mover.limiter = CoolingReadLimiter()
        self.client.download_media = download
        path = await asyncio.wait_for(mover.download(message(1, media=True), "post"), 0.5)
        self.assertEqual(path.read_bytes(), b"DATA")
        self.assertEqual(attempts, [1, 1])

    async def test_unchanged_byte_callbacks_do_not_hide_an_idle_download(self):
        mover = await self.mover()
        cancelled = asyncio.Event()

        async def zero_progress(_item, *, file, progress_callback):
            try:
                while True:
                    progress_callback(0, 4)
                    await asyncio.sleep(0.005)
            finally:
                cancelled.set()

        self.client.download_media = zero_progress
        with self.assertRaisesRegex(SyncError, "未收到响应或下载进度"):
            await asyncio.wait_for(mover.download(message(1, media=True), "post"), 0.3)
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(self.state.download(mover.route_id, "post", 1))

    async def test_stop_during_inflight_send_waits_for_result_then_commits(self):
        mover = await self.mover()
        started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def delayed_send(destination, text, **kwargs):
            started.set()
            try:
                await release.wait()
                return self.client._sent(destination, caption=text)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        self.client.send_message = delayed_send
        task = asyncio.create_task(mover.process([message(1)], "post", "reupload"))
        await asyncio.wait_for(started.wait(), 0.5)
        self.stopped = True
        await asyncio.sleep(0.06)
        self.assertFalse(task.done())
        self.assertFalse(cancelled.is_set())
        self.assertEqual(len(self.state.pending(mover.route_id)), 1)
        self.assertIsNone(self.state.get_message(mover.route_id, "post", 1))
        self.assertTrue(any("已请求停止" in event["message"] for event in self.events))
        release.set()
        with self.assertRaises(UserStopped):
            await asyncio.wait_for(task, 0.5)
        self.assertFalse(cancelled.is_set())
        self.assertEqual(self.state.get_message(mover.route_id, "post", 1)["target_id"], 101)
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 1)
        self.assertEqual(self.state.pending(mover.route_id), [])
        self.assertEqual(mover.counts["sent"], 1)
        self.assertIsNone(mover.summary)

    async def test_network_failure_keeps_uncertain_pending_and_never_emits_completion(self):
        mover = await self.mover()
        self.client.add(1, [message(1)])
        self.client.send_failure = ConnectionError("offline connection lost")
        with self.assertRaises(ConnectionError):
            await mover.run_once()
        self.assertEqual(len(self.state.pending(mover.route_id)), 1)
        self.assertIsNone(self.state.get_message(mover.route_id, "post", 1))
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)
        self.assertIsNone(mover.summary)
        self.assertFalse(any(event["phase"] in {"round_complete", "completed"} for event in self.events))

    async def test_external_send_cancellation_preserves_pending_instead_of_claiming_success(self):
        mover = await self.mover()
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def delayed_send(destination, text, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.client.send_message = delayed_send
        task = asyncio.create_task(mover.process([message(1)], "post", "reupload"))
        await asyncio.wait_for(started.wait(), 0.5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(self.state.pending(mover.route_id)), 1)
        self.assertIsNone(self.state.get_message(mover.route_id, "post", 1))
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)
        self.assertIsNone(mover.summary)

    async def test_empty_source_summary_explicitly_reports_zero_sends(self):
        mover = await self.mover()
        summary = await mover.run_once()
        self.assertEqual(summary["reason"], "source_empty")
        self.assertIn("发送 0 条", summary["message"])
        self.assertIn("没有可读取", summary["message"])

    async def test_settling_summary_does_not_claim_recent_message_was_sent(self):
        mover = await self.mover(settle_seconds=300)
        self.client.add(1, [message(1, date=datetime.now(timezone.utc))])
        summary = await mover.run_once()
        self.assertEqual(summary["reason"], "settling")
        self.assertIn("等待期", summary["message"])
        self.assertEqual(summary["round_counts"]["sent"], 0)
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)

    async def test_start_after_latest_summary_explains_no_content(self):
        mover = await self.mover(start_id=20)
        self.client.add(1, [message(10)])
        summary = await mover.run_once()
        self.assertEqual(summary["reason"], "start_after_latest")
        self.assertIn("最新 ID 10", summary["message"])
        self.assertIn("起点 ID 20", summary["message"])
        self.assertEqual(summary["round_counts"]["sent"], 0)

    async def test_caught_up_summary_uses_round_counts_instead_of_prior_sends(self):
        mover = await self.mover()
        self.client.add(1, [message(1)])
        first = await mover.run_once()
        second = await mover.run_once()
        self.assertEqual(first["round_counts"]["sent"], 1)
        self.assertEqual(second["reason"], "no_new")
        self.assertEqual(second["round_counts"]["sent"], 0)
        self.assertEqual(second["counts"]["sent"], 1)
        self.assertIn("发送 0 条", second["message"])

    async def test_all_adverts_summary_explains_filtering_instead_of_successful_sends(self):
        mover = await self.mover()
        self.client.add(1, [message(1, text="广告合作"), message(2, text="商务合作")])
        summary = await mover.run_once()
        self.assertEqual(summary["reason"], "filtered_all")
        self.assertEqual(summary["round_counts"]["filtered"], 2)
        self.assertEqual(summary["round_counts"]["sent"], 0)
        self.assertIn("未发送", summary["message"])
        self.assertEqual(self.client.sends(), [])

    async def test_backup_summary_reports_files_but_never_target_sends(self):
        mover = await self.mover(mode="download_only", comment_mode="download_only")
        self.client.add(1, [message(1, media=True), message(2, text="saved text")])
        summary = await mover.run_once()
        self.assertEqual(summary["reason"], "download_only")
        self.assertEqual(summary["round_counts"]["files"], 1)
        self.assertEqual(summary["round_counts"]["sent"], 0)
        self.assertIn("纯下载备份", summary["message"])
        self.assertIn("不向目标发送", summary["message"])
        self.assertEqual(self.client.sends(), [])

    async def test_recent_comment_is_reported_as_deferred_not_no_new(self):
        mover = await self.mover(sync_comments=True)
        self.client.add(1, [message(1)])
        self.client.link_post(1, 10)
        await mover.run_once()
        self.client.add(3, [message(11, peer=3, root=10, date=datetime.now(timezone.utc))], root=10)

        summary = await mover.run_once()

        self.assertEqual(summary["reason"], "settling")
        self.assertIn("等待期", summary["message"])
        self.assertEqual(summary["round_counts"]["sent"], 0)
        self.assertEqual(self.state.cursor(mover.route_id, "comment", 10), 0)


if __name__ == "__main__":
    unittest.main()
