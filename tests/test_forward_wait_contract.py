"""Bound uncertain forwards without replay: real Telethon dispatch, no network.

The MTProto sender contracts deliberately prove why merely cancelling an RPC
future does not remove an outgoing request. Engine integration then retains the
durable pending operation when no complete target mapping can be confirmed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
import unittest
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from telethon import TelegramClient, errors, functions, types
from telethon.extensions import BinaryReader
from telethon.network.mtprotosender import MTProtoSender
from telethon.sessions import MemorySession

from test_engine import OLD, channel, message
from tgmover import engine, gui_worker
from tgmover.config import Config
from tgmover.engine import Mover, SyncError, UserStopped
from tgmover.limits import LimitSettings
from tgmover.state import State


def config_for(root):
    return Config(api_id=12345, api_hash="a" * 32, source="source", target="target",
                  mode="forward", comment_mode="download_only", sync_comments=False,
                  session=root / "session", database=root / "state.sqlite3",
                  download_dir=root / "downloads", start_id=0, poll_interval=0,
                  settle_seconds=0, max_flood_wait=300, send_delay=0,
                  limits=LimitSettings(enabled=False))


class SenderQueueContractTests(unittest.IsolatedAsyncioTestCase):
    def sender(self):
        sender = MTProtoSender(None, loggers=defaultdict(lambda: logging.getLogger("offline.sender")))
        sender._user_connected = True
        return sender

    def request(self):
        return functions.messages.ForwardMessagesRequest(
            from_peer=types.InputPeerChannel(1, 100), id=[15220],
            to_peer=types.InputPeerChannel(2, 200), random_id=[123456789],
        )

    async def test_cancelled_future_is_still_packed_for_sending_by_real_telethon(self):
        sender = self.sender()
        future = sender.send(self.request())
        future.cancel()

        batch, encoded = await sender._send_queue.get()

        self.assertEqual(len(batch), 1)
        self.assertTrue(batch[0].future.cancelled())
        self.assertIsInstance(batch[0].request, functions.messages.ForwardMessagesRequest)
        self.assertTrue(encoded)

    async def test_disconnect_stops_sender_but_does_not_clear_its_unsent_queue(self):
        sender = self.sender()

        class NoNetworkConnection:
            closed = False

            async def disconnect(self):
                self.closed = True

        connection = NoNetworkConnection()
        sender._connection = connection
        future = sender.send(self.request())

        await sender.disconnect()

        self.assertTrue(connection.closed)
        self.assertFalse(sender.is_connected())
        self.assertEqual(len(sender._send_queue._deque), 1)
        with self.assertRaises(ConnectionError):
            sender.send(self.request())
        # Reconnecting this same sender later would expose the old queue. The
        # application must discard the client, while retaining its DB pending.
        batch, encoded = await sender._send_queue.get()
        self.assertEqual(batch[0].request.id, [15220])
        self.assertTrue(encoded)
        future.cancel()


class ForwardSender:
    def __init__(self, client):
        self.client = client
        self.requests = []
        self.futures = []
        self.started = asyncio.Event()
        self.immediate = False
        self.omit_mapping = False
        self.failure = None

    def result(self, request):
        updates = []
        for index, random_id in enumerate(request.random_id):
            target = message(10000 + request.id[index], peer=request.to_peer.channel_id)
            if request.top_msg_id:
                target.reply_to = types.MessageReplyHeader(
                    forum_topic=True, reply_to_msg_id=request.top_msg_id)
            updates.append(types.UpdateNewChannelMessage(message=target, pts=index + 1, pts_count=1))
            if not self.omit_mapping or index:
                updates.append(types.UpdateMessageID(id=target.id, random_id=random_id))
        return types.Updates(updates=list(reversed(updates)), users=[],
                             chats=[self.client.target], date=OLD, seq=0)

    def send(self, request, ordered=False):
        while isinstance(request, functions.InvokeWithoutUpdatesRequest):
            request = request.query
        with BinaryReader(bytes(request)) as reader:
            request = reader.tgread_object()
        if not isinstance(request, functions.messages.ForwardMessagesRequest):
            raise AssertionError(f"Unexpected offline request: {type(request).__name__}")
        self.requests.append(request)
        future = asyncio.get_running_loop().create_future()
        self.futures.append(future)
        self.started.set()
        if self.failure:
            future.set_exception(self.failure)
        elif self.immediate:
            future.set_result(self.result(request))
        return future

    def complete(self):
        for request, future in zip(self.requests, self.futures):
            if not future.done():
                future.set_result(self.result(request))


class OfflineForwardClient(TelegramClient):
    def __init__(self):
        super().__init__(MemorySession(), 12345, "a" * 32,
                         request_retries=0, raise_last_call_error=True,
                         flood_sleep_threshold=0, receive_updates=False)
        self.source, self.target = channel(1), channel(2)
        self.entities = {"source": self.source, "target": self.target}
        self.session.process_entities(types.messages.Chats(chats=[self.source, self.target]))
        self._sender = ForwardSender(self)
        self.disconnect_count = 0

    async def connect(self):
        raise AssertionError("These contracts must never connect to Telegram")

    async def disconnect(self):
        self.disconnect_count += 1
        for future in self._sender.futures:
            future.cancel()

    async def get_me(self):
        return SimpleNamespace(id=42)

    async def get_entity(self, value):
        if isinstance(value, (types.Channel, types.Chat)):
            return value
        return self.entities[value]


class ForwardWaitEngineContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = config_for(self.root)
        self.state = State(self.config.database)
        self.addCleanup(self.state.close)
        self.client = OfflineForwardClient()
        self.events = []
        self.stopped = False
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, number in (("SEND_RPC_TIMEOUT_SECONDS", 0.06), ("UPLOAD_IDLE_SECONDS", 0.035),
                             ("SEND_POLL_SECONDS", 0.003), ("HEARTBEAT_SECONDS", 0.01)):
            self.stack.enter_context(patch.object(engine, name, number))

    async def mover(self):
        result = Mover(self.client, self.config, self.state,
                       should_stop=lambda: self.stopped, on_status=self.events.append)
        await result.prepare()
        return result

    def assert_pending(self, mover, ids):
        pending = self.state.pending(mover.route_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["source_ids"], ids)
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)
        for identifier in ids:
            self.assertIsNone(self.state.get_message(mover.route_id, "post", identifier))
        self.assertIsNone(mover.summary)

    async def test_hung_real_forward_finishes_bounded_with_pending_and_no_replay(self):
        mover = await self.mover()

        with self.assertRaises(engine.SendOutcomeUnknown):
            await asyncio.wait_for(mover.process([message(15220)], "post", "forward"), timeout=0.6)

        self.assert_pending(mover, [15220])
        self.assertEqual(len(self.client._sender.requests), 1)
        self.assertTrue(self.client._sender.futures[0].cancelled())
        self.assertGreaterEqual(self.client.disconnect_count, 1)
        self.assertTrue(any(event.get("phase") in {"forwarding", "uncertain"} for event in self.events))
        self.assertFalse(any(event.get("phase") in {"completed", "round_complete"} for event in self.events))

    async def test_new_run_is_blocked_by_pending_before_a_second_forward(self):
        mover = await self.mover()
        with self.assertRaises(engine.SendOutcomeUnknown):
            await mover.process([message(15220)], "post", "forward")
        replacement = OfflineForwardClient()
        replacement._sender.immediate = True
        next_mover = Mover(replacement, self.config, self.state)

        with self.assertRaisesRegex(SyncError, "待人工核对|pending|待核对"):
            await next_mover.prepare()

        self.assertEqual(replacement._sender.requests, [])
        self.assert_pending(mover, [15220])

    async def test_same_mover_cannot_reuse_closed_client_or_create_another_pending_send(self):
        mover = await self.mover()
        with self.assertRaises(engine.SendOutcomeUnknown):
            await mover.process([message(15220)], "post", "forward")

        with self.assertRaises(engine.SendOutcomeUnknown):
            await mover.process([message(15221)], "post", "forward")

        self.assertEqual(len(self.client._sender.requests), 1)
        self.assert_pending(mover, [15220])

    async def test_stop_waits_for_complete_response_then_commits_before_stopping(self):
        mover = await self.mover()
        with patch.object(engine, "SEND_RPC_TIMEOUT_SECONDS", 0.25):
            task = asyncio.create_task(mover.process([message(15220)], "post", "forward"))
            await asyncio.wait_for(self.client._sender.started.wait(), timeout=0.3)
            self.stopped = True
            await asyncio.sleep(0.012)
            self.assertFalse(task.done())
            self.assertFalse(self.client._sender.futures[0].cancelled())
            self.client._sender.complete()

            with self.assertRaises(UserStopped):
                await asyncio.wait_for(task, timeout=0.3)

        self.assertEqual(len(self.client._sender.requests), 1)
        self.assertEqual(self.state.pending(mover.route_id), [])
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 15220)
        self.assertEqual(self.state.get_message(mover.route_id, "post", 15220)["target_id"], 25220)
        self.assertEqual(self.client.disconnect_count, 0)

    async def test_stop_without_a_response_still_reaches_unknown_timeout_boundary(self):
        mover = await self.mover()
        task = asyncio.create_task(mover.process([message(15220)], "post", "forward"))
        await asyncio.wait_for(self.client._sender.started.wait(), timeout=0.3)
        self.stopped = True

        with self.assertRaises(engine.SendOutcomeUnknown):
            await asyncio.wait_for(task, timeout=0.6)

        self.assert_pending(mover, [15220])
        self.assertEqual(len(self.client._sender.requests), 1)
        self.assertGreaterEqual(self.client.disconnect_count, 1)

    async def test_album_is_one_ordered_rpc_and_only_complete_mapping_commits(self):
        mover = await self.mover()
        self.client._sender.immediate = True
        batch = [message(15220, grouped_id=7), message(15221, grouped_id=7)]

        await mover.process(batch, "post", "forward")

        request, = self.client._sender.requests
        self.assertEqual(request.id, [15220, 15221])
        self.assertEqual(len(set(request.random_id)), 2)
        self.assertEqual(self.state.pending(mover.route_id), [])
        self.assertEqual([self.state.get_message(mover.route_id, "post", item.id)["target_id"] for item in batch],
                         [25220, 25221])

    async def test_missing_mapping_is_immediate_failure_not_another_send_or_wait(self):
        mover = await self.mover()
        self.client._sender.immediate = True
        self.client._sender.omit_mapping = True

        with patch.object(logging.getLogger("telethon.client.messageparse"), "warning"):
            with self.assertRaises(SyncError):
                await asyncio.wait_for(mover.process([message(15220)], "post", "forward"), timeout=0.3)

        self.assert_pending(mover, [15220])
        self.assertEqual(len(self.client._sender.requests), 1)

    async def test_explicit_rejection_clears_pending_and_surfaces_without_unknown_wait(self):
        mover = await self.mover()
        self.client._sender.failure = errors.ChatForwardsRestrictedError(None)
        self.client.download_media = AsyncMock(side_effect=AssertionError("Never download around source protection"))
        self.client.send_file = AsyncMock(side_effect=AssertionError("Never reupload around source protection"))
        self.client.send_message = AsyncMock(side_effect=AssertionError("Never copy around source protection"))

        with self.assertRaisesRegex(SyncError, "内容保护|禁止.*转发|保护.*转发") as caught:
            await asyncio.wait_for(mover.process([message(15220, media=True)], "post", "forward"), timeout=0.3)

        self.assertNotIsInstance(caught.exception, engine.SendOutcomeUnknown)
        self.assertNotIn("等待时间已保存", str(caught.exception))
        self.assertEqual(self.state.pending(mover.route_id), [])
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)
        self.assertEqual(len(self.client._sender.requests), 1)
        self.assertEqual(self.client.disconnect_count, 0)
        self.client.download_media.assert_not_called()
        self.client.send_file.assert_not_called()
        self.client.send_message.assert_not_called()

    async def test_noforwards_precheck_stops_both_forward_modes_before_media_quota_or_pending(self):
        class TrackingLimiter:
            def __init__(self):
                self.reservations = []

            async def read_wait(self):
                pass

            def check_runtime(self):
                pass

            async def reserve(self, *args):
                self.reservations.append(args)

        for mode in ("forward", "download_then_forward"):
            with self.subTest(mode=mode):
                client = OfflineForwardClient()
                client.source.noforwards = True
                client.download_media = AsyncMock(side_effect=AssertionError("No protected media download"))
                client.send_file = AsyncMock(side_effect=AssertionError("No protected media fallback"))
                limiter = TrackingLimiter()
                mover = Mover(client, replace(self.config, mode=mode), self.state, limiter=limiter)

                with self.assertRaisesRegex(SyncError, "内容保护|禁止.*转发|保护.*转发") as caught:
                    await mover.prepare()

                self.assertNotIsInstance(caught.exception, engine.SendOutcomeUnknown)
                self.assertEqual(client._sender.requests, [])
                self.assertEqual(limiter.reservations, [])
                client.download_media.assert_not_called()
                client.send_file.assert_not_called()
                self.assertEqual(self.state.conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)
                self.assertEqual(self.state.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
                self.assertEqual(list(self.config.download_dir.glob("**/*")), [])

    async def test_negative_server_error_codes_keep_uncertain_pending(self):
        for code in (-500, -503):
            with self.subTest(code=code):
                # Unknown negative server codes use the generic RPCError
                # fallback in Telethon. Their sign does not mean rejection.
                client = OfflineForwardClient()
                client._sender.failure = errors.RPCError(None, "OFFLINE_UNKNOWN_SERVER_RESULT", code=code)
                mover = Mover(client, self.config, self.state)
                await mover.prepare()

                with self.assertRaises(SyncError):
                    await asyncio.wait_for(mover.process([message(15220)], "post", "forward"), timeout=0.5)

                self.assert_pending(mover, [15220])
                self.assertEqual(len(client._sender.requests), 1)
                self.assertGreaterEqual(client.disconnect_count, 1)
                # Each subtest has its own unknown outcome, not a second run
                # over the previous pending marker.
                pending, = self.state.pending(mover.route_id)
                self.state.finish_operation(mover.route_id, pending["scope"], pending["key"])

    async def test_forum_forward_keeps_topic_routing_and_one_ordered_album_request(self):
        mover = await self.mover()
        mover.target_topic_id = 21
        self.client._sender.immediate = True

        await mover.process([message(15220, grouped_id=7), message(15221, grouped_id=7)], "post", "forward")

        request, = self.client._sender.requests
        self.assertEqual((request.top_msg_id, request.id), (21, [15220, 15221]))
        self.assertEqual(self.state.pending(mover.route_id), [])
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 15221)

    async def test_forum_forward_wait_is_also_bounded_and_keeps_pending(self):
        mover = await self.mover()
        mover.target_topic_id = 21

        with self.assertRaises(engine.SendOutcomeUnknown):
            await asyncio.wait_for(mover.process([message(15220)], "post", "forward"), timeout=0.6)

        request, = self.client._sender.requests
        self.assertEqual(request.top_msg_id, 21)
        self.assert_pending(mover, [15220])
        self.assertGreaterEqual(self.client.disconnect_count, 1)

    async def test_upload_activity_extends_idle_deadline_beyond_normal_rpc_deadline(self):
        mover = await self.mover()
        activity = [asyncio.get_running_loop().time()]
        started = activity[0]

        async def progressing_upload():
            for _ in range(6):
                await asyncio.sleep(0.02)
                activity[0] = asyncio.get_running_loop().time()
            return "uploaded"

        result = await asyncio.wait_for(mover._await_send(
            progressing_upload, timeout=0.08, activity=lambda: activity[0]), timeout=0.5)

        self.assertEqual(result, "uploaded")
        self.assertGreater(asyncio.get_running_loop().time() - started, engine.SEND_RPC_TIMEOUT_SECONDS)
        self.assertEqual(self.client.disconnect_count, 0)

    async def test_upload_idle_timeout_still_disconnects_and_reports_unknown(self):
        mover = await self.mover()
        activity = [asyncio.get_running_loop().time()]

        async def no_progress():
            await asyncio.Event().wait()

        with self.assertRaises(engine.SendOutcomeUnknown):
            await asyncio.wait_for(mover._await_send(
                no_progress, timeout=engine.UPLOAD_IDLE_SECONDS, activity=lambda: activity[0]), timeout=0.5)

        self.assertGreaterEqual(self.client.disconnect_count, 1)

    async def test_upload_heartbeat_without_advancing_progress_does_not_reset_idle_clock(self):
        mover = await self.mover()
        progress = mover.transfer_progress("uploading", 15220, "post")
        progress(1, 10)
        first = mover._upload_activity_at
        await asyncio.sleep(0.003)

        progress(1, 10)

        self.assertEqual(mover._upload_activity_at, first)
        progress(2, 10)
        self.assertGreater(mover._upload_activity_at, first)


class ForwardUnknownGuiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.config = config_for(Path(self.temporary.name))
        self.controller = gui_worker.GuiController()
        self.finished, self.failed, self.phases = [], [], []
        self.controller.finished.connect(self.finished.append)
        self.controller.failed.connect(self.failed.append)
        self.controller.task_phase.connect(self.phases.append)
        self.client = SimpleNamespace(disconnect_count=0)

        async def disconnect():
            self.client.disconnect_count += 1

        self.client.disconnect = disconnect

        async def authorized(_config):
            self.controller._client = self.client
            return SimpleNamespace(id=42)

        class UnknownMover:
            async def run(self):
                raise engine.SendOutcomeUnknown("Telegram 未返回完整结果，保留 pending，请先人工核对目标，勿直接重发。")

        self.controller._authorized = authorized
        self.controller._mover = lambda *_: UnknownMover()
        self.addCleanup(self.dispose)

    def wait_for(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Offline GUI outcome did not finish")

    def dispose(self):
        self.controller.shutdown()
        self.wait_for(lambda: "closed" in self.finished)
        self.controller._thread.join(timeout=2)
        self.assertFalse(self.controller._thread.is_alive())

    def test_uncertain_outcome_surfaces_and_discards_original_client(self):
        self.assertTrue(self.controller.run_task(self.config, once=True))
        self.wait_for(lambda: bool(self.finished) and not self.controller.busy)

        self.assertEqual(self.finished[-1], "uncertain")
        self.assertTrue(self.failed)
        self.assertTrue(any(event.get("phase") == "uncertain" for event in self.phases))
        self.assertIsNone(self.controller._client)
        self.assertGreaterEqual(self.client.disconnect_count, 1)
        self.assertNotIn("completed", self.finished)


if __name__ == "__main__":
    unittest.main()
