"""Forum selection/preview on the real GUI worker thread, without network."""
from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_gui_worker as gui_helpers
from test_engine import channel, message
from test_forum_rpc_contract import forum, topic, topic_page
from PySide6.QtWidgets import QApplication
from telethon import functions, types, utils

from tgmover import gui_worker
from tgmover.config import Config
from tgmover.limits import LimitSettings
from tgmover.state import State


class FakeForumClient(gui_helpers.FakeTelegramClient):
    async def get_entity(self, peer):
        self.record("get_entity", peer)
        return self.owner.entity

    async def __call__(self, request):
        self.record("rpc", request)
        if isinstance(request, functions.messages.GetForumTopicsRequest):
            self.owner.request_started.set()
            try:
                while not self.owner.release_request.is_set():
                    await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                self.owner.request_cancelled.set()
                raise
            if self.owner.tamper_identity:
                self.owner.controller._identity = ("changed-session",)
            return self.owner.pages.pop(0)
        if isinstance(request, functions.messages.GetForumTopicsByIDRequest):
            return topic_page([topic(identifier) for identifier in request.topics])
        raise AssertionError(f"Unexpected forum worker RPC: {type(request).__name__}")

    async def get_messages(self, peer, *, limit, reply_to=None):
        self.record("get_messages", limit, reply_to)
        return list(sorted(self.owner.histories.get(reply_to, []), key=lambda item: item.id, reverse=True))[:limit]

    def iter_dialogs(self, **kwargs):
        self.record("iter_dialogs", kwargs)

        async def values():
            yield SimpleNamespace(entity=self.owner.entity)
            yield SimpleNamespace(entity=channel(3))
        return values()


class ForumWorkerTests(unittest.TestCase):
    wait_for = gui_helpers.GuiWorkerTests.wait_for
    perform = gui_helpers.GuiWorkerTests.perform
    dispose = gui_helpers.GuiWorkerTests.dispose

    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.config = Config(
            api_id=12345, api_hash="a" * 32, source="source", target="target",
            mode="reupload", comment_mode="reupload", sync_comments=False,
            session=self.base / "session", database=self.base / "state.sqlite3",
            download_dir=self.base / "downloads", start_id=0, poll_interval=0,
            settle_seconds=2, max_flood_wait=300, send_delay=0,
            limits=LimitSettings(enabled=False),
        )
        self.authorized, self.bot = True, False
        self.connect_started, self.connect_gate = threading.Event(), threading.Event()
        self.connect_gate.set()
        self.connect_log = ""
        self.dialog_entities = []
        self.entity = forum()
        self.pages = [topic_page([topic(1), topic(21)])]
        self.histories = {None: [message(98), message(97)], 1: [message(8)], 21: [message(23, root=21)]}
        self.request_started, self.release_request, self.request_cancelled = threading.Event(), threading.Event(), threading.Event()
        self.release_request.set()
        self.tamper_identity = False
        self.clients, self.finished, self.failures = [], [], []
        self.topic_lists, self.chat_lists, self.previews = [], [], []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        gui_helpers.TrackedLock.active = set()
        gui_helpers.TrackedLock.events = []
        self.stack.enter_context(patch.object(gui_worker, "ProcessLock", gui_helpers.TrackedLock))

        def factory(*args, **kwargs):
            client = FakeForumClient(self, *args, **kwargs)
            self.clients.append(client)
            return client

        self.stack.enter_context(patch.object(gui_worker, "TelegramClient", factory))
        self.controller = gui_worker.GuiController()
        self.controller.finished.connect(self.finished.append)
        self.controller.failed.connect(self.failures.append)
        self.controller.topics_ready.connect(self.topic_lists.append)
        self.controller.chats_ready.connect(self.chat_lists.append)
        self.controller.preview_ready.connect(self.previews.append)
        self.addCleanup(self.dispose)

    def test_topic_listing_is_read_only_and_returns_chat_identity_and_all_pages(self):
        self.pages = [topic_page([topic(1)], count=3), topic_page([topic(21), topic(22, closed=True)], count=3)]
        with patch.object(gui_worker, "State", side_effect=AssertionError("Topic listing must not open State")):
            self.assertEqual(self.perform(lambda: self.controller.list_topics(self.config, "source")), "topics")
        self.assertEqual(self.topic_lists[-1]["chat_id"], utils.get_peer_id(self.entity))
        rows = self.topic_lists[-1]["topics"]
        self.assertEqual([row["id"] for row in rows], [1, 21, 22])
        self.assertTrue(rows[-1]["closed"])
        self.assertFalse(self.config.database.exists())
        self.assertEqual(len(gui_helpers.TrackedLock.active), 1)
        self.assertTrue(all(thread == self.controller._thread.ident
                            for _, _, thread in gui_helpers.TrackedLock.events))
        client = self.clients[-1]
        self.assertFalse(any(call[0] in {"send_code_request", "sign_in"} for call in client.calls))

    def test_chat_listing_marks_forum_without_turning_each_topic_into_a_chat(self):
        self.assertEqual(self.perform(lambda: self.controller.list_chats(self.config)), "chats")
        self.assertEqual([row["forum"] for row in self.chat_lists[-1]], [True, False])
        self.assertEqual(len(self.chat_lists[-1]), 2)

    def test_nonforum_topic_request_fails_without_sending_a_topics_rpc(self):
        self.entity = channel(1, broadcast=False)
        self.assertEqual(self.perform(lambda: self.controller.list_topics(self.config, "source")), "failed")
        self.assertEqual(self.topic_lists, [])
        self.assertIn("话题", self.failures[-1])
        self.assertFalse(any(call[0] == "rpc" for call in self.clients[-1].calls))

    def test_stop_cancels_topic_read_and_does_not_publish_partial_choices(self):
        self.release_request.clear()
        self.assertTrue(self.controller.list_topics(self.config, "source"))
        self.wait_for(self.request_started.is_set)
        self.controller.stop()
        self.wait_for(lambda: self.finished[-1:] == ["stopped"])
        self.assertTrue(self.request_cancelled.is_set())
        self.assertEqual(self.topic_lists, [])
        self.assertFalse(self.config.database.exists())

    def test_changed_account_identity_does_not_publish_previous_accounts_topics(self):
        self.tamper_identity = True
        self.assertEqual(self.perform(lambda: self.controller.list_topics(self.config, "source")), "failed")
        self.assertEqual(self.topic_lists, [])
        self.assertIn("账号已改变", self.failures[-1])

    def test_preview_honors_general_and_specific_topic_without_sending_or_advancing(self):
        for identifier, expected in ((0, [98, 97]), (1, [8]), (21, [23])):
            with self.subTest(topic=identifier):
                config = replace(self.config, source_topic_id=identifier)
                self.assertEqual(self.perform(lambda: self.controller.preview(config, 5)), "preview")
                self.assertEqual([row["source_id"] for row in self.previews[-1]], expected)
                call = next(call for call in reversed(self.clients[-1].calls) if call[0] == "get_messages")
                self.assertEqual(call[2], identifier or None)
        state = State(self.config.database)
        try:
            for table in ("messages", "cursors", "downloads", "operations", "routes"):
                self.assertEqual(state.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        finally:
            state.close()


if __name__ == "__main__":
    unittest.main()
