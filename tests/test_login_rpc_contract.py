"""Real Telethon auth RPC dispatch, with no connection to any Telegram server."""

from __future__ import annotations

import asyncio
import os
import threading
import time
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from telethon import TelegramClient, errors, functions, types
from telethon.sessions import MemorySession

from tgmover import gui_worker


PHONE = "+639171234567"
NOW = datetime.now(timezone.utc)


class InterceptedSender:
    def __init__(self, client, outcomes):
        self.client = client
        self.outcomes = list(outcomes)
        self.requests = []
        self.requested = threading.Event()

    def send(self, request, ordered=False):
        while isinstance(request, functions.InvokeWithoutUpdatesRequest):
            request = request.query
        self.requests.append(request)
        self.requested.set()
        future = asyncio.get_running_loop().create_future()
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome == "pending":
            return future
        if isinstance(outcome, asyncio.CancelledError):
            # This is what a sender disconnect without an error does to an
            # outstanding RPC: cancellation propagates through real _call.
            future.cancel()
        elif isinstance(outcome, BaseException):
            future.set_exception(outcome)
        elif outcome is not None:
            future.set_result(outcome)
        elif isinstance(request, functions.auth.SendCodeRequest):
            future.set_result(types.auth.SentCode(
                type=types.auth.SentCodeTypeApp(length=5), phone_code_hash="test-challenge",
            ))
        elif isinstance(request, functions.users.GetUsersRequest):
            if self.client._authorized:
                future.set_result([self.client.user])
            else:
                future.set_exception(errors.AuthKeyUnregisteredError(request))
        elif isinstance(request, functions.auth.SignInRequest):
            future.set_result(types.auth.Authorization(user=self.client.user))
        elif isinstance(request, functions.updates.GetStateRequest):
            future.set_result(types.updates.State(pts=0, qts=0, date=NOW, seq=0, unread_count=0))
        elif isinstance(request, functions.updates.GetDifferenceRequest):
            future.set_result(types.updates.DifferenceEmpty(date=NOW, seq=0))
        else:
            future.set_exception(AssertionError(f"Unexpected offline RPC: {type(request).__name__}"))
        return future


class InterceptedAuthClient(TelegramClient):
    """Keep real send_code_request/sign_in/_call, replace transport and DC I/O."""

    def __init__(self, outcomes=(), *, request_retries=0):
        super().__init__(MemorySession(), 12345, "a" * 32,
                         request_retries=request_retries, flood_sleep_threshold=0,
                         raise_last_call_error=True, receive_updates=False)
        self.session.set_dc(2, "127.0.0.1", 443)
        self._sender = InterceptedSender(self, outcomes)
        self.switched = []
        self.active = True
        self.disconnect_count = 0
        self.user = types.User(id=42, access_hash=123, first_name="Offline", bot=False)

    async def is_user_authorized(self):
        # Avoid adding unrelated authorization-probe RPCs to migration counts.
        return bool(self._authorized)

    async def _switch_dc(self, new_dc):
        self.switched.append(new_dc)
        self.session.set_dc(new_dc, "127.0.0.1", 443)

    def is_connected(self):
        return self.active

    async def connect(self):
        raise AssertionError("These contract tests must never connect")

    async def disconnect(self):
        self.disconnect_count += 1
        self.active = False


class TelethonMigrationContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_request_retries_switches_dc_but_requires_explicit_replay(self):
        for error_type in (errors.PhoneMigrateError, errors.NetworkMigrateError, errors.UserMigrateError):
            with self.subTest(error=error_type.__name__):
                client = InterceptedAuthClient([error_type(None, capture=4)])

                with self.assertRaises(error_type):
                    await client.send_code_request(PHONE)

                self.assertEqual(client.switched, [4])
                self.assertEqual(client.session.dc_id, 4)
                self.assertEqual(len(client._sender.requests), 1)
                result = await client.send_code_request(PHONE)
                self.assertIsInstance(result, types.auth.SentCode)
                self.assertEqual(len(client._sender.requests), 2)
                self.assertEqual(client.switched, [4])
                self.assertTrue(all(request.phone_number == PHONE.lstrip("+")
                                    for request in client._sender.requests))

    async def test_one_global_retry_would_replay_automatically_but_is_not_needed(self):
        client = InterceptedAuthClient([errors.PhoneMigrateError(None, capture=4)], request_retries=1)
        result = await client.send_code_request(PHONE)
        self.assertIsInstance(result, types.auth.SentCode)
        self.assertEqual(len(client._sender.requests), 2)
        self.assertEqual(client.switched, [4])

    async def test_send_code_request_itself_has_bounded_auth_restart_fallback(self):
        client = InterceptedAuthClient([errors.AuthRestartError(None) for _ in range(4)])
        with self.assertRaises(errors.AuthRestartError):
            await client.send_code_request(PHONE)
        self.assertEqual(len(client._sender.requests), 4)
        self.assertEqual(client.switched, [])

    async def test_real_call_propagates_sender_cancellation_as_base_exception(self):
        client = InterceptedAuthClient([asyncio.CancelledError()])
        with self.assertRaises(asyncio.CancelledError):
            await client.send_code_request(PHONE)
        self.assertNotIsInstance(asyncio.CancelledError(), Exception)


class GuiLoginRpcContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.controller = gui_worker.GuiController()
        self.results, self.failures, self.authenticated, self.finished = [], [], [], []
        self.challenge_ends = []
        self.controller.finished.connect(self.finished.append)
        self.controller.failed.connect(self.failures.append)
        self.controller.authenticated.connect(self.authenticated.append)
        self.controller.challenge_ended.connect(lambda: self.challenge_ends.append(True))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(self.close_controller)

    def wait_for(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Auth contract did not finish within its offline deadline")

    def close_controller(self):
        self.controller.shutdown()
        self.wait_for(lambda: "closed" in self.finished)
        self.controller._thread.join(timeout=2)
        self.assertFalse(self.controller._thread.is_alive())

    def run_code_rpc(self, client):
        async def operation():
            self.controller._client = client
            self.controller._phone = PHONE
            self.controller._phone_code_hash = "test-challenge"
            result = await self.controller._auth_rpc("发送验证码", lambda: client.send_code_request(PHONE))
            self.results.append(result)

        self.assertTrue(self.controller._submit("code_sent", operation))
        self.wait_for(lambda: bool(self.finished) and not self.controller.busy)

    def test_gui_replays_migration_once_without_switching_same_dc_twice(self):
        client = InterceptedAuthClient([errors.PhoneMigrateError(None, capture=4)])
        self.run_code_rpc(client)
        self.assertEqual(self.finished[-1], "code_sent")
        self.assertEqual(self.failures, [])
        self.assertEqual(len(self.results), 1)
        self.assertEqual(len(client._sender.requests), 2)
        self.assertEqual(client.switched, [4])
        self.assertEqual(client._request_retries, 0)

    def test_gui_migration_replay_budget_is_finite(self):
        client = InterceptedAuthClient([errors.PhoneMigrateError(None, capture=dc) for dc in (3, 4, 5, 2)])
        self.run_code_rpc(client)
        self.assertEqual(self.finished[-1], "failed")
        self.assertTrue(self.failures)
        self.assertEqual(self.results, [])
        self.assertLessEqual(len(client._sender.requests), 3)
        self.assertEqual(len(client.switched), len(client._sender.requests))

    def test_cancelled_sender_future_cannot_be_reported_as_authenticated(self):
        # First get_me is an unauthorized probe inside the real sign_in.
        # Cancel the subsequent auth.SignIn RPC to reproduce a dropped login.
        client = InterceptedAuthClient([errors.AuthKeyUnregisteredError(None), asyncio.CancelledError()])

        async def operation():
            self.controller._client = client
            self.controller._phone = PHONE
            self.controller._phone_code_hash = "test-challenge"
            result = await self.controller._auth_rpc("验证验证码", lambda: client.sign_in(
                phone=PHONE, code="12345", phone_code_hash="test-challenge"))
            self.results.append(result)

        self.assertTrue(self.controller._submit("authenticated", operation))
        self.wait_for(lambda: bool(self.finished) and not self.controller.busy)
        self.assertIn(self.finished[-1], {"failed", "stopped"})
        self.assertNotEqual(self.finished[-1], "authenticated")
        self.assertEqual(self.results, [])
        self.assertEqual(self.authenticated, [])
        self.assertTrue(self.failures or self.finished[-1] == "stopped")
        self.assertIsNone(self.controller._phone)
        self.assertIsNone(self.controller._phone_code_hash)
        self.assertGreaterEqual(client.disconnect_count, 1)

    def test_pending_auth_rpc_times_out_and_releases_busy_and_challenge(self):
        self.stack.enter_context(patch.object(gui_worker, "AUTH_TIMEOUT_SECONDS", 0.12))
        self.stack.enter_context(patch.object(gui_worker, "AUTH_POLL_SECONDS", 0.01))
        client = InterceptedAuthClient(["pending"])
        self.run_code_rpc(client)
        self.assertEqual(self.finished[-1], "failed")
        self.assertTrue(self.failures)
        self.assertEqual(self.results, [])
        self.assertFalse(self.controller.busy)
        self.assertIsNone(self.controller._phone)
        self.assertIsNone(self.controller._phone_code_hash)
        self.assertGreaterEqual(client.disconnect_count, 1)


if __name__ == "__main__":
    unittest.main()
