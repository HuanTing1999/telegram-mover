"""Offline native-AES integrity and archive-before-forward safety contracts."""

import asyncio
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from telethon import errors
from telethon.crypto import aes

import test_engine as fixtures
from tgmover.acceleration import backend_info, benchmark_current_backend
from tgmover.engine import SyncError


class AccelerationTests(unittest.TestCase):
    def test_installed_accelerator_is_actually_selected_by_telethon(self):
        info = backend_info()
        self.assertEqual(info["backend"], "cryptg")
        self.assertTrue(info["accelerated"])
        self.assertEqual(info["version"], "0.6.0")
        self.assertIn("实际传输速度", info["message"])

    def test_real_native_aes_matches_pure_python_and_roundtrips(self):
        native = aes.cryptg
        self.assertIsNotNone(native)
        key, iv = bytes(range(32)), bytes(range(32, 64))
        for size in (16, 64, 4096):
            with self.subTest(size=size):
                data = (bytes(range(256)) * ((size + 255) // 256))[:size]
                encrypted = native.encrypt_ige(data, key, iv)
                self.assertEqual(native.decrypt_ige(encrypted, key, iv), data)
                with patch.object(aes, "cryptg", None), patch.object(aes.libssl, "encrypt_ige", None), \
                        patch.object(aes.libssl, "decrypt_ige", None):
                    self.assertEqual(aes.AES.encrypt_ige(data, key, iv), encrypted)
                    self.assertEqual(aes.AES.decrypt_ige(encrypted, key, iv), data)

    def test_detection_reports_fallback_when_installed_extension_is_not_active(self):
        with patch.object(aes, "cryptg", None), patch.object(aes.libssl, "encrypt_ige", None), \
                patch.object(aes.libssl, "decrypt_ige", None):
            info = backend_info()
        self.assertEqual(info["backend"], "pyaes")
        self.assertFalse(info["accelerated"])
        self.assertIn("纯 Python", info["message"])

    def test_benchmark_only_reports_local_integrity_and_measured_cpu_work(self):
        with patch("socket.create_connection", side_effect=AssertionError("Benchmark cannot use a network")):
            measured = benchmark_current_backend(size_bytes=4096, repeats=2)
        self.assertTrue(measured["local_only"])
        self.assertTrue(measured["roundtrip_correct"])
        self.assertEqual((measured["size_bytes"], measured["repeats"]), (4096, 2))
        self.assertGreater(measured["decrypt_mib_per_second"], 0)
        self.assertNotIn("download_speed", measured)


class DownloadPerformanceTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.EngineTests.setUp
    mover = fixtures.EngineTests.mover

    async def test_all_album_archives_are_verified_before_one_original_forward(self):
        mover = await self.mover(mode="download_then_forward", sync_comments=False)
        batch = [fixtures.message(1, media=True, grouped_id=77), fixtures.message(2, media=True, grouped_id=77)]
        original = self.client.forward_messages

        async def checked_forward(destination, messages, **kwargs):
            for item in messages:
                record = self.state.download(mover.route_id, "post", item.id)
                self.assertIsNotNone(record)
                self.assertEqual((Path(record["path"]).read_bytes(), record["size"]), (b"DATA", 4))
            return await original(destination, messages, **kwargs)

        self.client.forward_messages = checked_forward
        await mover.process(batch, "post", "download_then_forward")
        self.assertEqual([call[0] for call in self.client.calls if call[0] in {"download", "forward", "file"}],
                         ["download", "download", "forward"])
        self.assertEqual(self.client.sends()[0][2], [1, 2])
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 2)

    async def test_verified_archives_are_reused_after_definite_forward_rejection(self):
        mover = await self.mover(mode="download_then_forward", sync_comments=False)
        batch = [fixtures.message(1, media=True, grouped_id=77), fixtures.message(2, media=True, grouped_id=77)]
        self.client.send_failure = errors.ChatWriteForbiddenError(None)
        with self.assertRaises(SyncError):
            await mover.process(batch, "post", "download_then_forward")
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)
        self.assertEqual(self.state.pending(mover.route_id), [])
        self.client.send_failure = None
        await mover.process(batch, "post", "download_then_forward")
        self.assertEqual(sum(call[0] == "download" for call in self.client.calls), 2)
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 2)

    async def test_archive_failure_never_forwards_partial_album_or_spends_send_budget(self):
        mover = await self.mover(mode="download_then_forward", sync_comments=False)
        batch = [fixtures.message(1, media=True, grouped_id=77), fixtures.message(2, media=True, grouped_id=77)]
        original = self.client.download_media

        async def failing_download(item, **kwargs):
            if item.id == 2:
                raise ConnectionError("offline synthetic download failure")
            return await original(item, **kwargs)

        self.client.download_media = failing_download
        with self.assertRaises(ConnectionError):
            await mover.process(batch, "post", "download_then_forward")
        self.assertEqual(self.client.sends(), [])
        self.assertEqual(self.state.pending(mover.route_id), [])
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)
        self.assertEqual(self.state.conn.execute("SELECT COUNT(*) FROM limit_events").fetchone()[0], 0)
        self.assertIsNotNone(self.state.download(mover.route_id, "post", 1))
        self.client.download_media = original
        await mover.process(batch, "post", "download_then_forward")
        self.assertEqual(sum(call[0] == "download" and call[1] == 1 for call in self.client.calls), 1)

    async def test_read_spacing_is_per_download_not_per_transfer_progress_chunk(self):
        mover = await self.mover(sync_comments=False)
        item = fixtures.message(1, media=True)
        item.document.size = 64

        async def chunked_download(_item, *, file, progress_callback):
            with Path(file).open("wb") as target:
                for chunk in range(16):
                    target.write(b"DATA")
                    progress_callback((chunk + 1) * 4, 64)
                    await asyncio.sleep(0)
            return file

        self.client.download_media = chunked_download
        mover.limiter.read_wait = AsyncMock(wraps=mover.limiter.read_wait)
        result = await mover.download(item, "post")
        self.assertEqual(result.read_bytes(), b"DATA" * 16)
        self.assertEqual(mover.limiter.read_wait.await_count, 1)
        self.assertEqual(self.state.conn.execute("SELECT COUNT(*) FROM limit_events").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
