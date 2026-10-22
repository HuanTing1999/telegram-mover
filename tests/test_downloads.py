"""Offline byte-integrity, protocol, bounded-pipeline and cleanup contracts."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telethon import TelegramClient, errors, functions, types
from telethon.extensions import BinaryReader
from telethon.sessions import MemorySession

from tgmover import downloads
from tgmover.downloads import CHUNK_SIZE, DownloadError, download_media


def media(size, *, reference=b"old-reference", doc_id=100, peer_id=123, message_id=7, dc_id=2):
    document = types.Document(id=doc_id, access_hash=101, file_reference=reference,
                              date=None, mime_type="application/octet-stream", size=size,
                              dc_id=dc_id, attributes=[])
    return types.Message(id=message_id, peer_id=types.PeerChannel(peer_id),
                         media=types.MessageMediaDocument(document=document), message="", date=None)


class ReadClient:
    def __init__(self, payload, *, delay=0):
        self.payload, self.delay = payload, delay
        self.session = SimpleNamespace(dc_id=1)
        self._sender = SimpleNamespace(dc_id=1)
        self.borrowed, self.returned, self.requests = [], [], []
        self.active = self.peak = self.cancelled = self.fallbacks = self.refreshes = 0
        self.expired = False
        self.fresh = None
        self.required_dc = None
        self.errors = {}
        self.gates = {}
        self.cdn = False
        self.bad_length = 0
        self.bad_result = False
        self.close_fail_dc = None

    async def _borrow_exported_sender(self, dc_id):
        await asyncio.sleep(0)
        self.borrowed.append(dc_id)
        return SimpleNamespace(dc_id=dc_id)

    async def _return_exported_sender(self, sender):
        self.returned.append(sender.dc_id)
        await asyncio.sleep(0)
        if sender.dc_id == self.close_fail_dc:
            raise ConnectionError("offline return failure")

    async def _call(self, sender, request):
        # Round-trip the actual MTProto request schema, not a dict imitation.
        assert isinstance(request, functions.upload.GetFileRequest)
        with BinaryReader(bytes(request)) as reader:
            decoded = reader.tgread_object()
        assert (decoded.offset, decoded.limit) == (request.offset, CHUNK_SIZE)
        assert request.offset % CHUNK_SIZE == 0
        assert request.offset // 1048576 == (request.offset + request.limit - 1) // 1048576
        self.requests.append((sender.dc_id, request))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            if request.offset in self.gates:
                await self.gates[request.offset].wait()
            if self.required_dc and sender.dc_id != self.required_dc:
                raise errors.FileMigrateError(request, self.required_dc)
            if self.expired and request.location.file_reference == b"old-reference":
                raise errors.FileReferenceExpiredError(request)
            failures = self.errors.get(request.offset, [])
            if failures:
                raise failures.pop(0)
            if self.cdn:
                return types.upload.FileCdnRedirect(dc_id=4, file_token=b"token",
                                                   encryption_key=b"k" * 32,
                                                   encryption_iv=b"i" * 16, file_hashes=[])
            if self.bad_result:
                return SimpleNamespace(bytes=b"unverified")
            data = self.payload[request.offset:request.offset + CHUNK_SIZE]
            if self.bad_length:
                data = data[:-1] if self.bad_length < 0 else data + b"x"
            return types.upload.File(type=types.storage.FileUnknown(), mtime=0, bytes=data)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1

    async def get_messages(self, peer, *, ids):
        self.refreshes += 1
        await asyncio.sleep(0.005)
        return self.fresh or media(len(self.payload), reference=b"fresh-reference")

    async def download_media(self, message, *, file, progress_callback=None):
        # Single-reader simulation uses the same per-request RTT and payload.
        assert self.active == 0, "Fallback started before all parallel reads stopped"
        assert sorted(self.borrowed) == sorted(self.returned), "A borrowed sender leaked into fallback"
        self.fallbacks += 1
        output = Path(file)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("wb") as target:
            for offset in range(0, len(self.payload), CHUNK_SIZE):
                await asyncio.sleep(self.delay)
                target.write(self.payload[offset:offset + CHUNK_SIZE])
                if progress_callback:
                    result = progress_callback(target.tell(), len(self.payload))
                    if hasattr(result, "__await__"):
                        await result
        return file

    async def send_message(self, *args, **kwargs):
        raise AssertionError("Downloader must not send messages")

    async def send_file(self, *args, **kwargs):
        raise AssertionError("Downloader must not upload")


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "file.bin.part"
        self.payload = bytes(range(251)) * ((CHUNK_SIZE * 8 + 37 + 250) // 251)
        self.payload = self.payload[:CHUNK_SIZE * 8 + 37]

    async def test_real_raw_schema_ordered_bytes_tail_and_aggregated_progress(self):
        client = ReadClient(self.payload, delay=0.001)
        progress = []
        result = await download_media(client, media(len(self.payload)), self.path,
                                      progress_callback=lambda current, total: progress.append((current, total)))
        self.assertEqual(result, str(self.path))
        self.assertEqual(self.path.read_bytes(), self.payload)
        self.assertEqual(self.path.stat().st_size, len(self.payload))
        counts = [current for current, _ in progress]
        self.assertEqual(counts, sorted(counts))
        self.assertCountEqual([count - previous for previous, count in zip([0] + counts, counts)],
                              [CHUNK_SIZE] * 8 + [37])
        self.assertTrue(all(total == len(self.payload) for _, total in progress))
        self.assertEqual(client.peak, 4)
        self.assertEqual((client.borrowed, client.returned), ([2], [2]))
        self.assertEqual(len({request.offset for _, request in client.requests}), 9)
        self.assertEqual(client.fallbacks, 0)

    async def test_async_progress_and_eight_lane_cap(self):
        client = ReadClient(self.payload, delay=0.001)
        seen = []
        async def progress(current, total):
            await asyncio.sleep(0)
            seen.append(current)
        await download_media(client, media(len(self.payload)), self.path,
                             connections=8, progress_callback=progress)
        self.assertGreater(client.peak, 1)
        self.assertLessEqual(client.peak, 8)
        self.assertEqual(seen[-1], len(self.payload))
        self.assertEqual(self.path.read_bytes(), self.payload)

    async def test_small_unknown_photo_single_lane_and_old_adapter_use_original(self):
        for kind in ("small", "unknown", "photo", "single", "old_adapter"):
            with self.subTest(kind=kind):
                client = ReadClient(b"small" if kind == "small" else self.payload)
                message = media(len(client.payload))
                if kind == "unknown":
                    message.document.size = None
                if kind == "photo":
                    message.media = types.MessageMediaPhoto(photo=types.PhotoEmpty(1))
                if kind == "old_adapter":
                    client._call = None
                await download_media(client, message, self.path, connections=1 if kind == "single" else 4)
                self.assertEqual(client.fallbacks, 1)
                self.assertEqual(client.requests, [])
                self.assertEqual(self.path.read_bytes(), client.payload)

    async def test_invalid_connection_counts_do_not_touch_network_or_disk(self):
        client = ReadClient(self.payload)
        for count in (0, 9, -1, True, 2.0, "4"):
            with self.assertRaises(DownloadError):
                await download_media(client, media(len(self.payload)), self.path, connections=count)
        self.assertFalse(self.path.exists())
        self.assertEqual(client.requests, [])

    async def test_short_oversized_and_wrong_result_never_return_success(self):
        for failure in ("short", "oversized", "type"):
            client = ReadClient(self.payload, delay=0.001)
            client.bad_length = -1 if failure == "short" else 1 if failure == "oversized" else 0
            client.bad_result = failure == "type"
            with self.assertRaises(DownloadError):
                await download_media(client, media(len(self.payload)), self.path)
            self.assertEqual(client.active, 0)
            self.assertEqual(client.returned, [2])

    async def test_flood_wait_escapes_unchanged_and_cancels_other_reads(self):
        client = ReadClient(self.payload)
        client.gates = {offset: asyncio.Event() for offset in (0, CHUNK_SIZE * 2, CHUNK_SIZE * 3)}
        original = errors.FloodWaitError(None, 77)
        client.errors[CHUNK_SIZE] = [original]
        with self.assertRaises(errors.FloodWaitError) as caught:
            await asyncio.wait_for(download_media(client, media(len(self.payload)), self.path), 1)
        self.assertIs(caught.exception, original)
        self.assertEqual(client.active, 0)
        self.assertEqual(client.returned, [2])
        self.assertGreater(client.cancelled, 0)
        self.assertEqual(client.fallbacks, 0)

    async def test_external_cancel_awaits_every_child_and_returns_sender(self):
        client = ReadClient(self.payload)
        client.gates = {i * CHUNK_SIZE: asyncio.Event() for i in range(4)}
        task = asyncio.create_task(download_media(client, media(len(self.payload)), self.path))
        deadline = asyncio.get_running_loop().time() + 2
        while client.active < 4 and not task.done() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0)
        self.assertEqual(client.active, 4)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((client.active, client.cancelled), (0, 4))
        self.assertEqual(client.returned, [2])

    async def test_file_reference_refresh_shared_across_lanes_and_no_mix(self):
        client = ReadClient(self.payload, delay=0.001)
        client.expired = True
        await download_media(client, media(len(self.payload)), self.path)
        self.assertEqual(client.refreshes, 1)
        self.assertEqual(self.path.read_bytes(), self.payload)
        self.assertTrue(any(request.location.file_reference == b"fresh-reference" for _, request in client.requests))

    async def test_refresh_rejects_changed_doc_size_peer_or_message(self):
        for changed in ("doc", "size", "peer", "message"):
            client = ReadClient(self.payload, delay=0.001)
            client.expired = True
            client.fresh = media(len(self.payload) + (changed == "size"),
                                 doc_id=101 if changed == "doc" else 100,
                                 peer_id=999 if changed == "peer" else 123,
                                 message_id=9 if changed == "message" else 7)
            with self.assertRaises(DownloadError):
                await download_media(client, media(len(self.payload)), self.path)
            self.assertEqual((client.active, client.refreshes), (0, 1))
            self.assertEqual(client.returned, [2])

    async def test_reference_refreshes_are_bounded_for_whole_file(self):
        client = ReadClient(self.payload, delay=0.001)
        client.expired = True
        client.fresh = media(len(self.payload), reference=b"old-reference")
        with self.assertRaises(DownloadError):
            await download_media(client, media(len(self.payload)), self.path)
        self.assertEqual(client.refreshes, 2)
        self.assertEqual(client.returned, [2])

    async def test_dc_migration_returns_old_and_new_borrow_exactly_once(self):
        client = ReadClient(self.payload, delay=0.001)
        client.required_dc = 3
        await download_media(client, media(len(self.payload)), self.path)
        self.assertEqual(self.path.read_bytes(), self.payload)
        self.assertEqual(client.borrowed, [2, 3])
        self.assertCountEqual(client.returned, [2, 3])

    async def test_dc_migration_count_is_bounded_for_whole_file(self):
        client = ReadClient(self.payload)
        async def endless_migration(sender, request):
            await asyncio.sleep(0)
            raise errors.FileMigrateError(request, sender.dc_id + 1)
        client._call = endless_migration
        with self.assertRaises(DownloadError):
            await download_media(client, media(len(self.payload)), self.path)
        self.assertLessEqual(len(client.borrowed), 3)
        self.assertCountEqual(client.returned, client.borrowed)
        self.assertEqual(client.fallbacks, 0)

    async def test_out_of_order_results_keep_bounded_window_and_sequential_file(self):
        client = ReadClient(self.payload, delay=0.001)
        first_gate = asyncio.Event()
        client.gates[0] = first_gate
        progress = []
        task = asyncio.create_task(download_media(client, media(len(self.payload)), self.path,
                                  progress_callback=lambda current, total: progress.append(current)))
        deadline = asyncio.get_running_loop().time() + 2
        while (len(client.requests) < 4 or client.active != 1) and not task.done():
            if asyncio.get_running_loop().time() > deadline:
                self.fail("Offline chunk reads did not reach the controlled gate")
            await asyncio.sleep(0.001)
        self.assertEqual(len(client.requests), 4)
        self.assertEqual(self.path.stat().st_size, 0)
        self.assertEqual(progress[-1], 3 * CHUNK_SIZE)
        first_gate.set()
        await task
        self.assertEqual(self.path.read_bytes(), self.payload)


class NativeSender:
    def __init__(self, owner, *args, **kwargs):
        self.owner = owner
        self.dc_id = None
        self.disconnects = 0
        self.disconnect_cancelled = False
        self.disconnected = False

    async def connect(self, connection):
        await self.owner.stage("connect")

    async def disconnect(self):
        self.disconnects += 1
        if self.owner.disconnect_hangs:
            try:
                await asyncio.sleep(3600)
            finally:
                self.disconnect_cancelled = True
        await asyncio.sleep(self.owner.disconnect_delay)
        self.disconnected = True


class NativePoolTests(unittest.IsolatedAsyncioTestCase):
    """Actual TelegramClient pool bookkeeping, with entirely fake transport."""

    def setUp(self):
        self.client = TelegramClient(MemorySession(), 12345, "a" * 32,
                                     receive_updates=False, request_retries=0, flood_sleep_threshold=0)
        self.original_query = self.client._init_request.query
        self.senders, self.requests = [], []
        self.fail_stage = self.block_stage = None
        self.disconnect_hangs = False
        self.disconnect_delay = 0.0
        self.original_error = ConnectionError("offline handshake failure")
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.payload = b"x" * (CHUNK_SIZE * 4 + 7)
        self.transport = ReadClient(self.payload)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "native.bin.part"

        async def dc(dc_id):
            return SimpleNamespace(id=dc_id, ip_address="127.0.0.1", port=443)
        self.client._get_dc = dc
        self.client._connection = lambda *args, **kwargs: object()
        self.client._call = self.call
        self.patch_sender = patch.object(downloads, "MTProtoSender", self.sender_factory)
        self.patch_sender.start()
        self.addCleanup(self.patch_sender.stop)

    def sender_factory(self, *args, **kwargs):
        sender = NativeSender(self, *args, **kwargs)
        self.senders.append(sender)
        return sender

    async def stage(self, name):
        if self.block_stage == name:
            self.entered.set()
            await self.release.wait()
        if self.fail_stage == name:
            raise self.original_error
        await asyncio.sleep(0)

    async def call(self, sender, request):
        self.requests.append(request)
        if isinstance(request, functions.auth.ExportAuthorizationRequest):
            self.assertIs(sender, self.client._sender)
            await self.stage("export")
            return types.auth.ExportedAuthorization(id=12, bytes=b"offline authorization")
        if isinstance(request, functions.InvokeWithLayerRequest):
            self.assertIsNot(request.query, self.client._init_request)
            self.assertIsInstance(request.query.query, functions.auth.ImportAuthorizationRequest)
            self.assertIs(self.client._init_request.query, self.original_query)
            await self.stage("import")
            return types.auth.Authorization(user=types.User(id=42, first_name="Offline"))
        return await self.transport._call(sender, request)

    async def test_real_client_pool_reuses_sender_and_balances_borrows_without_mutating_init(self):
        for _ in range(2):
            await download_media(self.client, media(len(self.payload)), self.path)
            self.assertEqual(self.path.read_bytes(), self.payload)
            state, sender = self.client._borrowed_senders[2]
            self.assertEqual(state._n, 0)
        self.assertEqual(len(self.senders), 1)
        self.assertEqual(sum(isinstance(req, functions.auth.ExportAuthorizationRequest) for req in self.requests), 1)
        self.assertEqual(self.senders[0].disconnects, 0)  # Cache owns the idle sender.
        self.assertIs(self.client._init_request.query, self.original_query)
        self.assertFalse(self.client._sender.is_connected())
        self.assertIsNone(self.client.session.filename if hasattr(self.client.session, "filename") else None)

    async def test_connect_export_and_import_failures_disconnect_unpublished_sender(self):
        self.disconnect_delay = 0.01  # Force cancellation to race a sibling's cleanup.
        for name in ("connect", "export", "import"):
            self.fail_stage = name
            with self.assertRaises(ConnectionError) as caught:
                await download_media(self.client, media(len(self.payload)), self.path)
            self.assertIs(caught.exception, self.original_error)
            self.assertEqual(self.client._borrowed_senders, {})
            self.assertEqual(self.senders[-1].disconnects, 1)
            self.assertTrue(all(sender.disconnected for sender in self.senders))
            self.assertIs(self.client._init_request.query, self.original_query)

    async def test_cancellation_during_each_handshake_stage_disconnects_and_awaits_children(self):
        self.disconnect_delay = 0.01
        for name in ("connect", "export", "import"):
            self.block_stage = name
            self.entered.clear()
            task = asyncio.create_task(download_media(self.client, media(len(self.payload)), self.path))
            await asyncio.wait_for(self.entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            self.assertEqual(self.client._borrowed_senders, {})
            self.assertEqual(self.senders[-1].disconnects, 1)
            self.assertTrue(self.senders[-1].disconnected)
            self.assertEqual(self.transport.active, 0)

    async def test_disconnected_cached_sender_cancel_never_increments_borrow_count(self):
        await download_media(self.client, media(len(self.payload)), self.path)
        state, sender = self.client._borrowed_senders[2]
        state._connected = False
        self.block_stage = "connect"
        task = asyncio.create_task(download_media(self.client, media(len(self.payload)), self.path))
        await asyncio.wait_for(self.entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(state._n, 0)
        self.assertFalse(state._connected)
        self.assertEqual(sender.disconnects, 1)

    async def test_failed_handshake_disconnect_has_bounded_wait_and_no_background_job(self):
        self.fail_stage, self.disconnect_hangs = "connect", True
        with patch.object(downloads, "SENDER_CLEANUP_TIMEOUT", 0.02), \
                self.assertLogs("tgmover.downloads", level="WARNING"):
            with self.assertRaises(ConnectionError):
                await asyncio.wait_for(download_media(self.client, media(len(self.payload)), self.path), 1)
        self.assertEqual(self.client._borrowed_senders, {})
        self.assertEqual(self.senders[-1].disconnects, 1)
        self.assertTrue(self.senders[-1].disconnect_cancelled)

class DownloadResourceTests(unittest.IsolatedAsyncioTestCase):
    setUp = DownloadTests.setUp

    async def test_reference_refresh_flood_is_not_retried_by_other_lanes(self):
        client = ReadClient(self.payload, delay=0.001)
        client.expired = True
        original = errors.FloodWaitError(None, 99)
        async def limited_refresh(peer, *, ids):
            client.refreshes += 1
            await asyncio.sleep(0)
            raise original
        client.get_messages = limited_refresh
        with self.assertRaises(errors.FloodWaitError) as caught:
            await download_media(client, media(len(self.payload)), self.path)
        self.assertIs(caught.exception, original)
        self.assertEqual(client.refreshes, 1)
        self.assertEqual(client.active, 0)
        self.assertEqual(client.returned, [2])

    async def test_primary_dc_is_never_returned_or_disconnected(self):
        client = ReadClient(self.payload)
        await download_media(client, media(len(self.payload), dc_id=1), self.path)
        self.assertEqual(client.borrowed, [])
        self.assertEqual(client.returned, [])
        self.assertEqual(self.path.read_bytes(), self.payload)

    async def test_cdn_fallback_only_after_all_tasks_and_senders_closed_once(self):
        client = ReadClient(self.payload, delay=0.001)
        client.cdn = True
        await download_media(client, media(len(self.payload)), self.path)
        self.assertEqual(client.fallbacks, 1)
        self.assertEqual(client.active, 0)
        self.assertEqual(self.path.read_bytes(), self.payload)
        self.assertEqual(client.returned, [2])

    async def test_cdn_does_not_start_fallback_when_sender_return_fails(self):
        client = ReadClient(self.payload, delay=0.001)
        client.cdn, client.close_fail_dc = True, 2
        with self.assertRaises(DownloadError):
            await download_media(client, media(len(self.payload)), self.path)
        self.assertEqual(client.active, 0)
        self.assertEqual(client.returned, [2])
        self.assertEqual(client.fallbacks, 0)

    async def test_callback_error_stops_read_pipeline_and_releases_all_senders(self):
        client = ReadClient(self.payload, delay=0.001)
        def bad_progress(current, total):
            raise RuntimeError("offline callback failure")
        with self.assertRaisesRegex(RuntimeError, "callback"):
            await download_media(client, media(len(self.payload)), self.path, progress_callback=bad_progress)
        self.assertEqual(client.active, 0)
        self.assertEqual(client.returned, [2])

    async def test_simulated_rtt_pipeline_preserves_bytes_and_overlaps_read_waits(self):
        measured = []
        for connections in (1, 4):
            client = ReadClient(self.payload, delay=0.02)
            started = time.perf_counter()
            await download_media(client, media(len(self.payload)), self.path, connections=connections)
            measured.append(time.perf_counter() - started)
            self.assertEqual(self.path.read_bytes(), self.payload)
            self.assertEqual(client.active, 0)
        # This only verifies artificial RTT overlap on the local machine. It
        # is deliberately not a prediction of Telegram/network throughput.
        self.assertGreater(measured[0], measured[1] * 1.3)


if __name__ == "__main__":
    unittest.main()
