"""Exercise real Telethon high-level send code while replacing transport only.

These tests do not replace send_file, send_message, forward_messages, album
assembly, entity validation, local-file conversion, or TL serialization.
The upload and RPC boundaries are captured locally; no socket is connected.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from telethon import TelegramClient, functions, types, utils
from telethon.sessions import MemorySession

from tgmover.engine import Mover
from tgmover.state import State


NOW = datetime.now(timezone.utc)
SOURCE = types.InputPeerChannel(1, 101)
DESTINATION = types.InputPeerChannel(2, 202)


def source_message(identifier, *, text="caption", media=True, grouped_id=None):
    document = types.Document(
        id=identifier * 100, access_hash=777, file_reference=b"original-reference",
        date=NOW, mime_type="application/octet-stream", size=4, dc_id=1,
        attributes=[types.DocumentAttributeFilename(file_name=f"source-{identifier}.bin")],
    ) if media else None
    return types.Message(
        id=identifier, peer_id=types.PeerChannel(1), date=NOW,
        message=text, grouped_id=grouped_id,
        entities=[types.MessageEntityBold(offset=0, length=min(3, len(text)))] if text else [],
        media=types.MessageMediaDocument(document=document) if document else None,
    )


class TransportCaptureClient(TelegramClient):
    """A real client whose upload and network boundaries cannot reach Telegram."""

    def __init__(self):
        super().__init__(MemorySession(), 12345, "a" * 32, receive_updates=False)
        self.uploads = []
        self.requests = []
        self.next_message_id = 1000

    async def connect(self):
        raise AssertionError("Contract tests must never connect to a network.")

    async def get_input_entity(self, peer):
        return utils.get_input_peer(peer)

    async def upload_file(self, file, **kwargs):
        if isinstance(file, (types.InputFile, types.InputFileBig)):
            raise AssertionError("The mover must supply local bytes, not an uploaded handle.")
        path = Path(file)
        data = path.read_bytes()
        handle = types.InputFile(
            id=5000 + len(self.uploads), parts=1, name=path.name,
            md5_checksum="0" * 32,
        )
        self.uploads.append((path, data, handle))
        return handle

    def sent_message(self, peer, text, reply_to=None):
        self.next_message_id += 1
        return types.Message(
            id=self.next_message_id, peer_id=types.PeerChannel(peer.channel_id),
            date=NOW, message=text,
            reply_to=types.MessageReplyHeader(reply_to_msg_id=reply_to.reply_to_msg_id)
            if reply_to else None,
        )

    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        # Serialize the actual TL request as an additional argument-type check.
        bytes(request)
        self.requests.append(request)
        if isinstance(request, functions.messages.UploadMediaRequest):
            uploaded = request.media
            if not isinstance(uploaded, types.InputMediaUploadedDocument):
                raise AssertionError(f"Unexpected uploaded media type: {type(uploaded).__name__}")
            return types.MessageMediaDocument(document=types.Document(
                id=uploaded.file.id + 10000, access_hash=888,
                file_reference=b"new-upload-reference", date=NOW,
                mime_type=uploaded.mime_type, size=4, dc_id=1,
                attributes=uploaded.attributes,
            ))
        if isinstance(request, functions.messages.SendMultiMediaRequest):
            return [self.sent_message(request.peer, item.message, request.reply_to)
                    for item in request.multi_media]
        if isinstance(request, (functions.messages.SendMessageRequest,
                                functions.messages.SendMediaRequest)):
            return self.sent_message(request.peer, request.message, request.reply_to)
        if isinstance(request, functions.messages.ForwardMessagesRequest):
            return [self.sent_message(request.to_peer, "forwarded") for _ in request.id]
        raise AssertionError(f"Unexpected RPC boundary: {type(request).__name__}")

    def _get_response_message(self, request, result, input_chat):
        # Mapping updates to messages is outside this suite's upload contract.
        return result


class UploadContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.client = TransportCaptureClient()
        self.state = State(self.base / "state.sqlite3")
        self.addCleanup(self.state.close)
        self.mover = Mover(self.client, SimpleNamespace(send_delay=0), self.state)
        self.mover.source, self.mover.target = SOURCE, DESTINATION
        self.mover.route_id = self.state.route({"test": "real-telethon-contract"})

    def local_file(self, name, data=b"DATA"):
        path = self.base / name
        path.write_bytes(data)
        return path

    async def test_reupload_local_path_reaches_actual_upload_file(self):
        item = source_message(12)
        path = self.local_file("media.bin")

        await self.mover.send_reupload([item], {item.id: path}, DESTINATION, None)

        self.assertEqual([(path, data) for path, data, _ in self.client.uploads], [(path, b"DATA")])
        self.assertEqual(len(self.client.requests), 1)
        request = self.client.requests[0]
        self.assertIsInstance(request, functions.messages.SendMediaRequest)
        self.assertIsInstance(request.media, types.InputMediaUploadedDocument)
        self.assertIs(request.media.file, self.client.uploads[0][2])
        self.assertNotEqual(request.media.file.id, item.document.id)
        self.assertEqual(request.message, item.message)
        self.assertIsInstance(request.entities[0], types.MessageEntityBold)

    async def test_album_uploads_each_local_file_and_sends_one_ordered_rpc(self):
        batch = [source_message(21, text="first", grouped_id=99),
                 source_message(22, text="second", grouped_id=99)]
        paths = {21: self.local_file("first.bin", b"ONE!"),
                 22: self.local_file("second.bin", b"TWO!")}

        await self.mover.send_reupload(batch, paths, DESTINATION, None)

        self.assertEqual([row[0] for row in self.client.uploads], [paths[21], paths[22]])
        self.assertEqual([row[1] for row in self.client.uploads], [b"ONE!", b"TWO!"])
        uploads = [r for r in self.client.requests if isinstance(r, functions.messages.UploadMediaRequest)]
        sends = [r for r in self.client.requests if isinstance(r, functions.messages.SendMultiMediaRequest)]
        self.assertEqual(len(uploads), 2)
        self.assertEqual(len(sends), 1)
        self.assertFalse(any(isinstance(r, functions.messages.SendMediaRequest) for r in self.client.requests))
        request = sends[0]
        self.assertEqual([item.message for item in request.multi_media], ["first", "second"])
        self.assertEqual([item.media.id.id for item in request.multi_media],
                         [row[2].id + 10000 for row in self.client.uploads])
        self.assertTrue(all(isinstance(item.entities[0], types.MessageEntityBold)
                            for item in request.multi_media))

    async def test_forward_uses_original_message_ids_without_upload(self):
        batch = [source_message(31, grouped_id=100), source_message(32, grouped_id=100)]

        await self.mover.process(batch, "post", "forward")

        self.assertEqual(self.client.uploads, [])
        self.assertEqual(len(self.client.requests), 1)
        request = self.client.requests[0]
        self.assertIsInstance(request, functions.messages.ForwardMessagesRequest)
        self.assertEqual(request.id, [31, 32])
        self.assertEqual(request.from_peer.channel_id, SOURCE.channel_id)
        self.assertEqual(request.to_peer.channel_id, DESTINATION.channel_id)
        self.assertFalse(request.drop_author)
        self.assertEqual(len(self.state.messages(self.mover.route_id, "post")), 2)

    async def test_comment_text_uses_regular_input_reply_to_message(self):
        item = source_message(41, text="comment text", media=False)

        await self.mover.send_reupload([item], {}, DESTINATION, 9876)

        self.assertEqual(self.client.uploads, [])
        request = self.client.requests[0]
        self.assertIsInstance(request, functions.messages.SendMessageRequest)
        self.assertIsInstance(request.reply_to, types.InputReplyToMessage)
        self.assertEqual(request.reply_to.reply_to_msg_id, 9876)
        self.assertIsNone(request.reply_to.top_msg_id)
        self.assertEqual(request.message, item.message)

    async def test_comment_media_upload_keeps_regular_thread_reply(self):
        item = source_message(51)
        path = self.local_file("comment.bin")

        await self.mover.send_reupload([item], {item.id: path}, DESTINATION, 9876)

        self.assertEqual(len(self.client.uploads), 1)
        request = self.client.requests[0]
        self.assertIsInstance(request, functions.messages.SendMediaRequest)
        self.assertIsInstance(request.media, types.InputMediaUploadedDocument)
        self.assertIsInstance(request.reply_to, types.InputReplyToMessage)
        self.assertEqual(request.reply_to.reply_to_msg_id, 9876)
        self.assertIsNone(request.reply_to.top_msg_id)


if __name__ == "__main__":
    unittest.main()
