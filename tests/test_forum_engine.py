"""Forum scopes retain ordered albums, durable progress and uncertain writes."""
from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from telethon import errors, functions

import test_engine
from test_engine import FakeClient, message
from test_forum_rpc_contract import forum, topic
from tgmover.engine import Mover, SyncError, UserStopped
from tgmover.forum import ForumError


class ForumFixtureClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.source, self.target = forum(1), forum(2)
        self.entities = {"source": self.source, "target": self.target}
        self.links = {}
        self.available = {1, 21, 22}

    async def __call__(self, request):
        if isinstance(request, functions.messages.GetForumTopicsByIDRequest):
            self.calls.append(("topic", request.peer.id, list(request.topics)))
            return SimpleNamespace(topics=[topic(i) for i in request.topics if i in self.available])
        return await super().__call__(request)

    def add_topic_message(self, item, root):
        self.add(1, [item], root=root)
        self.add(1, [item])


class ForumEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        test_engine.EngineTests.setUp(self)
        self.client = ForumFixtureClient()
        self.config = replace(self.config, sync_comments=False)

    async def mover(self, **changes):
        result = Mover(self.client, replace(self.config, **changes), self.state)
        await result.prepare()
        return result

    async def test_whole_general_and_specific_topic_have_separate_incremental_scopes(self):
        self.client.add_topic_message(message(8), 1)
        self.client.add_topic_message(message(23, root=21), 21)
        self.client.add_topic_message(message(98, root=22), 22)
        routes = []
        for scope, expected in ((0, [8, 23, 98]), (1, [8]), (21, [23])):
            with self.subTest(scope=scope):
                mover = await self.mover(source_topic_id=scope)
                routes.append(mover.route_id)
                await mover.run_once()
                saved = self.state.messages(mover.route_id, "post")
                self.assertEqual([row["source_id"] for row in saved], expected)
                self.assertEqual(self.state.cursor(mover.route_id, "post"), max(expected))
                previous_sends = len(self.client.sends())
                await mover.run_once()
                self.assertEqual(len(self.client.sends()), previous_sends)
        self.assertEqual(len(set(routes)), 3)

    async def test_target_general_and_default_reuse_route_and_send_without_reply_to(self):
        self.client.add_topic_message(message(23, root=21), 21)
        default = await self.mover(source_topic_id=21)
        await default.run_once()
        general = await self.mover(source_topic_id=21, target_topic_id=1)
        self.assertEqual(default.route_id, general.route_id)
        await general.run_once()
        self.assertEqual(len(self.client.sends()), 1)
        self.assertIsNone(self.client.sends()[0][3]["reply_to"])

    async def test_selected_target_topic_keeps_album_whole_and_maps_both_members(self):
        self.client.add_topic_message(message(31, media=True, grouped_id=7, root=21), 21)
        self.client.add_topic_message(message(32, media=True, grouped_id=7, root=21), 21)
        mover = await self.mover(source_topic_id=21, target_topic_id=22)
        await mover.run_once()
        sends = self.client.sends()
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][0], "file")
        self.assertEqual(len(sends[0][2]), 2)
        self.assertEqual(sends[0][3]["reply_to"], 22)
        self.assertFalse(sends[0][3]["allow_cache"])
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 32)
        self.assertEqual(len(self.state.messages(mover.route_id, "post")), 2)
        self.assertEqual(self.state.pending(mover.route_id), [])

    async def test_download_then_forward_to_topic_archives_before_raw_forward(self):
        self.client.add_topic_message(message(31, media=True, grouped_id=7, root=21), 21)
        self.client.add_topic_message(message(32, media=True, grouped_id=7, root=21), 21)
        mover = await self.mover(source_topic_id=21, target_topic_id=22, mode="download_then_forward")

        async def forward(client, destination, batch, source, topic_id):
            self.assertIs(client, self.client)
            self.assertEqual(topic_id, 22)
            self.assertEqual([item.id for item in batch], [31, 32])
            self.assertEqual(len(self.state.pending(mover.route_id)), 1)
            for item in batch:
                self.assertIsNotNone(self.state.download(mover.route_id, "post", item.id))
            return [client._sent(destination, reply_to=topic_id) for _ in batch]

        with patch("tgmover.engine.forward_to_topic", side_effect=forward) as raw:
            await mover.run_once()
        self.assertEqual(raw.call_count, 1)
        self.assertEqual(self.client.sends(), [])
        self.assertEqual(mover.counts["sent"], 2)
        self.assertEqual(self.state.pending(mover.route_id), [])

    async def test_uncertain_topic_forward_retains_pending_without_advancing(self):
        self.client.add_topic_message(message(23, root=21), 21)
        mover = await self.mover(source_topic_id=21, target_topic_id=22, mode="forward")
        with patch("tgmover.engine.forward_to_topic", side_effect=ForumError("missing random ID mapping")) as raw:
            with self.assertRaises(SyncError):
                await mover.run_once()
        self.assertEqual(raw.call_count, 1)
        self.assertEqual(len(self.state.pending(mover.route_id)), 1)
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)
        self.assertEqual(self.state.messages(mover.route_id, "post"), [])

    async def test_wrong_reupload_topic_retains_pending_instead_of_success(self):
        self.client.add_topic_message(message(23, root=21), 21)
        self.client.bad_reply = True
        mover = await self.mover(source_topic_id=21, target_topic_id=22)
        with self.assertRaisesRegex(SyncError, "目标话题响应不匹配"):
            await mover.run_once()
        self.assertEqual(len(self.state.pending(mover.route_id)), 1)
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)

    async def test_missing_or_nonforum_topic_stops_before_any_download_or_send(self):
        self.client.add(1, [message(31, media=True)])
        with self.assertRaisesRegex(SyncError, "源话题不可用"):
            await self.mover(source_topic_id=999)
        self.client.target.forum = False
        with self.assertRaisesRegex(SyncError, "目标选择了话题"):
            await self.mover(target_topic_id=21)
        self.assertEqual(self.client.sends(), [])
        self.assertFalse(any(call[0] == "download" for call in self.client.calls))
        self.assertEqual(self.state.conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0], 0)

    async def test_same_group_requires_nonoverlapping_explicit_topics(self):
        self.client.entities["target"] = self.client.source
        for source, target in ((0, 0), (0, 21), (21, 21), (1, 0), (1, 1)):
            with self.subTest(source=source, target=target), self.assertRaisesRegex(SyncError, "同一范围"):
                await self.mover(source_topic_id=source, target_topic_id=target)

    async def test_read_only_topic_gate_can_stop_during_server_flood_wait(self):
        stopped, requested = asyncio.Event(), asyncio.Event()
        mover = Mover(self.client, replace(self.config, max_flood_wait=300), None,
                      should_stop=stopped.is_set)
        calls = []

        async def limited():
            calls.append(True)
            requested.set()
            raise errors.FloodWaitError(None, capture=5)

        task = asyncio.create_task(mover.read(limited))
        await asyncio.wait_for(requested.wait(), 0.5)
        stopped.set()
        with self.assertRaises(UserStopped):
            await asyncio.wait_for(task, 0.5)
        self.assertEqual(len(calls), 1)
        for source, target in ((21, 22), (21, 0), (21, 1), (1, 21)):
            with self.subTest(source=source, target=target):
                await self.mover(source_topic_id=source, target_topic_id=target)


if __name__ == "__main__":
    unittest.main()
