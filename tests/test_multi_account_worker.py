"""Account switching uses separate sessions and one shared application pair.

These are real GUI worker-thread/signal tests. Only Telegram transport and the
file lock are replaced; the fake session store retains each user's login across
client instances, without contacting Telegram or reading a real session file.
"""

from __future__ import annotations

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

from test_gui_worker import FakeTelegramClient, TrackedLock
from tgmover import gui_worker
from tgmover.config import Config


class AccountSessionClient(FakeTelegramClient):
    """Persist authorization by session path, never by the shared API pair."""

    def __init__(self, owner, session, api_id, api_hash, **kwargs):
        super().__init__(owner, session, api_id, api_hash, **kwargs)
        self.api_id = api_id
        self.session_key = str(Path(session).resolve())
        self.profile = owner.sessions.get(self.session_key)
        self.authorized = self.profile is not None
        self.pending_phone = None

    def is_connected(self):
        return self.connected

    async def disconnect(self):
        await super().disconnect()
        self.connected = False

    async def get_me(self):
        self.record("get_me")
        return self.profile

    async def send_code_request(self, phone):
        self.pending_phone = phone
        return await super().send_code_request(phone)

    async def sign_in(self, **kwargs):
        if kwargs["phone"] != self.pending_phone:
            raise AssertionError("A verification challenge crossed account sessions")
        await super().sign_in(**kwargs)
        self.profile = self.owner.profiles[self.pending_phone]
        self.owner.sessions[self.session_key] = self.profile


class MultiAccountWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.config_a = Config(
            api_id=12345, api_hash="a" * 32, source="source", target="target",
            mode="reupload", comment_mode="reupload", sync_comments=False,
            session=self.base / "accounts" / "account-a",
            database=self.base / "state.sqlite3", download_dir=self.base / "downloads",
            start_id=0, poll_interval=0, settle_seconds=2, max_flood_wait=300,
            send_delay=0,
        )
        self.config_b = replace(self.config_a, session=self.base / "accounts" / "account-b")
        self.phone_a, self.phone_b = "+8613000000001", "+639000000002"
        self.profiles = {
            self.phone_a: SimpleNamespace(id=101, bot=False, first_name="Account A", last_name="",
                                          username="account_a", phone=self.phone_a[1:]),
            self.phone_b: SimpleNamespace(id=202, bot=False, first_name="Account B", last_name="",
                                          username="account_b", phone=self.phone_b[1:]),
        }
        self.sessions = {str(self.config_a.session.resolve()): self.profiles[self.phone_a]}
        self.authorized, self.bot = False, False
        self.connect_started, self.connect_gate = threading.Event(), threading.Event()
        self.connect_gate.set()
        self.connect_log = ""
        # Shared fake client now exposes read-only dialog enumeration for the
        # picker; account-switch tests do not need any available dialogs.
        self.dialog_entities = []
        self.clients, self.finished, self.failures = [], [], []
        self.users, self.code_requests, self.logs, self.statuses = [], [], [], []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        TrackedLock.active = set()
        TrackedLock.events = []
        self.stack.enter_context(patch.object(gui_worker, "TelegramClient", self.client_factory))
        self.stack.enter_context(patch.object(gui_worker, "ProcessLock", TrackedLock))
        self.controller = gui_worker.GuiController()
        self.controller.finished.connect(self.finished.append)
        self.controller.failed.connect(self.failures.append)
        self.controller.authenticated.connect(self.users.append)
        self.controller.code_sent.connect(self.code_requests.append)
        self.controller.log.connect(self.logs.append)
        self.controller.status.connect(self.statuses.append)
        self.addCleanup(self.dispose)

    def client_factory(self, *args, **kwargs):
        client = AccountSessionClient(self, *args, **kwargs)
        self.clients.append(client)
        return client

    def wait_for(self, condition, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if condition():
                return
            time.sleep(0.005)
        self.fail("The account operation did not finish within the test timeout")

    def perform(self, action):
        before = len(self.finished)
        self.assertTrue(action())
        self.wait_for(lambda: len(self.finished) > before and not self.controller.busy)
        return self.finished[-1]

    def dispose(self):
        self.connect_gate.set()
        self.controller.shutdown()
        self.wait_for(lambda: "closed" in self.finished)
        self.controller._thread.join(timeout=2)
        self.assertFalse(self.controller._thread.is_alive())
        self.assertEqual(TrackedLock.active, set())

    def connect_a(self):
        self.assertEqual(self.perform(lambda: self.controller.connect_account(self.config_a)), "authenticated")
        self.assertEqual(self.users[-1]["id"], 101)
        return self.clients[-1]

    def login_b(self):
        self.assertEqual(self.perform(lambda: self.controller.request_code(self.config_b, self.phone_b)), "code_sent")
        self.assertEqual(self.perform(lambda: self.controller.submit_code("12345")), "authenticated")
        self.assertEqual(self.users[-1]["id"], 202)
        return self.clients[-1]

    def test_accounts_share_application_credentials_but_keep_separate_logins(self):
        account_a = self.connect_a()
        account_b = self.login_b()

        self.assertTrue(account_a.disconnected)
        self.assertIsNot(account_a, account_b)
        self.assertEqual([(client.api_id, client.api_hash) for client in self.clients],
                         [(self.config_a.api_id, self.config_a.api_hash)] * 2)
        self.assertNotEqual(account_a.session_key, account_b.session_key)
        self.assertEqual(self.sessions[account_a.session_key].id, 101)
        self.assertEqual(self.sessions[account_b.session_key].id, 202)
        self.assertEqual([call for call in account_b.calls if call[0] == "send_code_request"],
                         [("send_code_request", self.phone_b)])

    def test_switch_releases_previous_session_lock_before_acquiring_new_one(self):
        self.connect_a()
        self.login_b()

        lock_a = Path(str(self.config_a.session.resolve()) + ".session.mover.lock")
        lock_b = Path(str(self.config_b.session.resolve()) + ".session.mover.lock")
        events = [(action, path) for action, path, _thread in TrackedLock.events]
        self.assertEqual(events, [("enter", lock_a), ("exit", lock_a), ("enter", lock_b)])
        self.assertEqual(TrackedLock.active, {lock_b})
        self.assertTrue(all(thread == self.controller._thread.ident for _, _, thread in TrackedLock.events))

    def test_switching_back_reuses_each_persisted_session_without_new_codes(self):
        self.connect_a()
        original_b = self.login_b()
        self.assertEqual(self.perform(lambda: self.controller.connect_account(self.config_a)), "authenticated")
        self.assertEqual(self.users[-1]["id"], 101)
        self.assertTrue(original_b.disconnected)
        self.assertEqual(self.perform(lambda: self.controller.connect_account(self.config_b)), "authenticated")
        self.assertEqual(self.users[-1]["id"], 202)

        requested = [call for client in self.clients for call in client.calls if call[0] == "send_code_request"]
        signed_in = [call for client in self.clients for call in client.calls if call[0] == "sign_in"]
        self.assertEqual(len(requested), 1)
        self.assertEqual(len(signed_in), 1)
        self.assertEqual(len(self.clients), 4)
        self.assertEqual(len(TrackedLock.active), 1)

    def test_requesting_another_phone_in_authorized_session_fails_without_false_success(self):
        account_a = self.connect_a()
        user_count = len(self.users)

        self.assertEqual(self.perform(lambda: self.controller.request_code(self.config_a, self.phone_b)), "failed")

        self.assertEqual(len(self.users), user_count)
        self.assertEqual(self.code_requests, [])
        self.assertFalse(any(call[0] == "send_code_request" for call in account_a.calls))
        self.assertTrue(self.failures)
        self.assertIn("账号", self.failures[-1])
        output = "\n".join(self.failures + self.logs + self.statuses)
        self.assertNotIn(self.phone_a[1:], output)
        self.assertNotIn(self.phone_b[1:], output)
        self.assertIsNone(self.controller._phone_code_hash)

    def test_same_authorized_phone_reuses_session_after_international_normalization(self):
        self.sessions[str(self.config_b.session.resolve())] = self.profiles[self.phone_b]

        result = self.perform(lambda: self.controller.request_code(self.config_b, "0063 900-000-0002"))

        self.assertEqual(result, "authenticated")
        self.assertEqual(self.users[-1]["id"], 202)
        self.assertEqual(self.code_requests, [])
        self.assertFalse(any(call[0] == "send_code_request" for call in self.clients[-1].calls))

    def test_switching_away_from_pending_code_cannot_verify_old_challenge_on_other_account(self):
        self.assertEqual(self.perform(lambda: self.controller.request_code(self.config_b, self.phone_b)), "code_sent")
        pending_b = self.clients[-1]
        self.connect_a()
        user_count = len(self.users)

        self.assertEqual(self.perform(lambda: self.controller.submit_code("12345")), "failed")

        self.assertTrue(pending_b.disconnected)
        self.assertEqual(len(self.users), user_count)
        self.assertFalse(any(call[0] == "sign_in" for client in self.clients for call in client.calls))
        self.assertNotIn(str(self.config_b.session.resolve()), self.sessions)


if __name__ == "__main__":
    unittest.main()
