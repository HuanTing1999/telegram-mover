"""Real Qt signals and a background asyncio loop; all Telegram calls are fake."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from telethon import errors, types, utils

from test_engine import FakeClient as EngineFakeClient, channel, message
from tgmover.config import Config
from tgmover.engine import UserStopped
from tgmover.filters import FilterSettings
from tgmover.limits import LimitSettings
from tgmover.state import State
from tgmover import gui_worker


class TrackedState(State):
    instances = []

    def __init__(self, path):
        self.created_thread = threading.get_ident()
        self.closed = False
        super().__init__(path)
        self.instances.append(self)

    def close(self):
        self.closed_thread = threading.get_ident()
        self.closed = True
        super().close()


class TrackedLock:
    active = set()
    events = []

    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        if self.path in self.active:
            raise AssertionError("A session or database lock was acquired twice")
        self.active.add(self.path)
        self.events.append(("enter", self.path, threading.get_ident()))
        return self

    def __exit__(self, *_):
        self.active.remove(self.path)
        self.events.append(("exit", self.path, threading.get_ident()))


class FakeTelegramClient:
    def __init__(self, owner, session, api_id, api_hash, **kwargs):
        self.owner = owner
        self.session = session
        self.api_hash = api_hash
        self.thread_id = threading.get_ident()
        self.authorized = owner.authorized
        self.bot = owner.bot
        self.connected = False
        self.disconnected = False
        self.calls = []
        self.sign_in_errors = []
        self.messages = []
        self.dialogs = list(owner.dialog_entities)

    def record(self, name, *args):
        if threading.get_ident() != self.thread_id:
            raise AssertionError("A Telegram client crossed thread boundaries")
        self.calls.append((name, *args))

    async def connect(self):
        self.record("connect")
        self.owner.connect_started.set()
        while not self.owner.connect_gate.is_set():
            await asyncio.sleep(0.005)
        self.connected = True
        if self.owner.connect_log:
            logging.getLogger("tgmover.worker_test").warning(self.owner.connect_log)

    async def disconnect(self):
        self.record("disconnect")
        self.disconnected = True

    async def is_user_authorized(self):
        self.record("is_user_authorized")
        return self.authorized

    async def get_me(self):
        self.record("get_me")
        return SimpleNamespace(id=42, bot=self.bot, first_name="Test", last_name="User",
                               username="tester", phone="8613800000000")

    async def send_code_request(self, phone):
        self.record("send_code_request", phone)
        return SimpleNamespace(phone_code_hash="private-challenge-hash")

    async def sign_in(self, **kwargs):
        self.record("sign_in", kwargs)
        if self.sign_in_errors:
            raise self.sign_in_errors.pop(0)
        self.authorized = True

    async def get_entity(self, peer):
        self.record("get_entity", peer)
        return channel(1)

    async def get_messages(self, peer, *, limit):
        self.record("get_messages", limit)
        return sorted(self.messages, key=lambda item: item.id, reverse=True)[:limit]

    def iter_dialogs(self, **kwargs):
        self.record("iter_dialogs", kwargs)

        async def iterator():
            for dialog in self.dialogs:
                self.record("dialog_item", getattr(dialog.entity, "id", 0))
                yield dialog

        return iterator()

    async def send_message(self, *args, **kwargs):
        raise AssertionError("Preview must never send messages")

    async def send_file(self, *args, **kwargs):
        raise AssertionError("Preview must never upload files")

    async def download_media(self, *args, **kwargs):
        raise AssertionError("Preview must never download media")


class GuiWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.main_thread = threading.get_ident()
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.config = Config(
            api_id=12345, api_hash="a" * 32, source="source", target="target",
            mode="reupload", comment_mode="reupload", sync_comments=False,
            session=self.base / "user", database=self.base / "state.sqlite3",
            download_dir=self.base / "downloads", start_id=0, poll_interval=0,
            settle_seconds=2, max_flood_wait=300, send_delay=0,
            limits=LimitSettings(min_send_interval=0, min_request_interval=0,
                                 max_messages_per_minute=0, max_messages_per_hour=0,
                                 max_messages_per_day=0, max_messages_per_run=0,
                                 max_runtime_minutes=0),
            filters=FilterSettings(keywords=("广告合作",)),
        )
        self.authorized, self.bot = True, False
        self.clients = []
        self.connect_started = threading.Event()
        self.connect_gate = threading.Event()
        self.connect_gate.set()
        self.connect_log = ""
        self.dialog_entities = []
        self.finished, self.failures, self.users = [], [], []
        self.ended_challenges = []
        self.password_prompts = []
        self.previews, self.progress, self.logs, self.busy_values = [], [], [], []
        self.phases = []
        self.chat_lists = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        TrackedState.instances = []
        TrackedLock.active = set()
        TrackedLock.events = []
        self.stack.enter_context(patch.object(gui_worker, "TelegramClient", self.client_factory))
        self.stack.enter_context(patch.object(gui_worker, "ProcessLock", TrackedLock))
        self.stack.enter_context(patch.object(gui_worker, "State", TrackedState))
        self.controller = gui_worker.GuiController()
        self.controller.finished.connect(self.finished.append)
        self.controller.failed.connect(self.failures.append)
        self.controller.authenticated.connect(self.users.append)
        self.controller.challenge_ended.connect(lambda: self.ended_challenges.append(True))
        self.controller.password_required.connect(self.password_prompts.append)
        self.controller.preview_ready.connect(self.previews.append)
        self.controller.chats_ready.connect(self.chat_lists.append)
        self.controller.stats.connect(self.progress.append)
        self.controller.task_phase.connect(self.phases.append)
        self.controller.log.connect(self.logs.append)
        self.controller.busy_changed.connect(self.busy_values.append)
        self.addCleanup(self.dispose)

    def client_factory(self, *args, **kwargs):
        client = FakeTelegramClient(self, *args, **kwargs)
        self.clients.append(client)
        return client

    def wait_for(self, condition, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if condition():
                return
            time.sleep(0.005)
        self.fail("The background operation did not finish within the test timeout")

    def perform(self, call):
        count = len(self.finished)
        self.assertTrue(call())
        self.wait_for(lambda: len(self.finished) > count and not self.controller.busy)
        return self.finished[-1]

    def dispose(self):
        self.connect_gate.set()
        self.controller.shutdown()
        self.wait_for(lambda: "closed" in self.finished)
        self.controller._thread.join(timeout=2)
        self.assertFalse(self.controller._thread.is_alive())
        self.assertEqual(TrackedLock.active, set())

    def connect(self):
        self.assertEqual(self.perform(lambda: self.controller.connect_account(self.config)), "authenticated")
        return self.clients[-1]

    def fake_mover(self, *, wait_in_limiter=False):
        owner = self
        owner.run_started = threading.Event()
        owner.release_batch = threading.Event()
        owner.batch_committed = threading.Event()
        owner.movers = []

        class FakeMover:
            def __init__(self, client, config, state, *, limiter, should_stop, on_progress, on_status):
                self.client, self.config, self.state = client, config, state
                self.limiter, self.should_stop, self.on_progress = limiter, should_stop, on_progress
                owner.movers.append(self)
                self.summary = {"message": "本轮完成：发送 2 条。", "counts": {"sent": 2}}

            def waiting(self, event):
                pass

            async def prepare(self):
                self.prepared = True

            async def run(self):
                await self.prepare()
                owner.run_started.set()
                if wait_in_limiter:
                    await self.limiter.sleep(120)
                else:
                    while not owner.release_batch.is_set():
                        await asyncio.sleep(0.005)
                    # A safe batch boundary is reached only after a durable save.
                    owner.batch_committed.set()
                    self.on_progress({"processed": 2, "sent": 2, "filtered": 0, "files": 2})
                if self.should_stop():
                    raise UserStopped("stopped at committed boundary")

        self.stack.enter_context(patch.object(gui_worker, "Mover", FakeMover))

    def use_real_engine(self, items):
        owner = self

        class OfflineEngineClient(EngineFakeClient):
            def __init__(self, *_args, **_kwargs):
                super().__init__()
                self.thread_id = threading.get_ident()
                self.disconnected = False
                self.add(1, items)
                owner.clients.append(self)

            async def connect(self):
                self.calls.append(("connect",))

            async def disconnect(self):
                self.calls.append(("disconnect",))
                self.disconnected = True

            async def is_user_authorized(self):
                return True

            async def get_me(self):
                return SimpleNamespace(id=42, bot=False, first_name="Test", last_name="User", username="tester")

        self.stack.enter_context(patch.object(gui_worker, "TelegramClient", OfflineEngineClient))

    def test_constructor_never_connects_automatically(self):
        self.application.processEvents()
        self.assertEqual(self.clients, [])
        self.assertEqual(TrackedLock.active, set())

    def test_list_chats_includes_archived_channels_groups_and_excludes_users_without_state(self):
        public = channel(11)
        public.title, public.username = "Public channel", "public_channel"
        archived = channel(12, broadcast=False)
        archived.title = "Archived group"
        basic = types.Chat(id=13, title="Basic group", photo=types.ChatPhotoEmpty(),
                           participants_count=2, date=None, version=1)
        person = types.User(id=14, first_name="Personal chat")
        self.dialog_entities = [SimpleNamespace(entity=public, archived=False),
                                SimpleNamespace(entity=person, archived=False),
                                SimpleNamespace(entity=archived, archived=True),
                                SimpleNamespace(entity=basic, archived=True),
                                SimpleNamespace(entity=public, archived=False)]
        self.assertEqual(self.perform(lambda: self.controller.list_chats(self.config)), "chats")
        self.assertEqual(self.chat_lists, [[
            {"id": utils.get_peer_id(public), "title": "Public channel", "username": "public_channel", "forum": False},
            {"id": utils.get_peer_id(archived), "title": "Archived group", "username": "", "forum": False},
            {"id": utils.get_peer_id(basic), "title": "Basic group", "username": "", "forum": False},
        ]])
        client = self.clients[-1]
        self.assertNotEqual(client.thread_id, self.main_thread)
        self.assertIn(("iter_dialogs", {}), client.calls)
        self.assertFalse(any(call[0] in {"send_code_request", "sign_in"} for call in client.calls))
        self.assertEqual(TrackedState.instances, [])
        self.assertFalse(self.config.database.exists())
        self.assertEqual(len(TrackedLock.active), 1)  # The user session lock stays held.

    def test_list_chats_requires_login_without_requesting_codes_or_dialogs(self):
        self.authorized = False
        self.assertEqual(self.perform(lambda: self.controller.list_chats(self.config)), "failed")
        self.assertIn("先", self.failures[-1])
        self.assertEqual(self.chat_lists, [])
        self.assertFalse(any(call[0] in {"iter_dialogs", "send_code_request", "sign_in"}
                             for call in self.clients[-1].calls))
        self.assertEqual(TrackedState.instances, [])

    def hanging_chats(self):
        client = self.connect()
        self.dialog_started, self.dialog_cancelled = threading.Event(), threading.Event()

        def iter_dialogs():
            client.record("iter_dialogs", {})

            async def iterator():
                yield SimpleNamespace(entity=channel(11))
                self.dialog_started.set()
                try:
                    await asyncio.sleep(3600)
                finally:
                    self.dialog_cancelled.set()

            return iterator()

        client.iter_dialogs = iter_dialogs
        return client

    def test_list_chats_page_read_can_stop_without_publishing_partial_old_results(self):
        self.hanging_chats()
        count = len(self.finished)
        self.assertTrue(self.controller.list_chats(self.config))
        self.wait_for(self.dialog_started.is_set)
        self.controller.stop()
        self.wait_for(lambda: len(self.finished) > count and not self.controller.busy)
        self.assertEqual(self.finished[-1], "stopped")
        self.assertTrue(self.dialog_cancelled.is_set())
        self.assertEqual(self.chat_lists, [])
        self.assertEqual(TrackedState.instances, [])

    def test_list_chats_page_timeout_reports_step_and_never_creates_mapping_database(self):
        self.hanging_chats()
        with patch("tgmover.engine.READ_TIMEOUT_SECONDS", 0.12):
            self.assertEqual(self.perform(lambda: self.controller.list_chats(self.config)), "failed")
        self.assertTrue(self.dialog_cancelled.is_set())
        self.assertIn("读取频道／群组列表", self.failures[-1])
        self.assertEqual(self.chat_lists, [])
        self.assertEqual(TrackedState.instances, [])
        self.assertFalse(self.config.database.exists())

    def test_list_chats_busy_gate_rejects_session_switch(self):
        previous = self.hanging_chats()
        identity = self.controller._identity
        count = len(self.finished)
        self.assertTrue(self.controller.list_chats(self.config))
        self.wait_for(self.dialog_started.is_set)
        another = replace(self.config, session=self.base / "another-account")
        self.assertFalse(self.controller.connect_account(another))
        self.assertIs(self.controller._client, previous)
        self.assertEqual(self.controller._identity, identity)
        self.assertEqual(len(self.clients), 1)
        self.controller.stop()
        self.wait_for(lambda: len(self.finished) > count and not self.controller.busy)
        self.assertEqual(self.chat_lists, [])

    def test_list_chats_after_session_switch_never_reuses_previous_account_list(self):
        self.dialog_entities = [SimpleNamespace(entity=channel(11))]
        self.assertEqual(self.perform(lambda: self.controller.list_chats(self.config)), "chats")
        previous = self.clients[-1]
        self.dialog_entities = [SimpleNamespace(entity=channel(22, broadcast=False))]
        another = replace(self.config, session=self.base / "another-account")
        self.assertEqual(self.perform(lambda: self.controller.list_chats(another)), "chats")
        self.assertTrue(previous.disconnected)
        self.assertEqual(self.chat_lists[0][0]["id"], utils.get_peer_id(channel(11)))
        self.assertEqual(self.chat_lists[1][0]["id"], utils.get_peer_id(channel(22)))
        self.assertEqual(TrackedState.instances, [])

    def test_list_chats_initial_connection_can_stop_without_automatic_login(self):
        self.connect_gate.clear()
        count = len(self.finished)
        self.assertTrue(self.controller.list_chats(self.config))
        self.wait_for(self.connect_started.is_set)
        self.controller.stop()
        self.wait_for(lambda: len(self.finished) > count and not self.controller.busy)
        self.assertEqual(self.finished[-1], "stopped")
        self.assertEqual(self.chat_lists, [])
        self.assertFalse(any(call[0] in {"send_code_request", "sign_in", "iter_dialogs"}
                             for call in self.clients[-1].calls))
        self.assertEqual(TrackedState.instances, [])
        self.assertEqual(TrackedLock.active, set())

    def test_existing_user_session_is_reused_and_client_stays_on_worker_thread(self):
        client = self.connect()

        self.assertNotEqual(client.thread_id, self.main_thread)
        self.assertEqual(self.users[-1], {"id": 42, "name": "Test User", "username": "tester"})
        self.assertFalse(any(name == "send_code_request" for name, *_ in client.calls))
        self.assertFalse(client.disconnected)
        self.assertEqual(len(TrackedLock.active), 1)

    def test_phone_and_code_flow_is_noninteractive_and_does_not_emit_phone(self):
        self.authorized = False
        code_messages = []
        self.controller.code_sent.connect(code_messages.append)
        self.assertEqual(self.perform(lambda: self.controller.request_code(self.config, "+8613800000000")), "code_sent")
        client = self.clients[-1]
        self.assertEqual(self.perform(lambda: self.controller.submit_code("12345")), "authenticated")

        self.assertEqual([call[1] for call in client.calls if call[0] == "send_code_request"], ["+8613800000000"])
        call = next(call[1] for call in client.calls if call[0] == "sign_in")
        self.assertEqual(call, {"phone": "+8613800000000", "code": "12345", "phone_code_hash": "private-challenge-hash"})
        self.assertEqual(len(code_messages), 1)
        self.assertNotIn("8613800000000", code_messages[0])
        self.assertNotIn("phone", self.users[-1])

    def test_invalid_phone_does_not_create_a_client_or_request_a_code(self):
        self.assertEqual(self.perform(lambda: self.controller.request_code(self.config, "12345:token")), "failed")
        self.assertEqual(self.clients, [])
        self.assertIn("手机号格式无效", self.failures[-1])

    def test_global_numbers_are_normalized_without_changing_country(self):
        self.authorized = False
        for raw, canonical in (("+63 917 123 4567", "+639171234567"),
                               ("+44 (7700) 900-123", "+447700900123"),
                               ("+7 912 345 67 89", "+79123456789"),
                               ("001 202-555-0123", "+12025550123"),
                               ("+49 151 23456789", "+4915123456789")):
            with self.subTest(country=canonical[:3]):
                self.assertEqual(self.perform(lambda: self.controller.request_code(self.config, raw)), "code_sent")
                self.assertEqual(self.clients[-1].calls[-1], ("send_code_request", canonical))
                self.assertEqual(self.controller._phone, canonical)

    def waiting_code_client(self):
        self.authorized = False
        self.assertEqual(self.perform(lambda: self.controller.connect_account(self.config)), "connected")
        client = self.clients[-1]
        self.code_started = threading.Event()
        self.code_cancelled = threading.Event()

        async def wait_for_code(phone):
            client.record("send_code_request", phone)
            self.code_started.set()
            try:
                await asyncio.sleep(3600)
            finally:
                self.code_cancelled.set()
        client.send_code_request = wait_for_code
        return client

    def test_get_code_timeout_releases_buttons_and_session_without_resending(self):
        client = self.waiting_code_client()
        with patch.object(gui_worker, "AUTH_TIMEOUT_SECONDS", 0.12):
            self.assertEqual(self.perform(lambda: self.controller.request_code(self.config, "+639171234567")), "failed")
        self.assertTrue(self.code_cancelled.is_set())
        self.assertTrue(client.disconnected)
        self.assertIsNone(self.controller._phone_code_hash)
        self.assertIn("超时", self.failures[-1])
        self.assertEqual(TrackedLock.active, set())
        self.assertEqual(sum(call[0] == "send_code_request" for call in client.calls), 1)
        self.assertEqual(len(self.ended_challenges), 1)

    def test_get_code_can_be_stopped_while_waiting_for_telegram(self):
        client = self.waiting_code_client()
        previous = len(self.finished)
        self.assertTrue(self.controller.request_code(self.config, "+639171234567"))
        self.wait_for(self.code_started.is_set)
        self.controller.stop()
        self.wait_for(lambda: len(self.finished) > previous and not self.controller.busy)
        self.assertEqual(self.finished[-1], "stopped")
        self.assertTrue(self.code_cancelled.is_set())
        self.assertTrue(client.disconnected)
        self.assertIsNone(self.controller._phone_code_hash)
        self.assertEqual(TrackedLock.active, set())
        self.assertEqual(sum(call[0] == "send_code_request" for call in client.calls), 1)

    def test_disconnected_code_request_never_reports_authenticated_or_code_sent(self):
        self.authorized = False
        self.assertEqual(self.perform(lambda: self.controller.connect_account(self.config)), "connected")
        client = self.clients[-1]

        async def disconnected(phone):
            client.record("send_code_request", phone)
            raise asyncio.CancelledError()
        client.send_code_request = disconnected
        self.assertEqual(self.perform(lambda: self.controller.request_code(self.config, "+639171234567")), "failed")
        self.assertEqual(self.users, [])
        self.assertIn("连接已中断", self.failures[-1])
        self.assertTrue(client.disconnected)
        self.assertEqual(TrackedLock.active, set())

    def test_sign_in_timeout_clears_challenge_without_replaying_code(self):
        self.authorized = False
        self.assertEqual(self.perform(lambda: self.controller.request_code(self.config, "+639171234567")), "code_sent")
        client = self.clients[-1]

        async def waiting_sign_in(**kwargs):
            client.record("sign_in", kwargs)
            await asyncio.sleep(3600)
        client.sign_in = waiting_sign_in
        with patch.object(gui_worker, "AUTH_TIMEOUT_SECONDS", 0.12):
            self.assertEqual(self.perform(lambda: self.controller.submit_code("12 345")), "failed")
        call = next(call[1] for call in client.calls if call[0] == "sign_in")
        self.assertEqual(call["code"], "12345")
        self.assertEqual(sum(call[0] == "sign_in" for call in client.calls), 1)
        self.assertEqual(self.users, [])
        self.assertIsNone(self.controller._phone_code_hash)
        self.assertTrue(client.disconnected)
        self.assertIn("超时", self.failures[-1])

    def test_network_failure_is_not_retried_and_releases_login_connection(self):
        self.authorized = False
        self.assertEqual(self.perform(lambda: self.controller.connect_account(self.config)), "connected")
        client = self.clients[-1]

        async def failed_request(phone):
            client.record("send_code_request", phone)
            raise ConnectionError("offline test")
        client.send_code_request = failed_request
        self.assertEqual(self.perform(lambda: self.controller.request_code(self.config, "+639171234567")), "failed")
        self.assertEqual(sum(call[0] == "send_code_request" for call in client.calls), 1)
        self.assertTrue(client.disconnected)
        self.assertIsNone(self.controller._phone_code_hash)
        self.assertEqual(TrackedLock.active, set())

    def test_bot_session_is_rejected_and_disconnected(self):
        self.bot = True
        self.assertEqual(self.perform(lambda: self.controller.connect_account(self.config)), "failed")
        self.assertIn("机器人", self.failures[-1])
        self.assertEqual(self.users, [])
        self.assertTrue(self.clients[-1].disconnected)
        self.assertEqual(TrackedLock.active, set())

    def test_three_invalid_codes_stop_challenge_without_resending(self):
        self.authorized = False
        self.perform(lambda: self.controller.request_code(self.config, "+8613800000000"))
        client = self.clients[-1]
        client.sign_in_errors = [errors.PhoneCodeInvalidError(None) for _ in range(3)]
        for attempt in range(4):
            self.assertEqual(self.perform(lambda: self.controller.submit_code("00000")), "failed")
            self.assertEqual(len(self.ended_challenges), int(attempt >= 2))
        self.assertEqual(sum(call[0] == "sign_in" for call in client.calls), 3)
        self.assertEqual(sum(call[0] == "send_code_request" for call in client.calls), 1)

    def test_expired_code_ends_challenge_once_and_allows_explicit_new_request(self):
        self.authorized = False
        self.perform(lambda: self.controller.request_code(self.config, "+8613800000000"))
        client = self.clients[-1]
        client.sign_in_errors = [errors.PhoneCodeExpiredError(None)]
        self.assertEqual(self.perform(lambda: self.controller.submit_code("12345")), "failed")
        self.assertEqual(len(self.ended_challenges), 1)
        self.assertIn("过期", self.failures[-1])
        self.assertIsNone(self.controller._phone_code_hash)
        self.assertEqual(sum(call[0] == "send_code_request" for call in client.calls), 1)
        self.assertEqual(self.perform(lambda: self.controller.request_code(self.config, "+8613800000000")), "code_sent")
        self.assertEqual(sum(call[0] == "send_code_request" for call in client.calls), 2)
        self.assertEqual(self.perform(lambda: self.controller.submit_code("12345")), "authenticated")

    def test_empty_code_does_not_trigger_telethon_implicit_resend(self):
        self.authorized = False
        self.perform(lambda: self.controller.request_code(self.config, "+8613800000000"))
        client = self.clients[-1]
        self.perform(lambda: self.controller.submit_code(""))
        self.assertEqual(self.ended_challenges, [])
        self.assertFalse(any(call[0] == "sign_in" for call in client.calls))
        self.assertEqual(sum(call[0] == "send_code_request" for call in client.calls), 1)

    def require_password(self):
        self.authorized = False
        self.perform(lambda: self.controller.request_code(self.config, "+8613800000000"))
        client = self.clients[-1]
        client.sign_in_errors = [errors.SessionPasswordNeededError(None)]
        self.assertEqual(self.perform(lambda: self.controller.submit_code("12345")), "password_required")
        return client

    def test_2fa_transitions_to_password_step_without_losing_session_or_resending(self):
        client = self.require_password()
        self.assertTrue(self.controller._password_pending)
        self.assertEqual(self.failures, [])
        self.assertEqual(self.users, [])
        self.assertEqual(len(self.password_prompts), 1)
        self.assertIn("两步验证", self.password_prompts[0])
        self.assertEqual(self.ended_challenges, [])
        self.assertFalse(client.disconnected)
        self.assertEqual(len(TrackedLock.active), 1)
        self.perform(lambda: self.controller.submit_code("12345"))
        self.assertEqual(sum(call[0] == "sign_in" for call in client.calls), 1)
        self.assertEqual(sum(call[0] == "send_code_request" for call in client.calls), 1)

    def test_password_success_preserves_spaces_and_does_not_retain_password(self):
        client = self.require_password()
        password = "  private-Ω-password  "
        self.assertEqual(self.perform(lambda: self.controller.submit_password(password)), "authenticated")
        self.assertEqual([call[1] for call in client.calls if call[0] == "sign_in"][-1], {"password": password})
        self.assertEqual(len(self.users), 1)
        self.assertFalse(self.controller._password_pending)
        self.assertIsNone(self.controller._phone_code_hash)
        self.assertNotIn(password, self.controller._secrets)
        self.assertNotIn(password, " ".join(self.logs + self.failures + self.password_prompts))

    def test_wrong_password_retains_challenge_and_can_succeed_without_new_code(self):
        client = self.require_password()
        client.sign_in_errors = [errors.PasswordHashInvalidError(None)]
        self.assertEqual(self.perform(lambda: self.controller.submit_password("wrong")), "failed")
        self.assertTrue(self.controller._password_pending)
        self.assertEqual(self.ended_challenges, [])
        self.assertIn("还可尝试 2", self.failures[-1])
        self.assertEqual(self.perform(lambda: self.controller.submit_password("correct")), "authenticated")
        self.assertEqual(sum(call[0] == "send_code_request" for call in client.calls), 1)
        self.assertEqual(len(self.password_prompts), 1)

    def test_three_wrong_passwords_end_challenge_and_release_session(self):
        client = self.require_password()
        client.sign_in_errors = [errors.PasswordHashInvalidError(None) for _ in range(3)]
        for attempt in range(3):
            self.assertEqual(self.perform(lambda: self.controller.submit_password("wrong")), "failed")
            self.assertEqual(self.controller._password_pending, attempt < 2)
        self.assertIn("连续 3 次", self.failures[-1])
        self.assertTrue(client.disconnected)
        self.assertEqual(TrackedLock.active, set())
        self.assertEqual(len(self.ended_challenges), 1)
        previous = len(client.calls)
        self.assertEqual(self.perform(lambda: self.controller.submit_password("correct")), "failed")
        self.assertEqual(len(client.calls), previous)

    def test_empty_password_stays_local_and_all_spaces_are_preserved(self):
        client = self.require_password()
        calls = len(client.calls)
        self.assertEqual(self.perform(lambda: self.controller.submit_password("")), "failed")
        self.assertEqual(len(client.calls), calls)
        self.assertTrue(self.controller._password_pending)
        self.assertEqual(self.perform(lambda: self.controller.submit_password("   ")), "authenticated")
        self.assertEqual([call[1] for call in client.calls if call[0] == "sign_in"][-1], {"password": "   "})

    def test_password_without_server_challenge_never_calls_sign_in(self):
        client = self.connect()
        calls = len(client.calls)
        self.assertEqual(self.perform(lambda: self.controller.submit_password("secret")), "failed")
        self.assertEqual(len(client.calls), calls)
        self.assertNotIn("secret", self.failures[-1])

    def test_expired_srp_parameters_allow_password_retry_without_counting_wrong_password(self):
        client = self.require_password()
        client.sign_in_errors = [errors.SrpIdInvalidError(None), errors.SrpPasswordChangedError(None)]
        for _ in range(2):
            self.assertEqual(self.perform(lambda: self.controller.submit_password("current")), "failed")
            self.assertTrue(self.controller._password_pending)
            self.assertEqual(self.controller._password_attempts, 0)
            self.assertIn("参数已更新", self.failures[-1])
        self.assertEqual(self.perform(lambda: self.controller.submit_password("current")), "authenticated")
        self.assertEqual(sum(call[0] == "send_code_request" for call in client.calls), 1)

    def test_cancel_password_login_clears_challenge_without_logging_out_or_deleting_session(self):
        client = self.require_password()
        self.assertEqual(self.perform(self.controller.cancel_authentication), "auth_cancelled")
        self.assertFalse(self.controller._password_pending)
        self.assertIsNone(self.controller._phone)
        self.assertTrue(client.disconnected)
        self.assertEqual(TrackedLock.active, set())
        self.assertEqual(len(self.ended_challenges), 1)
        self.assertEqual(self.perform(lambda: self.controller.submit_password("secret")), "failed")

    def test_password_timeout_cleans_challenge_and_secret(self):
        client = self.require_password()
        async def wait_password(**kwargs):
            client.record("sign_in", kwargs)
            await asyncio.sleep(3600)
        client.sign_in = wait_password
        with patch.object(gui_worker, "AUTH_TIMEOUT_SECONDS", 0.12):
            self.assertEqual(self.perform(lambda: self.controller.submit_password("private-timeout")), "failed")
        self.assertIn("超时", self.failures[-1])
        self.assertNotIn("private-timeout", self.controller._secrets)
        self.assertFalse(self.controller._password_pending)
        self.assertTrue(client.disconnected)
        self.assertEqual(TrackedLock.active, set())

    def test_cancelled_password_rpc_is_not_authenticated_and_cleans_secret(self):
        client = self.require_password()
        async def cancelled(**kwargs):
            raise asyncio.CancelledError()
        client.sign_in = cancelled
        self.assertEqual(self.perform(lambda: self.controller.submit_password("private-cancelled")), "failed")
        self.assertEqual(self.users, [])
        self.assertFalse(self.controller._password_pending)
        self.assertNotIn("private-cancelled", self.controller._secrets)
        self.assertEqual(TrackedLock.active, set())

    def test_password_is_redacted_from_worker_logs_while_in_flight(self):
        client = self.require_password()
        async def logged(**kwargs):
            logging.getLogger("tgmover.worker_test").warning("password=%s", kwargs["password"])
            client.authorized = True
        client.sign_in = logged
        self.assertEqual(self.perform(lambda: self.controller.submit_password("private log password")), "authenticated")
        self.assertTrue(self.logs)
        self.assertNotIn("private log password", " ".join(self.logs))
        self.assertIn("已隐藏", " ".join(self.logs))

    def test_changing_session_invalidates_previous_password_challenge(self):
        previous = self.require_password()
        self.authorized = True
        another = replace(self.config, session=self.base / "other-user")
        self.assertEqual(self.perform(lambda: self.controller.connect_account(another)), "authenticated")
        active = self.clients[-1]
        calls = len(active.calls)
        self.assertEqual(self.perform(lambda: self.controller.submit_password("old-password")), "failed")
        self.assertEqual(len(active.calls), calls)
        self.assertTrue(previous.disconnected)

    def test_concurrent_operations_are_rejected_without_blocking_gui(self):
        self.connect_gate.clear()
        self.assertTrue(self.controller.connect_account(self.config))
        self.wait_for(self.connect_started.is_set)
        self.assertTrue(self.controller.busy)
        self.assertFalse(self.controller.request_code(self.config, "+8613800000000"))
        self.application.processEvents()
        self.assertTrue(self.controller.busy)
        self.connect_gate.set()
        self.wait_for(lambda: "authenticated" in self.finished)
        self.assertEqual(len(self.clients), 1)

    def test_changing_session_or_credentials_disconnects_previous_client(self):
        first = self.connect()
        changed = replace(self.config, api_hash="b" * 32, session=self.base / "second-user")
        self.assertEqual(self.perform(lambda: self.controller.connect_account(changed)), "authenticated")
        self.assertTrue(first.disconnected)
        self.assertEqual(len(self.clients), 2)
        self.assertEqual(self.clients[-1].api_hash, "b" * 32)
        self.assertEqual(len(TrackedLock.active), 1)

    def test_run_uses_worker_thread_state_and_does_not_reacquire_session_lock(self):
        self.connect()
        self.fake_mover()
        self.release_batch.set()

        self.assertEqual(self.perform(lambda: self.controller.run_task(self.config)), "completed")

        state = TrackedState.instances[-1]
        self.assertNotEqual(state.created_thread, self.main_thread)
        self.assertEqual(state.created_thread, state.closed_thread)
        self.assertTrue(state.closed)
        self.assertEqual(self.progress[-1]["sent"], 2)
        self.assertEqual(sum(".mover.lock" in str(path) for action, path, _ in TrackedLock.events if action == "enter"), 1)
        self.assertEqual(sum("state.sqlite3.lock" in str(path) for action, path, _ in TrackedLock.events if action == "enter"), 1)

    def test_check_only_prepares_without_running_or_sending(self):
        self.connect()
        self.fake_mover()
        self.assertEqual(self.perform(lambda: self.controller.run_task(self.config, check_only=True)), "checked")
        self.assertTrue(self.movers[-1].prepared)
        self.assertFalse(self.run_started.is_set())
        self.assertTrue(TrackedState.instances[-1].closed)

    def test_stop_does_not_cancel_current_batch_and_shutdown_waits_for_commit(self):
        client = self.connect()
        self.fake_mover()
        self.assertTrue(self.controller.run_task(self.config))
        self.wait_for(self.run_started.is_set)
        self.controller.shutdown()
        self.application.processEvents()
        self.assertTrue(self.controller.busy)
        self.assertFalse(client.disconnected)
        self.assertFalse(self.batch_committed.is_set())
        self.release_batch.set()
        self.wait_for(lambda: "closed" in self.finished)
        self.assertTrue(self.batch_committed.is_set())
        self.assertTrue(TrackedState.instances[-1].closed)
        self.assertTrue(client.disconnected)

    def test_stop_interrupts_limiter_wait_without_cancelling_a_send(self):
        self.connect()
        self.fake_mover(wait_in_limiter=True)
        self.assertTrue(self.controller.run_task(self.config))
        self.wait_for(self.run_started.is_set)
        began = time.monotonic()
        self.controller.stop()
        self.wait_for(lambda: self.finished[-1:] == ["stopped"])
        self.assertLess(time.monotonic() - began, 1)
        self.assertTrue(TrackedState.instances[-1].closed)

    def test_close_racing_with_task_completion_still_disconnects(self):
        client = self.connect()
        self.fake_mover()
        reached_completion = threading.Event()
        continue_completion = threading.Event()
        original_gate = self.controller._gate
        owner = self

        class CompletionBarrierGate:
            fired = False

            def __enter__(self):
                original_gate.acquire()
                return self

            def __exit__(self, *_):
                original_gate.release()
                # Pause the worker immediately after its completion-state
                # transaction, allowing a real main-thread shutdown to race it.
                if (not self.fired and threading.get_ident() == owner.controller._thread.ident
                        and owner.batch_committed.is_set()):
                    self.fired = True
                    reached_completion.set()
                    continue_completion.wait(timeout=2)

        self.controller._gate = CompletionBarrierGate()
        self.assertTrue(self.controller.run_task(self.config))
        self.wait_for(self.run_started.is_set)
        self.release_batch.set()
        self.wait_for(reached_completion.is_set)
        self.controller.shutdown()
        continue_completion.set()

        self.wait_for(lambda: "closed" in self.finished)
        self.assertTrue(client.disconnected)
        self.assertTrue(TrackedState.instances[-1].closed)
        self.assertEqual(TrackedLock.active, set())

    def test_preview_evaluates_whole_boundary_album_without_writing_progress(self):
        client = self.connect()
        client.messages = [message(1, text="album ordinary", grouped_id=77),
                           message(2, text="广告合作", grouped_id=77), message(3, text="latest")]
        self.assertEqual(self.perform(lambda: self.controller.preview(self.config, limit=2)), "preview")
        rows = self.previews[-1]
        self.assertEqual([row["source_id"] for row in rows], [3, 2, 1])
        self.assertEqual([row["blocked"] for row in rows], [False, True, True])
        self.assertTrue(rows[-1]["reasons"])
        self.assertTrue(TrackedState.instances[-1].closed)
        state = State(self.config.database)
        try:
            for table in ("messages", "cursors", "downloads", "operations"):
                self.assertEqual(state.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        finally:
            state.close()

    def test_real_engine_filters_and_reports_saved_counts_in_worker_thread(self):
        self.use_real_engine([message(1, text="广告合作"), message(2, text="ordinary post")])

        self.assertEqual(self.perform(lambda: self.controller.run_task(self.config)), "completed")

        self.assertEqual(self.progress[-1]["processed"], 2)
        self.assertEqual(self.progress[-1]["filtered"], 1)
        self.assertEqual(self.progress[-1]["sent"], 1)
        self.assertEqual(len(self.clients[-1].sends()), 1)
        self.assertTrue(TrackedState.instances[-1].closed)
        saved = State(self.config.database)
        try:
            statuses = saved.conn.execute("SELECT source_id,status FROM messages ORDER BY source_id").fetchall()
            self.assertEqual([(row["source_id"], row["status"]) for row in statuses],
                             [(1, "filtered_ad"), (2, "done")])
            cost = saved.conn.execute("SELECT SUM(cost) FROM limit_events WHERE account_id=42").fetchone()[0]
            self.assertEqual(cost, 1)
        finally:
            saved.close()

    def test_task_reports_authorizing_before_connect_wait_then_real_zero_summary(self):
        self.use_real_engine([])
        self.assertEqual(self.perform(lambda: self.controller.run_task(self.config)), "completed")
        self.assertEqual(self.phases[0]["phase"], "authorizing")
        self.assertEqual(self.phases[-1]["phase"], "completed")
        self.assertEqual(self.phases[-1]["reason"], "source_empty")
        self.assertEqual(self.phases[-1]["counts"]["sent"], 0)
        self.assertIn("没有可读取", self.phases[-1]["message"])
        self.assertFalse(self.failures)

    def test_real_engine_read_timeout_returns_idle_with_stage_and_no_success(self):
        from tgmover import engine
        self.use_real_engine([])
        client = self.connect()

        async def stalled_source(_value):
            await asyncio.sleep(3600)

        client.get_entity = stalled_source
        with patch.object(engine, "READ_TIMEOUT_SECONDS", 0.12):
            self.assertEqual(self.perform(lambda: self.controller.run_task(self.config)), "failed")
        self.assertEqual(self.phases[-1]["phase"], "failed")
        self.assertIn("识别源", self.phases[-1]["message"])
        self.assertIn("未收到响应", self.failures[-1])
        self.assertFalse(self.controller.busy)
        self.assertTrue(TrackedState.instances[-1].closed)
        self.assertEqual(client.sends(), [])

    def test_auth_restart_failure_clears_challenge_and_does_not_resend_forever(self):
        self.authorized = False
        self.perform(lambda: self.controller.connect_account(self.config))
        client = self.clients[-1]

        async def restart(phone):
            client.record("send_code_request", phone)
            raise errors.AuthRestartError(None)

        client.send_code_request = restart
        self.assertEqual(self.perform(lambda: self.controller.request_code(self.config, "+639171234567")), "failed")
        self.assertIn("AUTH_RESTART", self.failures[-1])
        self.assertIn("本次登录未完成", self.failures[-1])
        self.assertEqual(sum(call[0] == "send_code_request" for call in client.calls), 1)
        self.assertIsNone(self.controller._phone_code_hash)
        self.assertTrue(client.disconnected)
        self.assertTrue(self.ended_challenges)

    def test_real_engine_stop_during_rate_wait_preserves_completed_mapping(self):
        self.use_real_engine([message(1, text="first"), message(2, text="second")])
        config = replace(self.config, limits=replace(self.config.limits, min_send_interval=60))
        self.assertTrue(self.controller.run_task(config))
        self.wait_for(lambda: any(values.get("sent") == 1 for values in self.progress))
        self.controller.stop()
        self.wait_for(lambda: self.finished[-1:] == ["stopped"])

        self.assertEqual(len(self.clients[-1].sends()), 1)
        self.assertTrue(TrackedState.instances[-1].closed)
        saved = State(self.config.database)
        try:
            self.assertEqual(saved.conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)
            self.assertEqual(saved.conn.execute("SELECT source_id FROM messages").fetchone()[0], 1)
            self.assertEqual(saved.conn.execute("SELECT value FROM cursors WHERE scope='post'").fetchone()[0], 1)
            self.assertEqual(saved.conn.execute("SELECT SUM(cost) FROM limit_events").fetchone()[0], 1)
        finally:
            saved.close()

    def test_logs_redact_credentials_phone_and_code_without_traceback(self):
        self.connect_log = "api_hash=" + self.config.api_hash + " phone=+8613800000000 code=12345"
        self.connect()
        self.wait_for(lambda: bool(self.logs))
        combined = "\n".join(self.logs)
        self.assertNotIn(self.config.api_hash, combined)
        self.assertNotIn("8613800000000", combined)
        self.assertNotIn("12345", combined)
        self.assertIn("已隐藏", combined)

    def test_late_close_signal_subscription_after_thread_initialization_failure(self):
        self.wait_for(lambda: self.controller._loop is not None)
        with patch.object(gui_worker.asyncio, "new_event_loop", side_effect=RuntimeError("local initialization failed")):
            failed_controller = gui_worker.GuiController()
            failed_controller._thread.join(timeout=2)
        self.assertFalse(failed_controller._thread.is_alive())
        terminal = []
        # A real window may not have existed when the first signal was emitted.
        failed_controller.finished.connect(terminal.append)

        failed_controller.shutdown()
        self.application.processEvents()

        self.assertEqual(terminal, ["closed"])
        self.assertFalse(failed_controller.busy)

    def test_connection_exception_does_not_reveal_secret_and_releases_session_lock(self):
        secret = self.config.api_hash + " +8613800000000 otp=12345"

        def failed_client_factory(*args, **kwargs):
            client = self.client_factory(*args, **kwargs)

            async def failed_connect():
                client.record("connect")
                raise RuntimeError(secret)

            client.connect = failed_connect
            return client

        self.stack.enter_context(patch.object(gui_worker, "TelegramClient", failed_client_factory))
        self.assertEqual(self.perform(lambda: self.controller.connect_account(self.config)), "failed")
        self.assertIn("RuntimeError", self.failures[-1])
        self.assertNotIn(self.config.api_hash, self.failures[-1])
        self.assertNotIn("8613800000000", self.failures[-1])
        self.assertNotIn("12345", self.failures[-1])
        self.assertTrue(self.clients[-1].disconnected)
        self.assertEqual(TrackedLock.active, set())


if __name__ == "__main__":
    unittest.main()
