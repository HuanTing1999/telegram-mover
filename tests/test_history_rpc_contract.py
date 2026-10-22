"""Real Telethon history/reply pagination; only the RPC boundary is scripted."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telethon import TelegramClient, functions, types
from telethon.sessions import MemorySession

from test_engine import channel, message
from tgmover.config import Config
from tgmover.engine import Mover
from tgmover.limits import LimitSettings
from tgmover.state import State


class ScriptedHistoryClient(TelegramClient):
    def __init__(self, responses, *, peer=1):
        super().__init__(MemorySession(), 12345, "a" * 32, request_retries=0)
        self.responses = list(responses)
        self.requests = []
        self.peer = peer

    async def connect(self):
        raise AssertionError("History contract tests must not connect")

    async def get_input_entity(self, entity):
        return types.InputPeerChannel(entity.id, entity.access_hash)

    async def _call(self, _sender, request, **kwargs):
        if not isinstance(request, (functions.messages.GetHistoryRequest, functions.messages.GetRepliesRequest)):
            raise AssertionError(f"Unexpected offline RPC: {type(request).__name__}")
        # Telethon mutates its paging request in place, so snapshot every call.
        self.requests.append({"type": type(request), "offset_id": request.offset_id,
                              "add_offset": request.add_offset, "limit": request.limit,
                              "msg_id": getattr(request, "msg_id", None),
                              "peer": request.peer.channel_id})
        if not self.responses:
            raise AssertionError("The iterator issued an unexpected extra page")
        ids, total, grouped = self.responses.pop(0)
        return types.messages.MessagesSlice(
            count=total,
            messages=[message(identifier, peer=self.peer, grouped_id=grouped.get(identifier)) for identifier in ids],
            users=[], chats=[channel(self.peer, broadcast=self.peer == 1)], topics=[],
        )


class HistoryRpcContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.state = State(base / "state.sqlite3")
        self.addCleanup(self.state.close)
        self.config = Config(
            api_id=12345, api_hash="a" * 32, source="source", target="target",
            mode="reupload", comment_mode="reupload", sync_comments=True,
            session=base / "session", database=base / "state.sqlite3", download_dir=base / "downloads",
            start_id=0, poll_interval=0, settle_seconds=2, max_flood_wait=300,
            send_delay=0, limits=LimitSettings(enabled=False),
        )

    async def collect(self, client, *, minimum, root=None):
        mover = Mover(client, self.config, self.state)
        peer = channel(client.peer, broadcast=client.peer == 1)

        async def no_wait(_seconds):
            return None

        # Exercise RequestIter's real paging logic without spending a second
        # between each fake page. This does not replace iter_messages/get_messages.
        with patch("telethon.requestiter.asyncio.sleep", side_effect=no_wait):
            return [[item.id for item in batch] async for batch in mover.scan(peer, minimum=minimum, root=root)]

    async def test_reverse_history_is_exclusive_and_continues_after_partial_page(self):
        client = ScriptedHistoryClient([
            ([237], 237, {}),
            (list(range(225, 125, -1)), 237, {}),
            (list(range(237, 225, -1)), 237, {}),
            ([], 237, {}),
        ])

        result = await self.collect(client, minimum=125)

        self.assertEqual([item for batch in result for item in batch], list(range(126, 238)))
        self.assertEqual([request["offset_id"] for request in client.requests], [0, 126, 226, 238])
        self.assertEqual([request["add_offset"] for request in client.requests], [0, -100, -100, -100])
        self.assertTrue(all(request["type"] is functions.messages.GetHistoryRequest for request in client.requests))
        self.assertEqual(client.responses, [])

    async def test_comment_thread_uses_get_replies_and_the_same_exclusive_cursor(self):
        client = ScriptedHistoryClient([
            ([237], 117, {}),
            (list(range(220, 120, -1)), 117, {}),
            (list(range(237, 220, -1)), 117, {}),
            ([], 117, {}),
        ], peer=3)

        result = await self.collect(client, minimum=120, root=10)

        self.assertEqual([item for batch in result for item in batch], list(range(121, 238)))
        self.assertTrue(all(request["type"] is functions.messages.GetRepliesRequest for request in client.requests))
        self.assertTrue(all(request["peer"] == 3 and request["msg_id"] == 10 for request in client.requests))
        self.assertEqual([request["offset_id"] for request in client.requests], [0, 121, 221, 238])

    async def test_album_straddling_real_telethon_pages_is_yielded_whole(self):
        grouped = {identifier: 777 for identifier in range(99, 103)}
        client = ScriptedHistoryClient([
            ([105], 105, grouped),
            (list(range(100, 0, -1)), 105, grouped),
            (list(range(105, 100, -1)), 105, grouped),
            ([], 105, grouped),
        ])

        result = await self.collect(client, minimum=0)

        self.assertIn([99, 100, 101, 102], result)
        self.assertEqual([item for batch in result for item in batch], list(range(1, 106)))
        self.assertEqual(sum(99 in batch or 100 in batch or 101 in batch or 102 in batch for batch in result), 1)

    async def test_caught_up_cursor_does_not_rescan_already_processed_history(self):
        client = ScriptedHistoryClient([([237], 237, {})])

        result = await self.collect(client, minimum=237)

        self.assertEqual(result, [])
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.requests[0]["limit"], 1)


if __name__ == "__main__":
    unittest.main()
