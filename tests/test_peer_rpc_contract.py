"""Real Telethon entity parsing and cache contracts, without Telegram I/O.

Some tests intentionally document why handing message URLs or numeric strings
directly to Telethon fails. Application normalization can then use supported
usernames or typed IDs without depending on undocumented URL parsing.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from telethon import TelegramClient, errors, functions, types, utils
from telethon.sessions import MemorySession

from test_engine import channel
from tgmover.config import Config
from tgmover.engine import Mover, SyncError
from tgmover.limits import LimitSettings
from tgmover.state import State


class PeerSender:
    def __init__(self, client):
        self.client = client
        self.requests = []

    def send(self, request, ordered=False):
        while isinstance(request, functions.InvokeWithoutUpdatesRequest):
            request = request.query
        self.requests.append(request)
        future = asyncio.get_running_loop().create_future()
        if self.client.rpc_error_type is not None:
            future.set_exception(self.client.rpc_error_type(request))
            return future
        if isinstance(request, functions.contacts.ResolveUsernameRequest):
            if request.username == self.client.channel.username:
                future.set_result(types.contacts.ResolvedPeer(
                    peer=types.PeerChannel(self.client.channel.id), chats=[self.client.channel], users=[],
                ))
            else:
                future.set_exception(errors.UsernameNotOccupiedError(request))
        elif isinstance(request, functions.contacts.GetContactsRequest):
            future.set_result(types.contacts.Contacts(contacts=[], saved_count=0, users=[]))
        elif isinstance(request, functions.channels.GetChannelsRequest):
            if all(item.channel_id == self.client.channel.id
                   and item.access_hash == self.client.channel.access_hash for item in request.id):
                future.set_result(types.messages.Chats(chats=[self.client.channel]))
            else:
                future.set_exception(errors.ChannelInvalidError(request))
        elif isinstance(request, functions.messages.CheckChatInviteRequest):
            if self.client.joined:
                future.set_result(types.ChatInviteAlready(chat=self.client.channel))
            else:
                future.set_result(types.ChatInvite(
                    title="Offline invite", photo=types.PhotoEmpty(id=0),
                    participants_count=0, color=0, channel=True, broadcast=True,
                ))
        else:
            future.set_exception(AssertionError(f"Unexpected offline peer RPC: {type(request).__name__}"))
        return future


class PeerContractClient(TelegramClient):
    def __init__(self, *, cached=False, joined=True):
        super().__init__(MemorySession(), 12345, "a" * 32,
                         request_retries=0, flood_sleep_threshold=0,
                         raise_last_call_error=True, receive_updates=False)
        self.channel = channel(1234567890)
        self.channel.username = "sample_channel"
        self.joined = joined
        self.rpc_error_type = None
        self.dialogs_calls = 0
        self.dialog_entities = [self.channel]
        self._sender = PeerSender(self)
        if cached:
            self.cache_channel()

    def cache_channel(self):
        self.session.process_entities(types.messages.Chats(chats=[self.channel]))

    async def connect(self):
        raise AssertionError("Peer contract tests must never connect")

    async def get_dialogs(self):
        self.dialogs_calls += 1
        self.session.process_entities(types.messages.Chats(chats=self.dialog_entities))
        return [SimpleNamespace(entity=item, name=item.title, id=utils.get_peer_id(item))
                for item in self.dialog_entities]


class PeerRpcContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_usernames_and_chat_urls_use_resolve_username(self):
        for value in ("@sample_channel", "sample_channel", "https://t.me/sample_channel",
                      "https://telegram.me/sample_channel/", "t.me/sample_channel"):
            with self.subTest(value=value):
                client = PeerContractClient()
                result = await client.get_entity(value)
                self.assertEqual(result.id, client.channel.id)
                self.assertEqual(len(client._sender.requests), 1)
                request = client._sender.requests[0]
                self.assertIsInstance(request, functions.contacts.ResolveUsernameRequest)
                self.assertEqual(request.username, "sample_channel")

    async def test_message_urls_are_not_chat_identifiers_even_with_a_warm_cache(self):
        for value in ("https://t.me/sample_channel/123", "https://t.me/sample_channel/12/123",
                      "https://t.me/c/1234567890/123", "https://t.me/c/1234567890/12/123",
                      "https://t.me/s/sample_channel/123", "https://t.me/sample_channel?single"):
            with self.subTest(value=value):
                client = PeerContractClient(cached=True)
                with self.assertRaises(ValueError):
                    await client.get_entity(value)
                self.assertEqual(client._sender.requests, [])

    async def test_numeric_strings_take_the_phone_contact_path_not_the_id_cache(self):
        client = PeerContractClient(cached=True)
        marked = utils.get_peer_id(client.channel)
        for value in (str(client.channel.id), str(marked)):
            with self.subTest(value=value):
                before = len(client._sender.requests)
                with self.assertRaises(ValueError):
                    await client.get_entity(value)
                requests = client._sender.requests[before:]
                self.assertEqual(len(requests), 1)
                self.assertIsInstance(requests[0], functions.contacts.GetContactsRequest)

    async def test_integer_marked_ids_and_typed_channel_ids_use_account_access_hash(self):
        client = PeerContractClient(cached=True)
        marked = utils.get_peer_id(client.channel)
        for value in (marked, client.channel.id, types.PeerChannel(client.channel.id)):
            with self.subTest(value=type(value).__name__):
                before = len(client._sender.requests)
                result = await client.get_entity(value)
                self.assertEqual(result.id, client.channel.id)
                requests = client._sender.requests[before:]
                self.assertEqual(len(requests), 1)
                self.assertIsInstance(requests[0], functions.channels.GetChannelsRequest)
                self.assertEqual(requests[0].id[0].access_hash, client.channel.access_hash)

    async def test_private_id_needs_this_accounts_hash_then_resolves_after_cache_population(self):
        client = PeerContractClient()
        marked = utils.get_peer_id(client.channel)
        with self.assertRaises(ValueError):
            await client.get_entity(marked)
        first = client._sender.requests[-1]
        self.assertIsInstance(first, functions.channels.GetChannelsRequest)
        self.assertEqual(first.id[0].access_hash, 0)

        # Equivalent to the entity cache populated by a dialogs RPC response.
        client.cache_channel()
        result = await client.get_entity(marked)
        self.assertEqual(result.id, client.channel.id)
        self.assertEqual(client._sender.requests[-1].id[0].access_hash, client.channel.access_hash)

    async def test_joined_invite_links_are_checked_without_joining_or_importing(self):
        for value in ("https://t.me/+Offline_Invite-ABC", "https://t.me/joinchat/Offline_Invite-ABC"):
            with self.subTest(value=value):
                client = PeerContractClient(joined=True)
                result = await client.get_entity(value)
                self.assertEqual(result.id, client.channel.id)
                self.assertEqual(len(client._sender.requests), 1)
                self.assertIsInstance(client._sender.requests[0], functions.messages.CheckChatInviteRequest)
                self.assertEqual(client._sender.requests[0].hash, "Offline_Invite-ABC")

    async def test_unjoined_invite_reports_failure_without_automatic_membership_change(self):
        client = PeerContractClient(joined=False)
        with self.assertRaisesRegex(ValueError, "not part of"):
            await client.get_entity("https://t.me/+Offline_Invite-ABC")
        self.assertEqual(len(client._sender.requests), 1)
        self.assertIsInstance(client._sender.requests[0], functions.messages.CheckChatInviteRequest)
        self.assertFalse(any(isinstance(item, (functions.messages.ImportChatInviteRequest,
                                              functions.channels.JoinChannelRequest))
                             for item in client._sender.requests))


class ApplicationPeerResolutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.state = State(base / "state.sqlite3")
        self.addCleanup(self.state.close)
        self.config = Config(
            api_id=12345, api_hash="a" * 32, source="source", target="target",
            mode="reupload", comment_mode="reupload", sync_comments=False,
            session=base / "session", database=base / "state.sqlite3", download_dir=base / "downloads",
            start_id=0, poll_interval=0, settle_seconds=2, max_flood_wait=300,
            send_delay=0, limits=LimitSettings(enabled=False),
        )

    async def test_public_message_links_normalize_to_username_and_resolve_without_dialogs(self):
        from tgmover.peers import parse_chat_reference

        for value in ("https://t.me/sample_channel/123?single#fragment",
                      "https://t.me/sample_channel/12/123", "https://t.me/s/sample_channel/123",
                      "tg://resolve?domain=sample_channel&post=123"):
            with self.subTest(value=value):
                reference = parse_chat_reference(value)
                self.assertEqual(reference.peer, "@sample_channel")
                self.assertEqual(reference.kind, "username")
                self.assertTrue(reference.from_message)
                client = PeerContractClient()
                mover = Mover(client, self.config, self.state)
                result = await mover.resolve_entity(value, role="源频道／群组")
                self.assertEqual(result.id, client.channel.id)
                self.assertEqual(client.dialogs_calls, 0)

    async def test_private_message_links_normalize_to_marked_id_and_fill_current_account_cache(self):
        from tgmover.peers import parse_chat_reference

        for value in ("https://t.me/c/1234567890/123", "https://t.me/c/1234567890/12/123",
                      "tg://privatepost?channel=1234567890&post=123"):
            with self.subTest(value=value):
                client = PeerContractClient()
                reference = parse_chat_reference(value)
                self.assertEqual(reference.peer, utils.get_peer_id(client.channel))
                self.assertTrue(reference.from_message)
                mover = Mover(client, self.config, self.state)
                result = await mover.resolve_entity(value, role="源频道／群组")
                self.assertEqual(result.id, client.channel.id)
                self.assertEqual(client.dialogs_calls, 1)
                self.assertFalse(any(isinstance(item, functions.contacts.GetContactsRequest)
                                     for item in client._sender.requests))

    async def test_normalized_numeric_strings_do_not_query_phone_contacts(self):
        from tgmover.peers import parse_chat_reference

        client = PeerContractClient(cached=True)
        mover = Mover(client, self.config, self.state)
        for value in (str(utils.get_peer_id(client.channel)), str(client.channel.id)):
            with self.subTest(value=value):
                self.assertIsInstance(parse_chat_reference(value).peer, int)
                result = await mover.resolve_entity(value, role="目标频道／群组")
                self.assertEqual(result.id, client.channel.id)
        self.assertEqual(client.dialogs_calls, 0)
        self.assertFalse(any(isinstance(item, functions.contacts.GetContactsRequest)
                             for item in client._sender.requests))

    async def test_unknown_explicit_username_errors_identify_role_without_dialog_refresh(self):
        client = PeerContractClient()
        mover = Mover(client, self.config, self.state)
        with self.assertRaisesRegex(SyncError, "目标"):
            await mover.resolve_entity("@missing_channel", role="目标频道／群组")
        self.assertEqual(client.dialogs_calls, 0)

    async def test_unjoined_invite_is_actionable_and_never_joins_or_scans_dialogs(self):
        client = PeerContractClient(joined=False)
        mover = Mover(client, self.config, self.state)
        with self.assertRaisesRegex(SyncError, "源"):
            await mover.resolve_entity("https://t.me/+Offline_Invite-ABC", role="源频道／群组")
        self.assertEqual(client.dialogs_calls, 0)
        self.assertTrue(all(isinstance(item, functions.messages.CheckChatInviteRequest)
                            for item in client._sender.requests))

    async def test_missing_private_channel_errors_identify_role_after_one_dialog_refresh(self):
        client = PeerContractClient()
        mover = Mover(client, self.config, self.state)
        with self.assertRaisesRegex(SyncError, "源"):
            await mover.resolve_entity("https://t.me/c/999999999/123", role="源频道／群组")
        self.assertEqual(client.dialogs_calls, 1)

    async def test_exact_display_title_is_usable_when_unique(self):
        client = PeerContractClient()
        client.channel.title = "唯一测试频道"
        mover = Mover(client, self.config, self.state)
        result = await mover.resolve_entity("唯一测试频道", role="源频道／群组")
        self.assertEqual(result.id, client.channel.id)
        self.assertEqual(client.dialogs_calls, 1)

    async def test_duplicate_display_titles_are_rejected_instead_of_picking_a_destination(self):
        client = PeerContractClient()
        first, second = client.channel, channel(987654321)
        first.title = second.title = "重复测试频道"
        client.dialog_entities = [first, second]
        mover = Mover(client, self.config, self.state)
        with self.assertRaisesRegex(SyncError, "目标"):
            await mover.resolve_entity("重复测试频道", role="目标频道／群组")
        self.assertEqual(client.dialogs_calls, 1)

    async def test_tg_direct_messages_link_is_not_reinterpreted_as_the_broadcast_channel(self):
        from tgmover.peers import PeerReferenceError, parse_chat_reference

        with self.assertRaises(PeerReferenceError):
            parse_chat_reference("tg://resolve?domain=sample_channel&direct")

    async def test_invalid_and_expired_invite_rpc_errors_identify_role_without_joining(self):
        for error_type in (errors.InviteHashExpiredError, errors.InviteHashInvalidError):
            with self.subTest(error=error_type.__name__):
                client = PeerContractClient()
                client.rpc_error_type = error_type
                mover = Mover(client, self.config, self.state)
                with self.assertRaisesRegex(SyncError, "源"):
                    await mover.resolve_entity("https://t.me/+Offline_Invite-ABC", role="源频道／群组")
                self.assertEqual(client.dialogs_calls, 0)
                self.assertTrue(all(isinstance(item, functions.messages.CheckChatInviteRequest)
                                    for item in client._sender.requests))

    async def test_username_invalid_and_channel_private_rpc_errors_identify_role(self):
        for error_type, peer in ((errors.UsernameInvalidError, "@sample_channel"),
                                 (errors.ChannelPrivateError, -1001234567890)):
            with self.subTest(error=error_type.__name__):
                client = PeerContractClient(cached=True)
                client.rpc_error_type = error_type
                mover = Mover(client, self.config, self.state)
                with self.assertRaisesRegex(SyncError, "目标"):
                    await mover.resolve_entity(peer, role="目标频道／群组")
                self.assertEqual(client.dialogs_calls, 0)


if __name__ == "__main__":
    unittest.main()
