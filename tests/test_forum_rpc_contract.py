"""Forum MTProto contracts using real Telethon dispatch and response mapping."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient, functions, types, utils
from telethon.sessions import MemorySession

from test_engine import channel, message
from test_upload_contract import TransportCaptureClient, DESTINATION
from tgmover.forum import ForumError, forward_to_topic, get_forum_topic, list_forum_topics

NOW = datetime(2020, 1, 1, tzinfo=timezone.utc)


def forum(identifier=1):
    result = channel(identifier, broadcast=False)
    result.forum = True
    return result


def topic(identifier, *, closed=False, hidden=False):
    return types.ForumTopic(
        id=identifier, date=NOW, peer=types.PeerChannel(1), title=f"topic-{identifier}",
        icon_color=0, top_message=identifier + 100, read_inbox_max_id=0,
        read_outbox_max_id=0, unread_count=0, unread_mentions_count=0,
        unread_reactions_count=0, unread_poll_votes_count=0,
        from_id=types.PeerUser(42), notify_settings=types.PeerNotifySettings(),
        closed=closed, hidden=hidden,
    )


def topic_page(items, *, count=None, creation_order=False):
    return types.messages.ForumTopics(
        count=len(items) if count is None else count, topics=items,
        messages=[message(item.top_message, date=NOW + timedelta(days=1))
                  for item in items if isinstance(item, types.ForumTopic)],
        chats=[forum()], users=[], pts=0, order_by_create_date=creation_order,
    )


class ForumSender:
    def __init__(self, client):
        self.client, self.requests = client, []
        self.pages, self.by_id, self.history = [], {}, []
        self.wrong_topic = False
        self.omit_mapping = False

    def send(self, request, ordered=False):
        while isinstance(request, functions.InvokeWithoutUpdatesRequest):
            request = request.query
        bytes(request)
        self.requests.append(request)
        future = asyncio.get_running_loop().create_future()
        if isinstance(request, functions.messages.GetForumTopicsRequest):
            result = self.pages.pop(0)
        elif isinstance(request, functions.messages.GetForumTopicsByIDRequest):
            result = topic_page([self.by_id.get(identifier, types.ForumTopicDeleted(identifier))
                                 for identifier in request.topics])
        elif isinstance(request, (functions.messages.GetRepliesRequest, functions.messages.GetHistoryRequest)):
            values = self.history.pop(0) if self.history else []
            result = types.messages.MessagesSlice(count=len(values), messages=values, topics=[], chats=[forum()], users=[])
        elif isinstance(request, functions.messages.ForwardMessagesRequest):
            updates = []
            for index, random_id in enumerate(request.random_id):
                target = message(1001 + index, peer=request.to_peer.channel_id)
                if request.top_msg_id:
                    target.reply_to = types.MessageReplyHeader(
                        forum_topic=True, reply_to_msg_id=999 if self.wrong_topic else request.top_msg_id)
                updates.append(types.UpdateNewChannelMessage(message=target, pts=index + 1, pts_count=1))
                if not self.omit_mapping:
                    updates.append(types.UpdateMessageID(id=target.id, random_id=random_id))
            # Mapping must follow input random_id order, not incoming update order.
            result = types.Updates(updates=list(reversed(updates)), users=[], chats=[forum(2)], date=NOW, seq=0)
        else:
            future.set_exception(AssertionError(f"Unexpected offline forum RPC: {type(request).__name__}"))
            return future
        future.set_result(result)
        return future


class ForumClient(TelegramClient):
    def __init__(self):
        super().__init__(MemorySession(), 12345, "a" * 32,
                         request_retries=0, receive_updates=False, flood_sleep_threshold=0)
        self.session.process_entities(types.messages.Chats(chats=[forum(), forum(2)]))
        self._sender = ForumSender(self)

    async def connect(self):
        raise AssertionError("Forum contracts must never connect")


class ForumRpcContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_topic_list_uses_messages_namespace_and_reads_past_short_pages(self):
        client = ForumClient()
        client._sender.pages = [topic_page([topic(1), topic(21)], count=4),
                                topic_page([topic(22), topic(23, closed=True)], count=4)]
        rows = await list_forum_topics(client, forum())
        self.assertEqual([row["id"] for row in rows], [1, 21, 22, 23])
        self.assertTrue(rows[-1]["closed"])
        first, second = client._sender.requests
        self.assertIsInstance(first, functions.messages.GetForumTopicsRequest)
        self.assertEqual((first.offset_id, first.offset_topic), (0, 0))
        self.assertEqual((second.offset_id, second.offset_topic), (121, 21))
        self.assertEqual(second.offset_date, NOW + timedelta(days=1))

    async def test_creation_sorted_topic_page_uses_topic_creation_date(self):
        client = ForumClient()
        client._sender.pages = [topic_page([topic(21)], count=2, creation_order=True),
                                topic_page([topic(1)], count=2, creation_order=True)]
        await list_forum_topics(client, forum())
        self.assertEqual(client._sender.requests[1].offset_date, NOW)

    async def test_hidden_general_is_fetched_by_id_when_absent_from_list(self):
        client = ForumClient()
        client._sender.pages = [topic_page([topic(21)])]
        client._sender.by_id[1] = topic(1, hidden=True)
        rows = await list_forum_topics(client, forum())
        self.assertEqual(rows[0]["id"], 1)
        self.assertTrue(rows[0]["hidden"])
        self.assertIsInstance(client._sender.requests[-1], functions.messages.GetForumTopicsByIDRequest)
        self.assertEqual(client._sender.requests[-1].topics, [1])

    async def test_repeated_paging_cursor_is_an_error_not_a_partial_success(self):
        client = ForumClient()
        client._sender.pages = [topic_page([topic(21)], count=3), topic_page([topic(21)], count=3)]
        with self.assertRaisesRegex(ForumError, "分页"):
            await list_forum_topics(client, forum())

    async def test_deleted_topic_and_nonforum_are_rejected_without_creating_anything(self):
        client = ForumClient()
        with self.assertRaises(ForumError):
            await get_forum_topic(client, forum(), 999)
        before = len(client._sender.requests)
        with self.assertRaises(ForumError):
            await list_forum_topics(client, channel(1, broadcast=False))
        self.assertEqual(len(client._sender.requests), before)

    async def test_general_history_is_real_get_replies_with_id_one(self):
        client = ForumClient()
        client._sender.history = [[message(8), message(7), message(6)], []]
        result = [item.id async for item in client.iter_messages(
            forum(), reply_to=1, min_id=5, max_id=9, reverse=True, wait_time=0)]
        self.assertEqual(result, [6, 7, 8])
        self.assertTrue(all(isinstance(request, functions.messages.GetRepliesRequest)
                            and request.msg_id == 1 for request in client._sender.requests))

    async def test_specific_topic_history_uses_get_replies_not_linked_discussion(self):
        client = ForumClient()
        client._sender.history = [[message(23, root=20), message(22, root=20)], []]
        result = [item.id async for item in client.iter_messages(
            forum(), reply_to=20, min_id=21, max_id=24, reverse=True, wait_time=0)]
        self.assertEqual(result, [22, 23])
        self.assertTrue(all(isinstance(request, functions.messages.GetRepliesRequest)
                            and request.msg_id == 20 for request in client._sender.requests))

    async def test_raw_forum_forward_keeps_album_order_and_maps_out_of_order_updates(self):
        client = ForumClient()
        batch = [message(31, grouped_id=777), message(32, grouped_id=777)]
        sent = await forward_to_topic(client, forum(2), batch, forum(), 21)
        self.assertEqual([item.id for item in sent], [1001, 1002])
        self.assertEqual(len(client._sender.requests), 1)
        request = client._sender.requests[0]
        self.assertIsInstance(request, functions.messages.ForwardMessagesRequest)
        self.assertEqual(request.id, [31, 32])
        self.assertEqual(request.top_msg_id, 21)
        self.assertIsNone(request.reply_to)
        self.assertEqual(len(set(request.random_id)), 2)
        self.assertFalse(request.drop_author)
        self.assertEqual((request.from_peer.channel_id, request.to_peer.channel_id), (1, 2))

    async def test_general_forward_has_no_forum_root_or_reply_parameter(self):
        for identifier in (0, 1):
            with self.subTest(topic=identifier):
                client = ForumClient()
                sent = await forward_to_topic(client, forum(2), [message(31)], forum(), identifier)
                self.assertEqual(len(sent), 1)
                self.assertIsNone(client._sender.requests[0].top_msg_id)
                self.assertIsNone(client._sender.requests[0].reply_to)

    async def test_missing_forward_mapping_or_wrong_topic_never_reports_success(self):
        for field in ("wrong_topic", "omit_mapping"):
            with self.subTest(failure=field):
                client = ForumClient()
                setattr(client._sender, field, True)
                with self.assertRaises(ForumError):
                    await forward_to_topic(client, forum(2), [message(31)], forum(), 21)
                self.assertEqual(len(client._sender.requests), 1)

    async def test_high_level_text_media_and_album_reply_to_existing_topic_root(self):
        client = TransportCaptureClient()
        await client.send_message(DESTINATION, "topic text", reply_to=21)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as base:
            paths = [Path(base) / "first.bin", Path(base) / "second.bin"]
            for path in paths:
                path.write_bytes(b"DATA")
            await client.send_file(DESTINATION, str(paths[0]), reply_to=21)
            await client.send_file(DESTINATION, [str(path) for path in paths], reply_to=21)
        sends = [request for request in client.requests if isinstance(request, (
            functions.messages.SendMessageRequest, functions.messages.SendMediaRequest,
            functions.messages.SendMultiMediaRequest))]
        self.assertEqual(len(sends), 3)
        self.assertTrue(all(request.reply_to.reply_to_msg_id == 21 and request.reply_to.top_msg_id is None
                            for request in sends))
        self.assertEqual(len(sends[-1].multi_media), 2)


if __name__ == "__main__":
    unittest.main()
