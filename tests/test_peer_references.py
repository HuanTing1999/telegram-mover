"""User-pasted Telegram links are chat references, not usernames in disguise."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from telethon import types

import test_engine
from test_engine import FakeClient, channel
from tgmover.engine import Mover, SyncError
from tgmover.peers import PeerReferenceError, parse_chat_reference


class PeerReferenceTests(unittest.TestCase):
    def test_public_links_resolve_chat_including_post_preview_and_topic_paths(self):
        values = [
            "https://t.me/example_feed", "http://telegram.me/example_feed/",
            "t.me/example_feed/12", "https://t.me/example_feed/12?single&comment=7",
            "https://t.me/example_feed/20/30?thread=20", "https://t.me/s/example_feed/12",
            "https://www.telegram.dog/example_feed/12", "https://T.ME/example_feed/12#fragment",
            "tg://resolve?domain=example_feed&post=12",
        ]
        for value in values:
            with self.subTest(value=value):
                parsed = parse_chat_reference(value)
                self.assertEqual(parsed.peer, "@example_feed")
                self.assertEqual(parsed.kind, "username")
                self.assertFalse(parsed.allow_title)
        self.assertTrue(parse_chat_reference("t.me/example_feed/12").from_message)
        self.assertFalse(parse_chat_reference("t.me/example_feed").from_message)

    def test_private_post_ids_use_telethon_mark_formula_not_concatenation(self):
        for raw in (42, 1234567890, 12345678901):
            for suffix in ("/12", "/20/30?single"):
                with self.subTest(raw=raw, suffix=suffix):
                    parsed = parse_chat_reference(f"https://t.me/c/{raw}{suffix}")
                    self.assertEqual(parsed.peer, -(10**12 + raw))
                    self.assertEqual(parsed.kind, "id")
                    self.assertTrue(parsed.from_message)
        self.assertEqual(parse_chat_reference("tg://privatepost?channel=42&post=12").peer, -1000000000042)

    def test_numeric_strings_are_integer_ids_and_never_phones(self):
        for value in ("-1001234567890", "1234567890", "-42", -1001234567890):
            parsed = parse_chat_reference(value)
            self.assertIsInstance(parsed.peer, int)
            self.assertEqual(parsed.peer, int(value))
        for value in (0, "0", True, False, str(2**64)):
            with self.subTest(value=value), self.assertRaises(PeerReferenceError):
                parse_chat_reference(value)

    def test_invitations_preserve_case_and_do_not_contain_join_actions(self):
        for value in ("https://t.me/+Ab_cd-EF", "https://telegram.me/joinchat/Ab_cd-EF",
                      "tg://join?invite=Ab_cd-EF"):
            with self.subTest(value=value):
                parsed = parse_chat_reference(value)
                self.assertEqual(parsed.kind, "invite")
                self.assertEqual(parsed.peer, "https://t.me/+Ab_cd-EF")
                self.assertNotIn("Ab_cd-EF", repr(parsed))

    def test_exact_titles_and_internal_tl_entities_remain_distinct(self):
        title = parse_chat_reference(" 我的小组 ")
        self.assertEqual((title.peer, title.kind), ("我的小组", "title"))
        entity = types.PeerChannel(42)
        self.assertIs(parse_chat_reference(entity).peer, entity)
        self.assertTrue(parse_chat_reference("example_feed").allow_title)
        self.assertFalse(parse_chat_reference("@example_feed").allow_title)

    def test_malformed_or_non_chat_links_are_rejected_without_echoing_private_url(self):
        values = [
            "", "https://example.org/channel", "https://t.me/", "t.me/c/42",
            "t.me/c/0/12", "t.me/c/42/no_message", "t.me/c/42/12/13/14",
            "https://t.me/example_feed/not_a_post", "https://t.me/example_feed//12",
            "https://t.me/+", "https://t.me/+PrivateHash/extra", "https://t.me/joinchat/",
            "https://t.me/share/url?url=private", "https://t.me/example_feed?direct",
            "https://t.me@example.org/example_feed", "https://t.me:443/example_feed",
            "tg://resolve?domain=first&domain=second", "tg://proxy?server=secret",
        ]
        for value in values:
            with self.subTest(value=value), self.assertRaises(PeerReferenceError) as raised:
                parse_chat_reference(value)
            if value:
                self.assertNotIn(value, str(raised.exception))


class ResolveReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Reuse only the engine fixture setup, not its test methods.
        test_engine.EngineTests.setUp(self)
        self.client = FakeClient()
        self.calls = []
        self.events = []
        self.dialogs = []
        self.cache = {}

        async def get_entity(value):
            self.calls.append(("entity", value))
            if value in self.cache:
                return self.cache[value]
            raise ValueError("raw private resolver information must not be shown")

        async def get_dialogs():
            self.calls.append(("dialogs",))
            for item in self.dialogs:
                entity = item.entity
                self.cache[-(10**12 + entity.id)] = entity
            return self.dialogs

        self.client.get_entity, self.client.get_dialogs = get_entity, get_dialogs
        self.mover = Mover(self.client, self.config, self.state, on_status=self.events.append)

    async def test_full_public_post_link_resolves_parent_without_dialog_refresh(self):
        expected = channel(42)
        self.cache["@example_feed"] = expected
        result = await self.mover.resolve_entity("https://t.me/example_feed/123?single", role="源")
        self.assertIs(result, expected)
        self.assertEqual(self.calls, [("entity", "@example_feed")])
        self.assertTrue(any("起始 ID" in event["message"] for event in self.events))

    async def test_private_link_refreshes_account_cache_then_resolves_correct_channel(self):
        expected = channel(42)
        self.dialogs = [SimpleNamespace(entity=expected)]
        result = await self.mover.resolve_entity("https://t.me/c/42/123", role="源")
        self.assertIs(result, expected)
        self.assertEqual(self.calls, [("entity", -1000000000042), ("dialogs",), ("entity", -1000000000042)])

    async def test_inaccessible_private_link_reports_correct_field_and_permission(self):
        with self.assertRaises(SyncError) as raised:
            await self.mover.resolve_entity("https://t.me/c/42/123", role="目标")
        self.assertIn("目标", str(raised.exception))
        self.assertIn("访问权限", str(raised.exception))
        self.assertNotIn("raw private", str(raised.exception))

    async def test_unjoined_invite_does_not_refresh_all_dialogs_or_auto_join(self):
        with self.assertRaises(SyncError) as raised:
            await self.mover.resolve_entity("https://t.me/+PrivateHash", role="源")
        self.assertIn("不会自动入群", str(raised.exception))
        self.assertEqual(self.calls, [("entity", "https://t.me/+PrivateHash")])
        self.assertNotIn("PrivateHash", str(raised.exception))

    async def test_exact_duplicate_titles_require_explicit_selection(self):
        first, second = channel(42), channel(43)
        first.title = second.title = "相同的名字"
        self.dialogs = [SimpleNamespace(entity=first), SimpleNamespace(entity=second)]
        with self.assertRaises(SyncError) as raised:
            await self.mover.resolve_entity("相同的名字", role="源")
        self.assertIn("多个同名", str(raised.exception))
        self.assertEqual(self.calls, [("dialogs",)])

    async def test_unique_title_resolves_only_a_chat_and_not_a_user(self):
        expected = channel(42)
        expected.title = "我的频道"
        self.dialogs = [SimpleNamespace(entity=types.User(id=1, first_name="我的频道")),
                        SimpleNamespace(entity=expected)]
        self.assertIs(await self.mover.resolve_entity("我的频道", role="源"), expected)


if __name__ == "__main__":
    unittest.main()
