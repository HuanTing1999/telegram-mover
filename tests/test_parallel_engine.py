"""Run actual chunk-pipeline downloads inside the ordered mover transaction."""
from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from telethon import errors

import test_downloads as download_fixture
import test_engine as engine_fixture
from tgmover.engine import SyncError


class PipelineEngineTests(unittest.IsolatedAsyncioTestCase):
    setUp = engine_fixture.EngineTests.setUp
    mover = engine_fixture.EngineTests.mover

    def wire_pipeline(self, *, album=False):
        self.payload = (bytes(range(251)) * 18000)[:4 * 1024 * 1024 + 37]
        self.reader = download_fixture.ReadClient(self.payload, delay=0.001)
        self.client.session, self.client._sender = self.reader.session, self.reader._sender
        for name in ("_call", "_borrow_exported_sender", "_return_exported_sender", "download_media"):
            setattr(self.client, name, getattr(self.reader, name))
        items = [engine_fixture.message(1, media=True, grouped_id=77 if album else None)]
        if album:
            items.append(engine_fixture.message(2, media=True, grouped_id=77))
        for item in items:
            item.document.size = len(self.payload)
        self.client.add(1, items)
        return items

    async def test_real_pipeline_archives_entire_album_before_one_ordered_forward(self):
        self.wire_pipeline(album=True)
        original = self.client.forward_messages

        async def check_archive_then_forward(destination, batch, **kwargs):
            self.assertEqual([item.id for item in batch], [1, 2])
            self.assertEqual(self.reader.active, 0)
            for item in batch:
                row = self.state.download(mover.route_id, "post", item.id)
                self.assertEqual(Path(row["path"]).read_bytes(), self.payload)
                self.assertFalse(Path(row["path"] + ".part").exists())
            return await original(destination, batch, **kwargs)

        self.client.forward_messages = check_archive_then_forward
        mover = await self.mover(mode="download_then_forward", sync_comments=False)
        await mover.run_once()
        self.assertEqual(self.reader.peak, 4)
        self.assertEqual(self.reader.fallbacks, 0)
        self.assertEqual(len(self.client.sends()), 1)
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 2)
        self.assertEqual(self.state.pending(mover.route_id), [])
        requests = len(self.reader.requests)
        await mover.run_once()
        self.assertEqual(len(self.reader.requests), requests)
        self.assertEqual(len(self.client.sends()), 1)

    async def test_one_channel_keeps_sequential_download_and_eight_is_bounded(self):
        self.wire_pipeline()
        mover = await self.mover(mode="download_only", sync_comments=False, download_connections=1)
        await mover.run_once()
        self.assertEqual(self.reader.fallbacks, 1)
        self.assertEqual(self.reader.requests, [])
        mover = await self.mover(mode="download_then_forward", sync_comments=False, download_connections=8)
        await mover.run_once()
        self.assertEqual(self.reader.peak, 8)
        self.assertEqual(len(self.client.sends()), 1)

    async def test_hung_chunk_stops_all_readers_without_sending_or_registering_partial_file(self):
        self.wire_pipeline()
        gate = asyncio.Event()
        self.reader.gates[0] = gate
        mover = await self.mover(mode="download_then_forward", sync_comments=False)
        with patch("tgmover.engine.DOWNLOAD_IDLE_SECONDS", 0.07):
            with self.assertRaises(SyncError):
                await mover.run_once()
        self.assertEqual(self.reader.active, 0)
        self.assertTrue(self.reader.cancelled)
        self.assertIsNone(self.state.download(mover.route_id, "post", 1))
        self.assertEqual(self.client.sends(), [])
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)
        self.assertEqual(self.state.pending(mover.route_id), [])
        gate.set()
        await mover.run_once()
        row = self.state.download(mover.route_id, "post", 1)
        self.assertEqual(Path(row["path"]).read_bytes(), self.payload)
        self.assertEqual(len(self.client.sends()), 1)

    async def test_direct_forward_never_downloads_or_creates_media_file(self):
        self.wire_pipeline()
        mover = await self.mover(mode="forward", sync_comments=False)
        await mover.run_once()
        self.assertEqual((self.reader.requests, self.reader.fallbacks), ([], 0))
        self.assertIsNone(self.state.download(mover.route_id, "post", 1))
        self.assertFalse(mover.config.download_dir.exists())
        self.assertEqual(len(self.client.sends()), 1)

    async def test_raw_read_flood_keeps_no_pending_and_no_forward(self):
        self.wire_pipeline()
        self.reader.errors[0] = [errors.FloodWaitError(None, capture=3)]
        mover = await self.mover(mode="download_then_forward", sync_comments=False)
        with self.assertRaises(SyncError):
            await mover.run_once()
        self.assertEqual(self.reader.active, 0)
        self.assertIsNone(self.state.download(mover.route_id, "post", 1))
        self.assertEqual(self.client.sends(), [])
        self.assertEqual(self.state.pending(mover.route_id), [])
        account = self.state.conn.execute("SELECT global_cooldown_until FROM limit_accounts WHERE account_id=42").fetchone()
        self.assertGreater(account[0], 0)


if __name__ == "__main__":
    unittest.main()
