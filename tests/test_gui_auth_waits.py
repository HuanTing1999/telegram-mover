"""Real worker threads/Qt signals with offline delivery and persistent waits."""

from __future__ import annotations

import os
import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from unittest.mock import patch

from telethon import errors, functions, types, utils

import test_gui_worker as worker_fixture
from tgmover import gui_worker


PHONE = "+639171234567"


class DeliveryClient(worker_fixture.FakeTelegramClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._phone_code_hash, self._phone = {}, None

    def response(self, results, default_hash):
        if results:
            result = results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return types.auth.SentCode(type=types.auth.SentCodeTypeApp(5), phone_code_hash=default_hash,
                                   next_type=types.auth.CodeTypeSms() if self.owner.has_next else None,
                                   timeout=self.owner.delivery_timeout)

    async def send_code_request(self, phone):
        self.record("send_code_request", phone, dict(self._phone_code_hash))
        result = self.response(self.owner.send_results, "offline-first-hash")
        self._phone_code_hash[utils.parse_phone(phone)] = result.phone_code_hash
        return result

    async def __call__(self, request):
        self.record("raw_request", request)
        if not isinstance(request, functions.auth.ResendCodeRequest):
            raise AssertionError("Only the explicit resend auth RPC is allowed")
        return self.response(self.owner.resend_results, "offline-new-hash")


class GuiAuthWaitTests(unittest.TestCase):
    # Reuse the worker/signal lifecycle without inheriting its unrelated cases.
    setUpClass = classmethod(worker_fixture.GuiWorkerTests.setUpClass.__func__)
    wait_for = worker_fixture.GuiWorkerTests.wait_for
    perform = worker_fixture.GuiWorkerTests.perform
    dispose = worker_fixture.GuiWorkerTests.dispose

    def setUp(self):
        self.send_results, self.resend_results = [], []
        self.delivery_timeout, self.has_next = 0, True
        worker_fixture.GuiWorkerTests.setUp(self)
        self.authorized = False
        self.now = 2_000_000_000.0
        self.stack.enter_context(patch.object(gui_worker.time, "time", lambda: self.now))
        self.deliveries = []
        self.controller.code_delivery.connect(self.deliveries.append)

    def client_factory(self, *args, **kwargs):
        client = DeliveryClient(self, *args, **kwargs)
        self.clients.append(client)
        return client

    def request(self):
        self.assertEqual(self.perform(lambda: self.controller.request_code(self.config, PHONE)), "code_sent")
        return self.clients[-1]

    def restart(self):
        previous = self.controller
        previous.shutdown()
        self.wait_for(lambda: previous._closed)
        previous._thread.join(timeout=2)
        self.assertFalse(previous._thread.is_alive())
        self.controller = gui_worker.GuiController()
        self.controller.finished.connect(self.finished.append)
        self.controller.failed.connect(self.failures.append)
        self.controller.authenticated.connect(self.users.append)
        self.controller.code_delivery.connect(self.deliveries.append)

    def test_delivery_timeout_channels_then_new_hash_without_implicit_send(self):
        self.delivery_timeout = 40
        client = self.request()
        event = self.deliveries[-1]
        self.assertEqual((event["delivery"], event["next_delivery"]), ("Telegram 官方客户端", "短信"))
        self.assertEqual(event["resend_after"], 40.0)
        self.assertFalse(event["can_resend"])
        self.assertTrue(event["resend_available"])
        self.assertEqual(self.perform(lambda: self.controller.resend_code(self.config, PHONE)), "failed")
        self.assertFalse(any(call[0] == "raw_request" for call in client.calls))
        self.now += 40
        self.assertEqual(self.perform(lambda: self.controller.resend_code(self.config, PHONE)), "code_sent")
        request = next(call[1] for call in client.calls if call[0] == "raw_request")
        self.assertEqual((request.phone_number, request.phone_code_hash), (PHONE[1:], "offline-first-hash"))
        self.assertEqual(client._phone_code_hash[PHONE[1:]], "offline-new-hash")
        self.assertEqual(self.perform(lambda: self.controller.submit_code("12345")), "authenticated")
        self.assertEqual(next(call[1] for call in client.calls if call[0] == "sign_in")["phone_code_hash"],
                         "offline-new-hash")
        self.assertEqual(sum(call[0] == "send_code_request" for call in client.calls), 1)

    def test_resend_rejects_old_phone_session_and_api_without_any_raw_call(self):
        client = self.request()
        for config, phone in ((self.config, "+447700900123"),
                              (replace(self.config, session=self.base / "other"), PHONE),
                              (replace(self.config, api_id=23456), PHONE),
                              (replace(self.config, api_hash="b" * 32), PHONE)):
            self.assertEqual(self.perform(lambda: self.controller.resend_code(config, phone)), "failed")
        self.assertFalse(any(call[0] == "raw_request" for call in client.calls))
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(self.controller._phone_code_hash, "offline-first-hash")

    def test_server_flood_deadline_survives_restart_without_secrets_or_renewal(self):
        self.send_results = [errors.FloodWaitError(None, 90)]
        self.assertEqual(self.perform(lambda: self.controller.request_code(self.config, PHONE)), "failed")
        database = self.config.session.parent / "auth_cooldowns.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            rows = connection.execute("SELECT * FROM auth_cooldowns").fetchall()
        self.assertEqual(rows[0][1], self.now + 90)
        self.assertEqual(len(rows[0][0]), 64)
        for secret in (PHONE, PHONE[1:], self.config.api_hash, "offline-first-hash"):
            self.assertNotIn(secret, str(rows))
        self.restart()
        self.now += 25
        self.assertEqual(self.perform(lambda: self.controller.request_code(self.config, PHONE)), "failed")
        self.assertIn("65 秒", self.failures[-1])
        self.assertFalse(any(call[0] == "send_code_request" for call in self.clients[-1].calls))
        self.now += 65
        self.request()
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT server_until FROM auth_cooldowns").fetchone()[0],
                             2_000_000_090.0)

    def test_server_wait_isolated_by_both_phone_and_session(self):
        self.send_results = [errors.FloodWaitError(None, 90)]
        self.assertEqual(self.perform(lambda: self.controller.request_code(self.config, PHONE)), "failed")
        self.assertEqual(self.perform(lambda: self.controller.request_code(self.config, "+447700900123")), "code_sent")
        another = replace(self.config, session=self.base / "other-session")
        self.assertEqual(self.perform(lambda: self.controller.request_code(another, PHONE)), "code_sent")
        self.assertEqual(sum(call[0] == "send_code_request" for client in self.clients for call in client.calls), 3)

    def test_cancelling_then_fresh_request_still_obeys_delivery_wait(self):
        self.delivery_timeout = 30
        previous = self.request()
        self.assertEqual(self.perform(self.controller.cancel_authentication), "auth_cancelled")
        self.assertEqual(previous._phone_code_hash, {})
        self.assertEqual(self.perform(lambda: self.controller.request_code(self.config, PHONE)), "failed")
        self.assertEqual(sum(call[0] == "send_code_request" for client in self.clients for call in client.calls), 1)
        self.now += 30
        client = self.request()
        self.assertEqual(next(call[2] for call in client.calls if call[0] == "send_code_request"), {})
        self.assertFalse(any(call[0] == "raw_request" for client in self.clients for call in client.calls))

    def test_resend_network_unknown_uses_local_guard_across_restart(self):
        client = self.request()
        self.resend_results = [ConnectionResetError("offline unknown result")]
        self.assertEqual(self.perform(lambda: self.controller.resend_code(self.config, PHONE)), "failed")
        self.assertTrue(client.disconnected)
        self.assertEqual(client._phone_code_hash, {})
        self.assertIn("本机", self.failures[-1])
        self.assertIn("不是 Telegram 服务端", self.failures[-1])
        self.restart()
        self.assertEqual(self.perform(lambda: self.controller.request_code(self.config, PHONE)), "failed")
        self.assertIn("本机", self.failures[-1])
        self.assertFalse(any(call[0] == "send_code_request" for call in self.clients[-1].calls))
        self.now += 30
        self.request()

    def test_server_wait_rejecting_code_does_not_consume_code_attempts(self):
        client = self.request()
        client.sign_in_errors = [errors.FloodWaitError(None, 60)]
        self.assertEqual(self.perform(lambda: self.controller.submit_code("12345")), "failed")
        attempts = self.controller._code_attempts
        for _ in range(4):
            self.assertEqual(self.perform(lambda: self.controller.submit_code("12345")), "failed")
        self.assertEqual(self.controller._code_attempts, attempts)
        self.assertEqual(sum(call[0] == "sign_in" for call in client.calls), 1)
        self.now += 60
        self.assertEqual(self.perform(lambda: self.controller.submit_code("12345")), "authenticated")

    def test_unknown_server_failure_ends_challenge_without_replay(self):
        client = self.request()
        self.resend_results = [errors.RpcCallFailError(None)]
        self.assertEqual(self.perform(lambda: self.controller.resend_code(self.config, PHONE)), "failed")
        self.assertIsNone(self.controller._phone_code_hash)
        self.assertEqual(client._phone_code_hash, {})
        self.assertTrue(client.disconnected)
        self.assertIn("结果未确认", self.failures[-1])
        self.assertIn("本机", self.failures[-1])
        self.assertEqual(sum(call[0] == "raw_request" for call in client.calls), 1)

    @unittest.skipUnless(os.name == "nt", "Windows path case normalization")
    def test_same_session_case_cannot_bypass_server_cooldown(self):
        self.send_results = [errors.FloodWaitError(None, 90)]
        self.assertEqual(self.perform(lambda: self.controller.request_code(self.config, PHONE)), "failed")
        self.perform(self.controller.cancel_authentication)
        changed_case = replace(self.config, session=type(self.config.session)(str(self.config.session).upper()))
        self.assertEqual(self.perform(lambda: self.controller.request_code(changed_case, PHONE)), "failed")
        self.assertEqual(sum(call[0] == "send_code_request" for client in self.clients for call in client.calls), 1)


if __name__ == "__main__":
    unittest.main()
