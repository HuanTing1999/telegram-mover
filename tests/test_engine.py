"""Offline engine integration tests with real Telethon TL message objects.

FakeClient replaces Telegram I/O, not Message media/reply/peer behavior. Tests
use actual SQLite files and downloaded bytes, and never contact Telegram.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from telethon import errors, functions, types, utils

from tgmover.config import Config
from tgmover.engine import Mover, SyncError, batches, thread_root
from tgmover.limits import LimitSettings
from tgmover.state import State


OLD = datetime.now(timezone.utc) - timedelta(days=1)


def channel(identifier, *, broadcast=True):
    return types.Channel(
        id=identifier, title=f"chat-{identifier}", photo=types.ChatPhotoEmpty(),
        date=OLD, broadcast=broadcast, megagroup=not broadcast, access_hash=identifier * 100,
    )


def message(identifier, peer=1, *, text="text", grouped_id=None, media=False, root=None, date=OLD):
    document = None
    if media:
        document = types.MessageMediaDocument(document=types.Document(
            id=identifier * 100, access_hash=999, file_reference=b"source-reference",
            date=OLD, mime_type="application/octet-stream", size=4, dc_id=1,
            attributes=[types.DocumentAttributeFilename(file_name=f"source-{identifier}.bin")],
        ))
    return types.Message(
        id=identifier, peer_id=types.PeerChannel(peer), date=date,
        message=text, grouped_id=grouped_id, media=document,
        reply_to=types.MessageReplyHeader(reply_to_msg_id=root) if root else None,
    )


class FakeClient:
    """Minimal API-compatible Telegram peer/history/discussion and send stub."""

    def __init__(self):
        self.source, self.target = channel(1), channel(2)
        self.source_link, self.target_link = channel(3, broadcast=False), channel(4, broadcast=False)
        self.entities = {"source": self.source, "target": self.target}
        self.links = {1: self.source_link, 2: self.target_link}
        self.history = {}
        self.roots = {}
        self.calls = []
        self.next_id = 100
        self.send_failure = None
        self.truncate_album = False
        self.bad_reply = False
        self.video_processing_pending = False

    def add(self, peer, messages, *, root=None):
        self.history.setdefault((peer, root), []).extend(messages)

    def link_post(self, post_id, root):
        self.roots[(1, post_id)] = root

    async def get_entity(self, value):
        if isinstance(value, (types.Channel, types.Chat)):
            return value
        if isinstance(value, types.PeerChannel):
            return next(entity for entity in [*self.entities.values(), *self.links.values()]
                        if entity and entity.id == value.channel_id)
        return self.entities[value]

    async def get_me(self):
        return SimpleNamespace(id=42)

    async def __call__(self, request):
        self.calls.append(("request", type(request).__name__))
        if isinstance(request, functions.channels.GetFullChannelRequest):
            linked = self.links.get(request.channel.id)
            return SimpleNamespace(
                full_chat=SimpleNamespace(linked_chat_id=linked.id if linked else None),
                chats=[linked] if linked else [],
            )
        if isinstance(request, functions.messages.GetDiscussionMessageRequest):
            root = self.roots.get((request.peer.id, request.msg_id))
            if root is None:
                return SimpleNamespace(messages=[])
            linked = self.links[request.peer.id]
            # Telegram includes the original channel post in the response too.
            return SimpleNamespace(messages=[
                message(request.msg_id, request.peer.id), message(root, linked.id),
            ])
        raise AssertionError(f"Unexpected request {request!r}")

    async def get_messages(self, entity, *, limit=1, reply_to=None, offset_id=0, ids=None):
        self.calls.append(("latest", entity.id, reply_to))
        if ids is not None:
            return message(ids, entity.id) if ids in self.roots.values() else None
        history = self.history.get((entity.id, reply_to), [])
        if reply_to is None and entity.id in {3, 4}:
            history = [item for (peer, _), items in self.history.items() if peer == entity.id for item in items]
        if offset_id:
            history = [item for item in history if item.id < offset_id]
        return sorted(history, key=lambda value: value.id, reverse=True)[:limit]

    def iter_messages(self, entity, *, min_id, max_id, reverse, reply_to=None, limit=None, wait_time=None):
        self.calls.append(("history", entity.id, min_id, max_id, reverse, reply_to, limit))

        async def iterator():
            for item in sorted(self.history.get((entity.id, reply_to), []), key=lambda value: value.id):
                if min_id < item.id < max_id:
                    yield item

        return iterator()

    async def download_media(self, item, *, file, progress_callback=None):
        self.calls.append(("download", item.id, file))
        Path(file).write_bytes(b"DATA")
        if progress_callback:
            progress_callback(4, 4)
        return file

    def _sent(self, destination, *, caption="", reply_to=None):
        self.next_id += 1
        if destination.id == self.target.id and self.links.get(self.target.id):
            self.roots[(destination.id, self.next_id)] = self.next_id + 10000
        result = message(self.next_id, destination.id, text=caption,
                         root=999999 if self.bad_reply else reply_to)
        result.video_processing_pending = self.video_processing_pending
        return result

    async def send_file(self, destination, file, **kwargs):
        self.calls.append(("file", destination.id, file, kwargs))
        if self.send_failure:
            raise self.send_failure
        if isinstance(file, list):
            captions = kwargs.get("caption", [""] * len(file))
            results = [self._sent(destination, caption=caption, reply_to=kwargs.get("reply_to"))
                       for caption in captions]
            return results[:-1] if self.truncate_album else results
        return self._sent(destination, caption=kwargs.get("caption", ""), reply_to=kwargs.get("reply_to"))

    async def send_message(self, destination, text, **kwargs):
        self.calls.append(("text", destination.id, text, kwargs))
        if self.send_failure:
            raise self.send_failure
        return self._sent(destination, caption=text, reply_to=kwargs.get("reply_to"))

    async def forward_messages(self, destination, batch, **kwargs):
        self.calls.append(("forward", destination.id, [item.id for item in batch], kwargs))
        if self.send_failure:
            raise self.send_failure
        return [self._sent(destination, caption=item.message) for item in batch]

    def sends(self):
        return [call for call in self.calls if call[0] in {"file", "text", "forward"}]


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = State(self.base / "state.sqlite3")
        self.addCleanup(self.state.close)
        self.client = FakeClient()
        self.config = Config(
            api_id=12345, api_hash="a" * 32, source="source", target="target",
            mode="reupload", comment_mode="reupload", sync_comments=True,
            session=self.base / "session", database=self.base / "state.sqlite3",
            download_dir=self.base / "downloads", start_id=0, poll_interval=0,
            settle_seconds=2, max_flood_wait=0, send_delay=0,
            limits=LimitSettings(enabled=False),
        )

    async def mover(self, **overrides):
        mover = Mover(self.client, replace(self.config, **overrides), self.state)
        await mover.prepare()
        return mover

    async def test_batches_preserve_album_members_and_source_order(self):
        items = [message(1), message(2, grouped_id=10), message(3, grouped_id=10),
                 message(4, grouped_id=20), message(5, grouped_id=20), message(6)]

        async def source():
            for item in items:
                yield item

        result = [[item.id for item in batch] async for batch in batches(source())]
        self.assertEqual(result, [[1], [2, 3], [4, 5], [6]])

    async def test_scan_requests_ascending_history_and_groups_album(self):
        self.client.add(1, [message(3), message(2, grouped_id=77), message(1, grouped_id=77)])
        mover = await self.mover(sync_comments=False)
        result = [[item.id for item in batch] async for batch in mover.scan(mover.source, minimum=0)]
        self.assertEqual(result, [[1, 2], [3]])
        request = next(call for call in self.client.calls if call[0] == "history")
        self.assertEqual(request[2:5], (0, 4, True))

    async def test_recent_album_is_deferred_without_cursor_advance(self):
        self.client.add(1, [message(1, grouped_id=77, media=True),
                            message(2, grouped_id=77, media=True, date=datetime.now(timezone.utc))])
        mover = await self.mover(sync_comments=False)
        await mover.run_once()
        self.assertEqual(self.client.sends(), [])
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)

    async def test_start_id_inside_album_stops_without_partial_delivery(self):
        self.client.add(1, [message(1, grouped_id=77, media=True),
                            message(2, grouped_id=77, media=True)])
        mover = await self.mover(start_id=1, sync_comments=False)
        with self.assertRaises(SyncError):
            await mover.run_once()
        self.assertEqual(self.client.sends(), [])
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)

    async def test_download_only_archives_posts_comments_and_never_sends(self):
        self.client.add(1, [message(1, media=True)])
        self.client.link_post(1, 10)
        self.client.add(3, [message(11, 3, media=True, root=10)], root=10)
        mover = await self.mover(mode="download_only", comment_mode="download_only", target=None)
        await mover.run_once()
        self.assertEqual(self.client.sends(), [])
        for scope, identifier in (("post", 1), ("comment", 11)):
            download = self.state.download(mover.route_id, scope, identifier)
            self.assertEqual(Path(download["path"]).read_bytes(), b"DATA")
            self.assertEqual(download["size"], 4)
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 1)
        self.assertEqual(self.state.cursor(mover.route_id, "comment", 10), 11)

    async def test_download_filename_is_contained_and_windows_safe_without_changing_bytes(self):
        mover = await self.mover(mode="download_only", comment_mode="download_only", target=None)
        originals = [
            "../../outside.bin", r"..\..\outside.bin", "CON.bin", "NUL.bin",
            'unsafe<>:"/\\|?*\x00\x1f.bin', "L" * 200 + ".bin", "report.bin.  ",
        ]
        for identifier, filename in enumerate(originals, 1):
            with self.subTest(filename=filename):
                item = message(identifier, media=True)
                item.document.attributes = [types.DocumentAttributeFilename(file_name=filename)]
                path = await mover.download(item, "post")
                expected_parent = self.config.download_dir / mover.route_id / "post" / str(identifier)
                self.assertEqual(path.parent.resolve(), expected_parent.resolve())
                self.assertTrue(path.name.startswith(f"{identifier}_"))
                self.assertNotRegex(path.name, r'[<>:"/\\|?*\x00-\x1f]')
                self.assertEqual(path.name, path.name.rstrip(" ."))
                self.assertLessEqual(len(path.stem.removeprefix(f"{identifier}_")), 120)
                self.assertRegex(path.suffix, r"^\.[A-Za-z0-9]{1,12}$")
                self.assertEqual(path.read_bytes(), b"DATA")
                record = self.state.download(mover.route_id, "post", identifier)
                self.assertEqual((Path(record["path"]), record["size"]), (path.resolve(), 4))
        self.assertEqual(self.client.sends(), [])

    async def test_reupload_album_uses_local_paths_no_cache_and_single_send(self):
        self.client.add(1, [message(1, text="first", media=True, grouped_id=77),
                            message(2, text="second", media=True, grouped_id=77)])
        mover = await self.mover(sync_comments=False)
        await mover.run_once()
        sends = self.client.sends()
        self.assertEqual(len(sends), 1)
        kind, destination, paths, kwargs = sends[0]
        self.assertEqual((kind, destination), ("file", 2))
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(isinstance(path, str) and Path(path).is_file() for path in paths))
        self.assertEqual(kwargs["caption"], ["first", "second"])
        self.assertIs(kwargs["allow_cache"], False)
        self.assertEqual([row["target_id"] for row in self.state.messages(mover.route_id, "post")], [101, 102])
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 2)

    async def test_text_reupload_sends_message_and_repeat_run_is_idempotent(self):
        self.client.add(1, [message(1, text="hello")])
        mover = await self.mover(sync_comments=False)
        await mover.run_once()
        await mover.run_once()
        self.assertEqual(len(self.client.sends()), 1)
        self.assertEqual(self.client.sends()[0][:3], ("text", 2, "hello"))
        self.assertEqual(self.state.pending(mover.route_id), [])

    async def test_forward_does_not_download_but_download_then_forward_does(self):
        batch = [message(1, media=True, grouped_id=77), message(2, media=True, grouped_id=77)]
        for mode, expected_downloads in (("forward", 0), ("download_then_forward", 2)):
            with self.subTest(mode=mode):
                self.client.calls.clear()
                mover = await self.mover(mode=mode, sync_comments=False)
                await mover.process(batch, "post", mode)
                self.assertEqual(len([call for call in self.client.calls if call[0] == "download"]), expected_downloads)
                sends = self.client.sends()
                self.assertEqual(len(sends), 1)
                self.assertEqual(sends[0][:3], ("forward", 2, [1, 2]))
                self.assertIs(sends[0][3]["from_peer"], self.client.source)

    async def test_old_post_new_comments_sync_with_independent_thread_cursors(self):
        self.client.add(1, [message(1), message(2)])
        self.client.link_post(1, 10)
        self.client.link_post(2, 20)
        self.client.add(3, [message(11, 3, text="one", root=10)], root=10)
        self.client.add(3, [message(25, 3, text="two", root=20)], root=20)
        mover = await self.mover()
        await mover.run_once()
        first_count = len(self.client.sends())
        self.assertEqual(first_count, 4)
        self.client.add(3, [message(30, 3, text="late one", media=True, root=10)], root=10)
        self.client.add(3, [message(27, 3, text="late two", root=20)], root=20)
        await mover.run_once()
        self.assertEqual(len(self.client.sends()), first_count + 2)
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 2)
        self.assertEqual(self.state.cursor(mover.route_id, "comment", 10), 30)
        self.assertEqual(self.state.cursor(mover.route_id, "comment", 20), 27)
        thread = self.state.get_thread(mover.route_id, 1)
        self.assertEqual((thread["source_root_id"], thread["target_post_id"], thread["target_root_id"]),
                         (10, 101, 10101))
        comment = self.state.get_message(mover.route_id, "comment", 30)
        self.assertEqual(comment["target_root_id"], 10101)
        media_call = next(call for call in self.client.sends()[first_count:] if call[0] == "file")
        self.assertEqual((media_call[1], media_call[3]["reply_to"]), (4, 10101))
        self.assertIs(media_call[3]["allow_cache"], False)
        await mover.run_once()
        self.assertEqual(len(self.client.sends()), first_count + 2)

    async def test_target_without_linked_discussion_fails_before_sending(self):
        self.client.links[2] = None
        self.client.add(1, [message(1)])
        with self.assertRaisesRegex(SyncError, "linked chat"):
            await self.mover()
        self.assertEqual(self.client.sends(), [])

    async def test_comment_forward_modes_are_explicitly_rejected_before_sending(self):
        for mode in ("forward", "download_then_forward"):
            with self.subTest(mode=mode), self.assertRaisesRegex(SyncError, "comment_mode='reupload'"):
                await self.mover(comment_mode=mode)
        self.assertEqual(self.client.sends(), [])

    async def test_network_uncertainty_preserves_pending_and_blocks_restart(self):
        self.client.add(1, [message(1)])
        self.client.send_failure = OSError("connection lost after possible acceptance")
        mover = await self.mover(sync_comments=False)
        with self.assertRaises(OSError):
            await mover.run_once()
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)
        self.assertIsNone(self.state.get_message(mover.route_id, "post", 1))
        self.assertEqual(self.state.pending(mover.route_id)[0]["source_ids"], [1])
        self.client.send_failure = None
        before = len(self.client.sends())
        with self.assertRaisesRegex(SyncError, "人工核对"):
            await self.mover(sync_comments=False)
        self.assertEqual(len(self.client.sends()), before)

    async def test_definite_rpc_rejection_clears_pending_without_advancing(self):
        mover = await self.mover(sync_comments=False)
        self.client.send_failure = errors.ChatWriteForbiddenError(request=None)
        with self.assertRaises(SyncError):
            await mover.process([message(1)], "post", "reupload")
        self.assertEqual(self.state.pending(mover.route_id), [])
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)

    async def test_server_rpc_error_keeps_uncertain_pending(self):
        mover = await self.mover(sync_comments=False)
        self.client.send_failure = errors.ServerError(request=None, message="INTERNAL", code=500)
        with self.assertRaises(SyncError):
            await mover.process([message(1)], "post", "reupload")
        self.assertEqual(len(self.state.pending(mover.route_id)), 1)
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)

    async def test_async_video_processing_preserves_pending_without_temporary_id_mapping(self):
        mover = await self.mover(sync_comments=False)
        item = message(1, media=True)
        item.document.mime_type = "video/mp4"
        item.document.attributes.append(types.DocumentAttributeVideo(duration=1, w=320, h=240))
        self.client.video_processing_pending = True
        with self.assertRaisesRegex(SyncError, "异步处理视频"):
            await mover.process([item], "post", "reupload")
        self.assertEqual(len(self.client.sends()), 1)
        self.assertIsNone(self.state.get_message(mover.route_id, "post", 1))
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)
        self.assertEqual(self.state.pending(mover.route_id)[0]["source_ids"], [1])
        download = self.state.download(mover.route_id, "post", 1)
        self.assertEqual((Path(download["path"]).read_bytes(), download["size"]), (b"DATA", 4))
        self.client.video_processing_pending = False
        with self.assertRaisesRegex(SyncError, "人工核对"):
            await self.mover(sync_comments=False)
        self.assertEqual(len(self.client.sends()), 1)

    async def test_deleted_target_discussion_root_blocks_later_comments(self):
        self.client.add(1, [message(1)])
        self.client.link_post(1, 10)
        self.client.add(3, [message(11, 3, root=10)], root=10)
        mover = await self.mover()
        await mover.run_once()
        before = len(self.client.sends())
        del self.client.roots[(2, 101)]
        self.client.add(3, [message(12, 3, root=10)], root=10)
        with self.assertRaisesRegex(SyncError, "已删除或不可读"):
            await mover.run_once()
        self.assertEqual(len(self.client.sends()), before)
        self.assertEqual(self.state.cursor(mover.route_id, "comment", 10), 11)

    async def test_service_message_is_archived_as_skipped_without_send(self):
        mover = await self.mover(sync_comments=False)
        service = types.MessageService(id=1, peer_id=types.PeerChannel(1), date=OLD,
                                       action=types.MessageActionPinMessage())
        await mover.process([service], "post", "reupload")
        self.assertEqual(self.client.sends(), [])
        self.assertEqual(self.state.get_message(mover.route_id, "post", 1)["status"], "skipped_service")
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 1)

    async def test_partial_recorded_album_stops_without_sending_or_advancing(self):
        mover = await self.mover(sync_comments=False)
        self.state.save_messages(mover.route_id, "post", [{"source_id": 1, "target_id": 101}], cursor=1)
        batch = [message(1, media=True, grouped_id=77), message(2, media=True, grouped_id=77)]
        with self.assertRaisesRegex(SyncError, "部分已处理的相册"):
            await mover.process(batch, "post", "reupload")
        self.assertEqual(self.client.sends(), [])
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 1)

    async def test_incomplete_send_response_keeps_pending_without_cursor_advance(self):
        mover = await self.mover(sync_comments=False)
        self.client.truncate_album = True
        batch = [message(1, media=True, grouped_id=77), message(2, media=True, grouped_id=77)]
        with self.assertRaisesRegex(SyncError, "完整目标 ID"):
            await mover.process(batch, "post", "reupload")
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)
        self.assertEqual(self.state.pending(mover.route_id)[0]["source_ids"], [1, 2])

    async def test_wrong_comment_reply_response_preserves_pending(self):
        mover = await self.mover()
        self.client.bad_reply = True
        context = {"source_post_id": 1, "source_root_id": 10, "target_root_id": 10101}
        with self.assertRaisesRegex(SyncError, "评论线程响应不匹配"):
            await mover.process([message(11, 3, root=10)], "comment", "reupload", context=context)
        self.assertEqual(self.state.cursor(mover.route_id, "comment", 10), 0)
        self.assertEqual(len(self.state.pending(mover.route_id)), 1)

    async def test_thread_root_uses_linked_group_peer_not_channel_post(self):
        result = thread_root([message(1, 1), message(200, 3), message(201, 3)],
                             utils.get_peer_id(self.client.source_link))
        self.assertEqual(result.id, 200)


if __name__ == "__main__":
    unittest.main()
