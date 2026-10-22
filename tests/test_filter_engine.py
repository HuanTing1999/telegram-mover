"""Offline traceable filtering and safe-stop integration with actual SQLite."""

from dataclasses import replace
import sqlite3
import unittest

from telethon import types, utils

import test_engine as fixtures
from tgmover.engine import Mover, UserStopped
from tgmover.filters import FilterSettings


class FilterEngineTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the real Telethon Message/FakeClient and isolated SQLite fixture,
    # without inheriting and rediscovering the base engine's own test methods.
    setUp = fixtures.EngineTests.setUp
    mover = fixtures.EngineTests.mover

    def attempted(self):
        return self.state.conn.execute("SELECT COALESCE(SUM(cost),0) FROM limit_events").fetchone()[0]

    async def test_filtered_album_is_auditable_without_download_send_or_budget(self):
        mover = await self.mover(sync_comments=False)
        batch = [fixtures.message(1, media=True, grouped_id=77),
                 fixtures.message(2, text="商务合作", media=True, grouped_id=77)]
        await mover.process(batch, "post", "reupload")
        self.assertEqual(self.client.sends(), [])
        self.assertFalse(any(call[0] == "download" for call in self.client.calls))
        self.assertEqual(self.attempted(), 0)
        self.assertEqual(self.state.pending(mover.route_id), [])
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 2)
        rows = self.state.messages(mover.route_id, "post")
        self.assertEqual([row["status"] for row in rows], ["filtered_ad", "filtered_ad"])
        self.assertEqual([row["target_id"] for row in rows], [None, None])
        for row in rows:
            audit = row["metadata"]["ad_filter"]
            self.assertEqual(audit["album_ids"], [1, 2])
            self.assertEqual(audit["matched_ids"], [2])
            self.assertTrue(any("商务合作" in reason for reason in audit["reasons"]))
        self.assertEqual(mover.counts, {"processed": 2, "sent": 0, "filtered": 2, "files": 0})

    async def test_filtered_comment_advances_only_its_thread_cursor(self):
        mover = await self.mover()
        context = {"source_post_id": 1, "source_root_id": 10, "target_root_id": 10101}
        batch = [fixtures.message(11, 3, text="sponsored post", media=True, root=10, grouped_id=88),
                 fixtures.message(12, 3, media=True, root=10, grouped_id=88)]
        await mover.process(batch, "comment", "reupload", context=context)
        self.assertEqual(self.state.cursor(mover.route_id, "comment", 10), 12)
        self.assertEqual(self.state.cursor(mover.route_id, "comment", 20), 0)
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)
        self.assertEqual(self.client.sends(), [])
        self.assertEqual(self.attempted(), 0)
        for row in self.state.messages(mover.route_id, "comment"):
            self.assertEqual((row["status"], row["source_post_id"], row["target_root_id"]),
                             ("filtered_ad", 1, 10101))
            self.assertEqual(row["metadata"]["ad_filter"]["album_ids"], [11, 12])

    async def test_filtered_batch_rows_and_cursor_roll_back_atomically(self):
        mover = await self.mover(sync_comments=False)
        self.state.conn.execute("""CREATE TRIGGER reject_second_filtered
            BEFORE INSERT ON messages WHEN NEW.source_id=2
            BEGIN SELECT RAISE(ABORT, 'test transaction rollback'); END""")
        batch = [fixtures.message(1, text="广告合作", grouped_id=77), fixtures.message(2, grouped_id=77)]
        with self.assertRaises(sqlite3.IntegrityError):
            await mover.process(batch, "post", "reupload")
        self.assertEqual(self.state.messages(mover.route_id, "post"), [])
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)
        self.assertEqual(self.client.sends(), [])
        self.assertEqual(self.attempted(), 0)

    async def test_filtered_post_never_resolves_or_copies_its_comments(self):
        self.client.add(1, [fixtures.message(1, text="广告合作", media=True)])
        self.client.link_post(1, 10)
        self.client.add(3, [fixtures.message(11, 3, text="a regular comment", root=10)], root=10)
        mover = await self.mover()
        await mover.run_once()
        self.assertFalse(any(call == ("request", "GetDiscussionMessageRequest") for call in self.client.calls))
        self.assertEqual(self.state.threads(mover.route_id), [])
        self.assertEqual(self.state.messages(mover.route_id, "comment"), [])
        self.assertEqual(self.client.sends(), [])

    async def test_ad_between_regular_posts_advances_incrementally_without_repeat(self):
        self.client.add(1, [fixtures.message(1, text="first regular post"),
                            fixtures.message(2, text="广告投放", media=True),
                            fixtures.message(3, text="third regular post")])
        mover = await self.mover(sync_comments=False)
        await mover.run_once()
        await mover.run_once()
        self.assertEqual([call[2] for call in self.client.sends()], ["first regular post", "third regular post"])
        self.assertEqual(self.attempted(), 2)
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 3)
        self.assertEqual(self.state.get_message(mover.route_id, "post", 2)["status"], "filtered_ad")
        self.assertFalse(any(call[0] == "download" for call in self.client.calls))
        self.client.add(1, [fixtures.message(4, text="fourth regular post")])
        await mover.run_once()
        self.assertEqual(len(self.client.sends()), 3)
        self.assertEqual(self.attempted(), 3)

    async def test_download_only_keeps_ad_backups_by_default(self):
        self.client.add(1, [fixtures.message(1, text="广告合作", media=True)])
        mover = await self.mover(mode="download_only", comment_mode="download_only", target=None,
                                 sync_comments=False)
        await mover.run_once()
        self.assertIsNotNone(self.state.download(mover.route_id, "post", 1))
        self.assertEqual(self.state.get_message(mover.route_id, "post", 1)["status"], "done")
        self.assertEqual(self.client.sends(), [])
        self.assertEqual(self.attempted(), 0)

    async def test_download_only_can_explicitly_filter_before_archiving(self):
        self.client.add(1, [fixtures.message(1, text="广告合作", media=True)])
        mover = await self.mover(mode="download_only", comment_mode="download_only", target=None,
                                 sync_comments=False, filters=FilterSettings(apply_to_downloads=True))
        await mover.run_once()
        self.assertIsNone(self.state.download(mover.route_id, "post", 1))
        self.assertEqual(self.state.get_message(mover.route_id, "post", 1)["status"], "filtered_ad")
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 1)
        self.assertEqual(self.attempted(), 0)

    async def test_filter_changes_reuse_route_and_do_not_resend_historical_filtered_rows(self):
        self.client.add(1, [fixtures.message(1, text="广告合作")])
        original = await self.mover(sync_comments=False)
        await original.run_once()
        changed = await self.mover(sync_comments=False, filters=FilterSettings(enabled=False))
        self.assertEqual(original.route_id, changed.route_id)
        await changed.run_once()
        self.assertEqual(self.client.sends(), [])
        self.assertEqual(self.state.get_message(changed.route_id, "post", 1)["status"], "filtered_ad")
        self.client.add(1, [fixtures.message(2, text="广告合作")])
        await changed.run_once()
        self.assertEqual(len(self.client.sends()), 1)
        self.assertEqual(self.state.cursor(changed.route_id, "post"), 2)

    async def test_whitelisted_album_is_sent_and_manual_image_block_has_priority(self):
        batch = [fixtures.message(1, text="广告合作", media=True, grouped_id=77),
                 fixtures.message(2, text="verified editorial", media=True, grouped_id=77)]
        mover = await self.mover(sync_comments=False, filters=FilterSettings(allowed_keywords=("verified editorial",)))
        await mover.process(batch, "post", "reupload")
        self.assertEqual(len(self.client.sends()), 1)
        self.assertEqual(self.attempted(), 2)
        self.assertEqual([row["status"] for row in self.state.messages(mover.route_id, "post")], ["done", "done"])

        pictures = [fixtures.message(3, text="verified editorial", grouped_id=88),
                    fixtures.message(4, text="", grouped_id=88)]
        for item in pictures:
            item.media = types.MessageMediaPhoto(photo=types.Photo(
                id=item.id * 100, access_hash=999, file_reference=b"photo",
                date=fixtures.OLD, sizes=[], dc_id=1))
        key = f"{utils.get_peer_id(pictures[0].peer_id)}:{pictures[0].id}"
        changed = await self.mover(sync_comments=False, filters=FilterSettings(
            allowed_keywords=("verified editorial",), blocked_message_keys=(key,)))
        before = len(self.client.calls)
        await changed.process(pictures, "post", "reupload")
        self.assertFalse(any(call[0] == "download" for call in self.client.calls[before:]))
        self.assertEqual(len(self.client.sends()), 1)
        self.assertEqual(self.attempted(), 2)
        for identifier in (3, 4):
            row = self.state.get_message(changed.route_id, "post", identifier)
            self.assertEqual(row["status"], "filtered_ad")
            self.assertEqual(row["metadata"]["ad_filter"]["album_ids"], [3, 4])
            self.assertTrue(any("手动屏蔽" in reason for reason in row["metadata"]["ad_filter"]["reasons"]))

    async def test_stop_after_reservation_before_rpc_creates_no_pending(self):
        mover = await self.mover(sync_comments=False)
        stopped = False
        reserve = mover.limiter.reserve

        async def stop_after_reserving(peer_id, cost):
            nonlocal stopped
            await reserve(peer_id, cost)
            stopped = True

        mover.limiter.reserve = stop_after_reserving
        mover.should_stop = lambda: stopped
        with self.assertRaises(UserStopped):
            await mover.process([fixtures.message(1)], "post", "reupload")
        self.assertEqual(self.client.sends(), [])
        self.assertEqual(self.state.pending(mover.route_id), [])
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)

    async def test_stop_after_success_commits_mapping_and_restart_does_not_duplicate(self):
        self.client.add(1, [fixtures.message(1, text="ordinary post")])
        stopped = False

        def on_progress(_):
            nonlocal stopped
            stopped = True

        mover = Mover(self.client, replace(self.config, sync_comments=False), self.state,
                      should_stop=lambda: stopped, on_progress=on_progress)
        await mover.prepare()
        with self.assertRaises(UserStopped):
            await mover.run_once()
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 1)
        self.assertEqual(self.state.get_message(mover.route_id, "post", 1)["target_id"], 101)
        self.assertEqual(self.state.pending(mover.route_id), [])
        replacement = await self.mover(sync_comments=False)
        await replacement.run_once()
        self.assertEqual(replacement.route_id, mover.route_id)
        self.assertEqual(len(self.client.sends()), 1)
        self.assertEqual(self.attempted(), 1)


if __name__ == "__main__":
    unittest.main()
