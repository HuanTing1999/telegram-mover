"""Real Telethon auth dispatch and GUI challenge transitions, without Telegram I/O.

The transport serializes and deserializes each actual TL request before returning
scripted RPC results. Telethon's send_code_request, sign_in, and _call are intact.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from telethon import TelegramClient, errors, functions, types
from telethon.extensions import BinaryReader
from telethon.sessions import MemorySession

from tgmover import gui_worker
from tgmover.config import Config


PHONE = "+639171234567"
NORMAL_PHONE = PHONE[1:]
OLD_HASH = "offline-old-hash"
NEW_HASH = "offline-new-hash"
NOW = datetime.now(timezone.utc)
PENDING = object()


def sent_code(code_hash=OLD_HASH, *, timeout=0, next_type=True):
    return types.auth.SentCode(
        type=types.auth.SentCodeTypeApp(length=5), phone_code_hash=code_hash,
        next_type=types.auth.CodeTypeSms() if next_type else None, timeout=timeout,
    )


class SerializedSender:
    def __init__(self, client, scripted=None):
        self.client = client
        self.scripted = defaultdict(list, {key: list(values) for key, values in (scripted or {}).items()})
        self.requests = []

    def send(self, request, ordered=False):
        while isinstance(request, functions.InvokeWithoutUpdatesRequest):
            request = request.query
        # Validate real TL fields and flags, rather than copying a FakeClient's
        # interpretation of send_code_request/sign_in into the tests.
        with BinaryReader(bytes(request)) as reader:
            request = reader.tgread_object()
        self.requests.append(request)
        future = asyncio.get_running_loop().create_future()
        queue = self.scripted[type(request)]
        if queue:
            result = queue.pop(0)
        elif isinstance(request, functions.auth.SendCodeRequest):
            result = sent_code()
        elif isinstance(request, functions.auth.ResendCodeRequest):
            result = sent_code(NEW_HASH)
        elif isinstance(request, functions.auth.CancelCodeRequest):
            result = True
        elif isinstance(request, functions.auth.SignInRequest):
            result = types.auth.Authorization(user=self.client.user)
        elif isinstance(request, functions.users.GetUsersRequest):
            result = ([self.client.user] if self.client._authorized
                      else errors.AuthKeyUnregisteredError(request))
        elif isinstance(request, functions.updates.GetStateRequest):
            result = types.updates.State(pts=0, qts=0, date=NOW, seq=0, unread_count=0)
        elif isinstance(request, functions.updates.GetDifferenceRequest):
            result = types.updates.DifferenceEmpty(date=NOW, seq=0)
        else:
            result = AssertionError(f"Unexpected offline RPC: {type(request).__name__}")
        if result is PENDING:
            return future
        if isinstance(result, asyncio.CancelledError):
            future.cancel()
        elif isinstance(result, BaseException):
            future.set_exception(result)
        else:
            future.set_result(result)
        return future


class SerializedAuthClient(TelegramClient):
    def __init__(self, scripted=None, *, stale_hash=None, authorized=False):
        super().__init__(MemorySession(), 12345, "a" * 32,
                         request_retries=0, flood_sleep_threshold=0,
                         raise_last_call_error=True, receive_updates=False)
        self._sender = SerializedSender(self, scripted)
        self._authorized = authorized
        self.active = False
        self.disconnect_count = 0
        self.user = types.User(id=42, access_hash=123, first_name="Offline",
                               phone=NORMAL_PHONE, bot=False)
        if stale_hash:
            self._phone_code_hash[NORMAL_PHONE] = stale_hash
            self._phone = NORMAL_PHONE

    async def connect(self):
        # No super().connect(): it would create a network transport.
        self.active = True

    async def disconnect(self):
        self.disconnect_count += 1
        self.active = False

    def is_connected(self):
        return self.active

    async def is_user_authorized(self):
        return bool(self._authorized)

    def requests(self, request_type):
        return [request for request in self._sender.requests if isinstance(request, request_type)]

    def delivery_requests(self):
        return [request for request in self._sender.requests if isinstance(request, (
            functions.auth.SendCodeRequest, functions.auth.ResendCodeRequest))]


class TelethonResendRpcContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_hash_changes_high_level_send_into_resend(self):
        client = SerializedAuthClient(stale_hash=OLD_HASH)

        result = await client.send_code_request(PHONE)

        self.assertEqual(result.phone_code_hash, NEW_HASH)
        self.assertEqual(client.requests(functions.auth.SendCodeRequest), [])
        request, = client.requests(functions.auth.ResendCodeRequest)
        self.assertEqual((request.phone_number, request.phone_code_hash), (NORMAL_PHONE, OLD_HASH))
        self.assertEqual(client._phone_code_hash[NORMAL_PHONE], NEW_HASH)

    async def test_clearing_stale_cache_restores_fresh_send(self):
        client = SerializedAuthClient(stale_hash=OLD_HASH)
        client._phone_code_hash.pop(NORMAL_PHONE, None)

        await client.send_code_request(PHONE)

        self.assertEqual(len(client.requests(functions.auth.SendCodeRequest)), 1)
        self.assertEqual(client.requests(functions.auth.ResendCodeRequest), [])

    async def test_raw_resend_new_hash_is_explicitly_used_for_real_sign_in(self):
        client = SerializedAuthClient(stale_hash=OLD_HASH)

        result = await client(functions.auth.ResendCodeRequest(NORMAL_PHONE, OLD_HASH))
        # A raw RPC does not update Telethon's high-level cache by itself.
        self.assertEqual(client._phone_code_hash[NORMAL_PHONE], OLD_HASH)
        await client.sign_in(phone=PHONE, code="12345", phone_code_hash=result.phone_code_hash)

        request, = client.requests(functions.auth.SignInRequest)
        self.assertEqual((request.phone_number, request.phone_code_hash, request.phone_code),
                         (NORMAL_PHONE, NEW_HASH, "12345"))
        self.assertEqual(len(client.delivery_requests()), 1)

    async def test_invalid_code_preserves_challenge_and_does_not_send_code(self):
        client = SerializedAuthClient({functions.auth.SignInRequest: [errors.PhoneCodeInvalidError(None)]},
                                      stale_hash=OLD_HASH)

        with self.assertRaises(errors.PhoneCodeInvalidError):
            await client.sign_in(phone=PHONE, code="12345", phone_code_hash=OLD_HASH)

        self.assertEqual(client._phone_code_hash[NORMAL_PHONE], OLD_HASH)
        self.assertEqual(client.delivery_requests(), [])

    async def test_expired_code_removes_telethon_cache_without_automatic_send(self):
        client = SerializedAuthClient({functions.auth.SignInRequest: [errors.PhoneCodeExpiredError(None)]},
                                      stale_hash=OLD_HASH)

        with self.assertRaises(errors.PhoneCodeExpiredError):
            await client.sign_in(phone=PHONE, code="12345", phone_code_hash=OLD_HASH)

        self.assertNotIn(NORMAL_PHONE, client._phone_code_hash)
        self.assertEqual(client.delivery_requests(), [])

    async def test_high_level_resend_expiry_falls_back_to_send_so_gui_uses_raw_rpc(self):
        client = SerializedAuthClient({functions.auth.ResendCodeRequest: [errors.PhoneCodeExpiredError(None)]},
                                      stale_hash=OLD_HASH)

        await client.send_code_request(PHONE)

        self.assertEqual([type(request) for request in client.delivery_requests()],
                         [functions.auth.ResendCodeRequest, functions.auth.SendCodeRequest])

    async def test_raw_resend_expiry_does_not_request_a_new_code(self):
        client = SerializedAuthClient({functions.auth.ResendCodeRequest: [errors.PhoneCodeExpiredError(None)]})

        with self.assertRaises(errors.PhoneCodeExpiredError):
            await client(functions.auth.ResendCodeRequest(NORMAL_PHONE, OLD_HASH))

        self.assertEqual([type(request) for request in client.delivery_requests()],
                         [functions.auth.ResendCodeRequest])

    async def test_cancel_rpc_serializes_only_matching_challenge_and_never_logs_out(self):
        client = SerializedAuthClient(stale_hash=OLD_HASH)

        result = await client(functions.auth.CancelCodeRequest(NORMAL_PHONE, OLD_HASH))

        self.assertIs(result, True)
        request, = client.requests(functions.auth.CancelCodeRequest)
        self.assertEqual((request.phone_number, request.phone_code_hash), (NORMAL_PHONE, OLD_HASH))
        self.assertEqual(client.requests(functions.auth.LogOutRequest), [])
        self.assertEqual(client.delivery_requests(), [])

    async def test_unknown_network_result_does_not_replay_send_or_resend(self):
        for request_type in (functions.auth.SendCodeRequest, functions.auth.ResendCodeRequest):
            with self.subTest(request=request_type.__name__):
                client = SerializedAuthClient({request_type: [ConnectionResetError("offline transport interruption")]})
                with self.assertRaises(ConnectionResetError):
                    if request_type is functions.auth.SendCodeRequest:
                        await client.send_code_request(PHONE)
                    else:
                        await client(functions.auth.ResendCodeRequest(NORMAL_PHONE, OLD_HASH))
                self.assertEqual(len(client.delivery_requests()), 1)

    async def test_sender_cancellation_cannot_cache_success_or_replay_send(self):
        client = SerializedAuthClient({functions.auth.SendCodeRequest: [asyncio.CancelledError()]})

        with self.assertRaises(asyncio.CancelledError):
            await client.send_code_request(PHONE)

        self.assertEqual(len(client.delivery_requests()), 1)
        self.assertNotIn(NORMAL_PHONE, client._phone_code_hash)


class GuiResendRpcContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.config = Config(api_id=12345, api_hash="a" * 32, source="source", target="target",
                             mode="reupload", comment_mode="reupload", sync_comments=False,
                             session=root / "accounts" / "offline", database=root / "state.sqlite3",
                             download_dir=root / "downloads", start_id=0, poll_interval=0,
                             settle_seconds=0, max_flood_wait=300, send_delay=0)
        self.scripted = {}
        self.stale_hash = None
        self.already_authorized = False
        self.clients, self.finished, self.failures = [], [], []
        self.users, self.sent, self.deliveries, self.ended = [], [], [], []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(gui_worker, "TelegramClient", self.client_factory))
        self.controller = gui_worker.GuiController()
        self.controller.finished.connect(self.finished.append)
        self.controller.failed.connect(self.failures.append)
        self.controller.authenticated.connect(self.users.append)
        self.controller.code_sent.connect(self.sent.append)
        # This is the public, credential-free resend readiness contract.
        self.controller.code_delivery.connect(self.deliveries.append)
        self.controller.challenge_ended.connect(lambda: self.ended.append(True))
        self.addCleanup(self.dispose)

    def client_factory(self, *args, **kwargs):
        client = SerializedAuthClient(self.scripted, stale_hash=self.stale_hash,
                                      authorized=self.already_authorized)
        self.clients.append(client)
        return client

    def wait_for(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Offline authorization operation did not finish")

    def perform(self, action):
        old = len(self.finished)
        self.assertTrue(action())
        self.wait_for(lambda: len(self.finished) > old and not self.controller.busy)
        return self.finished[-1]

    def dispose(self):
        self.controller.shutdown()
        self.wait_for(lambda: "closed" in self.finished)
        self.controller._thread.join(timeout=2)
        self.assertFalse(self.controller._thread.is_alive())

    def request(self):
        self.assertEqual(self.perform(lambda: self.controller.request_code(self.config, PHONE)), "code_sent")
        return self.clients[-1]

    def assert_cleared(self, client):
        self.assertIsNone(self.controller._phone)
        self.assertIsNone(self.controller._phone_code_hash)
        self.assertNotIn(NORMAL_PHONE, client._phone_code_hash)

    def test_fresh_gui_request_clears_internal_stale_hash_before_telethon_send(self):
        self.stale_hash = "old-process-challenge"

        client = self.request()

        self.assertEqual(len(client.requests(functions.auth.SendCodeRequest)), 1)
        self.assertEqual(client.requests(functions.auth.ResendCodeRequest), [])
        self.assertEqual(self.controller._phone_code_hash, OLD_HASH)
        self.assertTrue(self.deliveries)
        self.assertTrue({"can_resend", "resend_after", "delivery", "next_delivery", "message"}
                        .issubset(self.deliveries[-1]))
        for secret in (PHONE, NORMAL_PHONE, OLD_HASH):
            self.assertNotIn(secret, str(self.deliveries[-1]))

    def test_explicit_resend_then_submit_uses_new_hash_on_actual_sign_in_rpc(self):
        client = self.request()

        self.assertEqual(self.perform(lambda: self.controller.resend_code(self.config, PHONE)), "code_sent")
        self.assertEqual(self.controller._phone_code_hash, NEW_HASH)
        self.assertEqual(self.perform(lambda: self.controller.submit_code("12345")), "authenticated")

        request, = client.requests(functions.auth.ResendCodeRequest)
        self.assertEqual((request.phone_number.lstrip("+"), request.phone_code_hash), (NORMAL_PHONE, OLD_HASH))
        sign_in, = client.requests(functions.auth.SignInRequest)
        self.assertEqual(sign_in.phone_code_hash, NEW_HASH)
        self.assertEqual(len(client.requests(functions.auth.SendCodeRequest)), 1)
        self.assertEqual(len(self.users), 1)
        self.assert_cleared(client)

    def test_invalid_code_does_not_resend_and_three_errors_clear_both_caches(self):
        self.scripted = {functions.auth.SignInRequest: [errors.PhoneCodeInvalidError(None) for _ in range(3)]}
        client = self.request()

        for index in range(3):
            self.assertEqual(self.perform(lambda: self.controller.submit_code("12345")), "failed")
            if index < 2:
                self.assertEqual(self.controller._phone_code_hash, OLD_HASH)

        self.assertEqual(len(client.delivery_requests()), 1)
        self.assert_cleared(client)
        self.assertTrue(self.ended)
        self.assertEqual(self.users, [])

    def test_expired_sign_in_also_clears_worker_challenge_without_sending_again(self):
        self.scripted = {functions.auth.SignInRequest: [errors.PhoneCodeExpiredError(None)]}
        client = self.request()

        self.assertEqual(self.perform(lambda: self.controller.submit_code("12345")), "failed")

        self.assertEqual(len(client.delivery_requests()), 1)
        self.assert_cleared(client)
        self.assertTrue(self.ended)
        self.assertEqual(self.users, [])

    def test_expired_resend_clears_state_without_hidden_fresh_send(self):
        self.scripted = {functions.auth.ResendCodeRequest: [errors.PhoneCodeExpiredError(None)]}
        client = self.request()

        self.assertEqual(self.perform(lambda: self.controller.resend_code(self.config, PHONE)), "failed")

        self.assertEqual([type(request) for request in client.delivery_requests()],
                         [functions.auth.SendCodeRequest, functions.auth.ResendCodeRequest])
        self.assert_cleared(client)
        self.assertEqual(self.users, [])

    def test_auth_restart_during_sign_in_ends_old_challenge_without_new_code(self):
        self.scripted = {functions.auth.SignInRequest: [errors.AuthRestartError(None)]}
        client = self.request()

        self.assertEqual(self.perform(lambda: self.controller.submit_code("12345")), "failed")

        self.assertEqual(len(client.requests(functions.auth.SignInRequest)), 1)
        self.assertEqual(len(client.delivery_requests()), 1)
        self.assert_cleared(client)
        self.assertEqual(self.users, [])

    def test_next_type_absent_never_fabricates_a_resend_method(self):
        self.scripted = {functions.auth.SendCodeRequest: [sent_code(next_type=False)]}
        client = self.request()

        self.assertEqual(self.perform(lambda: self.controller.resend_code(self.config, PHONE)), "failed")

        self.assertEqual(client.requests(functions.auth.ResendCodeRequest), [])
        self.assertFalse(self.deliveries[-1]["can_resend"])

    def test_delivery_timeout_is_resend_gate_not_code_expiration(self):
        self.scripted = {functions.auth.SendCodeRequest: [sent_code(timeout=30)]}
        client = self.request()

        self.assertEqual(self.perform(lambda: self.controller.resend_code(self.config, PHONE)), "failed")

        self.assertEqual(client.requests(functions.auth.ResendCodeRequest), [])
        self.assertEqual(self.controller._phone_code_hash, OLD_HASH)
        # The first valid challenge remains usable while resend is unavailable.
        self.assertEqual(self.perform(lambda: self.controller.submit_code("12345")), "authenticated")
        self.assertEqual(client.requests(functions.auth.SignInRequest)[0].phone_code_hash, OLD_HASH)

    def test_cancel_removes_both_caches_without_logout_or_automatic_send(self):
        client = self.request()

        self.assertEqual(self.perform(self.controller.cancel_authentication), "auth_cancelled")

        self.assert_cleared(client)
        self.assertEqual(len(client.delivery_requests()), 1)
        self.assertEqual(client.requests(functions.auth.LogOutRequest), [])
        requests = client.requests(functions.auth.CancelCodeRequest)
        self.assertLessEqual(len(requests), 1)  # Server cancellation is best-effort.
        for request in requests:
            self.assertEqual((request.phone_number.lstrip("+"), request.phone_code_hash), (NORMAL_PHONE, OLD_HASH))
        self.assertGreaterEqual(client.disconnect_count, 1)

    def test_cancelling_authorized_connection_never_cancels_code_or_logs_out(self):
        self.already_authorized = True
        self.assertEqual(self.perform(lambda: self.controller.connect_account(self.config)), "authenticated")
        client = self.clients[-1]

        self.assertEqual(self.perform(self.controller.cancel_authentication), "auth_cancelled")

        self.assertEqual(client.requests(functions.auth.CancelCodeRequest), [])
        self.assertEqual(client.requests(functions.auth.LogOutRequest), [])
        self.assertTrue(client._authorized)

    def test_unknown_resend_network_result_does_not_fall_back_to_new_send(self):
        self.scripted = {functions.auth.ResendCodeRequest: [ConnectionResetError("offline uncertain resend")]}
        client = self.request()

        self.assertEqual(self.perform(lambda: self.controller.resend_code(self.config, PHONE)), "failed")

        self.assertEqual([type(request) for request in client.delivery_requests()],
                         [functions.auth.SendCodeRequest, functions.auth.ResendCodeRequest])
        self.assert_cleared(client)
        self.assertEqual(self.users, [])

    def test_unknown_resend_server_failure_also_ends_old_challenge_without_replay(self):
        # Telethon propagates an exhausted 500 RPC error, rather than converting
        # it into an OSError. It still cannot establish which code was sent.
        self.scripted = {functions.auth.ResendCodeRequest: [errors.RpcCallFailError(None)]}
        client = self.request()

        self.assertEqual(self.perform(lambda: self.controller.resend_code(self.config, PHONE)), "failed")

        self.assertEqual([type(request) for request in client.delivery_requests()],
                         [functions.auth.SendCodeRequest, functions.auth.ResendCodeRequest])
        self.assert_cleared(client)
        self.assertGreaterEqual(client.disconnect_count, 1)
        self.assertEqual(self.users, [])

    def test_server_wait_rejection_does_not_consume_invalid_code_attempts(self):
        self.scripted = {functions.auth.SignInRequest: [errors.FloodWaitError(None, capture=60)]}
        client = self.request()
        self.assertEqual(self.perform(lambda: self.controller.submit_code("12345")), "failed")
        attempted = self.controller._code_attempts

        self.assertEqual(self.perform(lambda: self.controller.submit_code("12345")), "failed")

        self.assertEqual(self.controller._code_attempts, attempted)
        self.assertEqual(len(client.requests(functions.auth.SignInRequest)), 1)
        self.assertEqual(len(client.delivery_requests()), 1)

    def test_cancelled_resend_rpc_is_not_success_and_never_replayed(self):
        self.scripted = {functions.auth.ResendCodeRequest: [asyncio.CancelledError()]}
        client = self.request()
        before = len(self.sent)

        self.assertEqual(self.perform(lambda: self.controller.resend_code(self.config, PHONE)), "failed")

        self.assertEqual(len(self.sent), before)
        self.assertEqual(len(client.requests(functions.auth.ResendCodeRequest)), 1)
        self.assert_cleared(client)
        self.assertGreaterEqual(client.disconnect_count, 1)


if __name__ == "__main__":
    unittest.main()
