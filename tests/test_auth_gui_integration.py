"""Exercise actual Qt/async worker login wiring; only Telegram I/O is fake."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QScrollArea
from telethon import errors, functions, types

import test_gui_worker as fixture
from tgmover.gui import MainWindow


class LoginWindowWorkerTests(unittest.TestCase):
    setUpClass = classmethod(fixture.GuiWorkerTests.setUpClass.__func__)
    wait_for = fixture.GuiWorkerTests.wait_for
    perform = fixture.GuiWorkerTests.perform
    dispose = fixture.GuiWorkerTests.dispose

    def setUp(self):
        fixture.GuiWorkerTests.setUp(self)
        self.authorized = False
        self.deliveries = []
        self.code_requests = 0
        self.resends = 0
        self.initial_timeout = 0
        self.resend_failure = None
        self.network_calls = []
        self.stack.enter_context(patch.dict(os.environ, {key: value for key, value in os.environ.items()
            if key not in {"API_ID", "API_HASH", "TGMOVER_APP_CREDENTIALS"}}, clear=True))
        self.stack.enter_context(patch("tgmover.credentials.application_directory", return_value=self.base))
        self.stack.enter_context(patch("tgmover.credentials.bundle_directory", return_value=None))

        async def telegram_rpc(client, request):
            self.network_calls.append(type(request).__name__)
            if isinstance(request, functions.auth.ResendCodeRequest):
                self.resends += 1
                client.record("resend", request.phone_number, request.phone_code_hash)
                if self.resend_failure is not None:
                    raise self.resend_failure
                return types.auth.SentCode(types.auth.SentCodeTypeSms(length=5), "new-hash",
                                           next_type=None, timeout=None)
            if isinstance(request, functions.auth.CancelCodeRequest):
                client.record("cancel_code", request.phone_number, request.phone_code_hash)
                return True
            raise AssertionError(type(request).__name__)

        self.stack.enter_context(patch.object(fixture.FakeTelegramClient, "__call__", telegram_rpc, create=True))
        self.path = self.base / "config.toml"
        self.window = MainWindow(config_path=self.path, controller=self.controller)
        self.window.show()
        self.application.processEvents()
        self.window.api_id.setText(str(self.config.api_id))
        self.window.api_hash.setText(self.config.api_hash)
        self.window.fields["telegram.session"].setText(str(self.config.session))
        self.window.phone.setText("+639171234567")
        self.controller.code_delivery.connect(self.deliveries.append)
        self.addCleanup(self.close_window)

    def client_factory(self, *args, **kwargs):
        client = fixture.GuiWorkerTests.client_factory(self, *args, **kwargs)
        client._phone_code_hash = {}
        session = Path(str(client.session))
        if session.suffix != ".session":
            session = Path(str(session) + ".session")
        session.touch()

        async def send(phone):
            self.code_requests += 1
            client.record("send_code_request", phone)
            client._phone_code_hash[phone.lstrip("+")] = "old-hash"
            return types.auth.SentCode(types.auth.SentCodeTypeApp(length=5), "old-hash",
                                       next_type=types.auth.CodeTypeSms(), timeout=self.initial_timeout)

        client.send_code_request = send
        return client

    def close_window(self):
        self.window._closed = True
        self.window.close()
        self.window.deleteLater()
        self.application.processEvents()

    def click_finished(self, button, outcome):
        count = len(self.finished)
        parent = button.parentWidget()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                parent.ensureWidgetVisible(button)
                break
            parent = parent.parentWidget()
        self.application.processEvents()
        self.assertTrue(button.isEnabled(), button.text())
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.wait_for(lambda: len(self.finished) > count and not self.controller.busy)
        self.assertEqual(self.finished[-1], outcome, self.failures)
        self.application.processEvents()

    def test_invalid_code_can_resend_then_authenticate_with_new_hash(self):
        self.click_finished(self.window.code_button, "code_sent")
        client = self.clients[-1]
        self.assertFalse(self.window.phone.isEnabled())
        self.assertTrue(self.window.resend_button.isVisible())
        self.assertTrue(self.window.resend_button.isEnabled())
        self.assertIn("Telegram", self.window.code_delivery_label.text())
        client.sign_in_errors = [errors.PhoneCodeInvalidError(None)]
        self.window.code.setText("00000")
        self.click_finished(self.window.login_button, "failed")
        self.assertEqual(self.window.code.text(), "")
        self.assertTrue(self.window.resend_button.isEnabled())
        self.assertIn("验证码", self.window.auth_status.text())
        self.click_finished(self.window.resend_button, "code_sent")
        self.assertEqual(self.resends, 1)
        self.assertEqual(self.code_requests, 1)
        self.assertFalse(self.window.phone.isEnabled())
        self.assertIn("短信", self.window.code_delivery_label.text())
        self.window.code.setText("12345")
        self.click_finished(self.window.login_button, "authenticated")
        submitted = [call[1] for call in client.calls if call[0] == "sign_in"]
        self.assertEqual(submitted[-1]["phone_code_hash"], "new-hash")
        self.assertFalse(self.window.login_details.isVisible())
        self.assertFalse(self.window.resend_button.isVisible())
        self.assertTrue(self.window._account_authenticated)
        saved = self.path.read_text(encoding="utf-8")
        for secret in ("639171234567", "new-hash", "old-hash", "00000"):
            self.assertNotIn(secret, saved)
            self.assertNotIn(secret, self.window.log_view.toPlainText())

    def test_cancel_then_get_code_starts_fresh_request_and_unlocks_phone(self):
        self.click_finished(self.window.code_button, "code_sent")
        first = self.clients[-1]
        first.sign_in_errors = [errors.PhoneCodeInvalidError(None)]
        self.window.code.setText("00000")
        self.click_finished(self.window.login_button, "failed")
        self.click_finished(self.window.connect_button, "auth_cancelled")
        self.assertFalse(self.window._auth_challenge)
        self.assertTrue(self.window.phone.isEnabled())
        self.assertTrue(self.window.code_button.isEnabled())
        self.assertFalse(self.window.resend_button.isVisible())
        self.assertEqual(self.window.code.text(), "")
        self.click_finished(self.window.code_button, "code_sent")
        self.assertEqual(self.code_requests, 2)
        self.assertEqual(self.resends, 0)
        self.assertEqual(self.window._challenge_identity, self.window._auth_identity())
        self.assertTrue(self.window.resend_button.isEnabled())

    def test_two_factor_hides_resend_and_cannot_start_another_code_chain(self):
        self.click_finished(self.window.code_button, "code_sent")
        self.clients[-1].sign_in_errors = [errors.SessionPasswordNeededError(None)]
        self.window.code.setText("12345")
        self.click_finished(self.window.login_button, "password_required")
        self.assertTrue(self.window.password_row.isVisible())
        self.assertFalse(self.window.resend_button.isVisible())
        self.assertFalse(self.window.code_button.isVisible())
        self.assertFalse(self.window.code_delivery_label.isVisible())
        self.assertIn("两步验证", self.window.password_row.title())
        self.assertIn("验证码已通过", self.window.auth_status.text())
        self.assertEqual(self.code_requests, 1)
        self.assertEqual(self.resends, 0)
        self.assertTrue(self.window._auth_challenge)
        self.assertEqual(self.window.code.text(), "")

    def test_two_factor_cancel_restores_visible_first_step_and_fresh_code(self):
        self.click_finished(self.window.code_button, "code_sent")
        self.clients[-1].sign_in_errors = [errors.SessionPasswordNeededError(None)]
        self.window.code.setText("12345")
        self.click_finished(self.window.login_button, "password_required")
        self.assertFalse(self.window.code_button.isVisible())
        self.click_finished(self.window.connect_button, "auth_cancelled")
        self.assertFalse(self.window.password_row.isVisible())
        self.assertTrue(self.window.code_button.isVisible())
        self.assertTrue(self.window.code.isVisible())
        self.assertTrue(self.window.login_button.isVisible())
        self.click_finished(self.window.code_button, "code_sent")
        self.assertEqual((self.code_requests, self.resends), (2, 0))

    def test_two_factor_password_completes_existing_challenge_without_another_code(self):
        self.initial_timeout = 60
        self.click_finished(self.window.code_button, "code_sent")
        client = self.clients[-1]
        client.sign_in_errors = [errors.SessionPasswordNeededError(None)]
        self.window.code.setText("12345")
        self.click_finished(self.window.login_button, "password_required")
        self.assertFalse(self.window.code_button.isVisible())
        self.assertFalse(self.window.resend_button.isVisible())
        self.assertFalse(self.window.code_delivery_label.isVisible())
        self.assertEqual(self.perform(lambda: self.controller.request_code(self.config, "+639171234567")), "failed")
        self.assertEqual(self.perform(lambda: self.controller.resend_code(self.config, "+639171234567")), "failed")
        self.assertTrue(self.window.password_row.isVisible())
        self.window.password.setText("two-factor-test")
        self.click_finished(self.window.password_button, "authenticated")
        submitted = [call[1] for call in client.calls if call[0] == "sign_in"]
        self.assertEqual(submitted[-1], {"password": "two-factor-test"})
        self.assertEqual((self.code_requests, self.resends), (1, 0))
        self.assertEqual(self.window.password.text(), "")
        self.assertFalse(self.window.login_details.isVisible())
        self.assertNotIn("two-factor-test", self.path.read_text(encoding="utf-8"))
        self.assertNotIn("two-factor-test", self.window.log_view.toPlainText())

    def test_real_timer_enables_manual_resend_without_sending_or_expiring_code(self):
        self.initial_timeout = 1
        self.click_finished(self.window.code_button, "code_sent")
        self.assertFalse(self.window.resend_button.isEnabled())
        self.assertTrue(self.window.login_button.isEnabled())
        self.wait_for(self.window.resend_button.isEnabled, timeout=3)
        self.assertTrue(self.window._auth_challenge)
        self.assertEqual((self.code_requests, self.resends), (1, 0))
        self.window.code.setText("12345")
        self.click_finished(self.window.login_button, "authenticated")
        self.assertEqual((self.code_requests, self.resends), (1, 0))

    def test_server_wait_disables_resend_and_countdown_preserves_error(self):
        self.click_finished(self.window.code_button, "code_sent")
        self.resend_failure = errors.FloodWaitError(None, capture=7)
        self.click_finished(self.window.resend_button, "failed")
        self.assertFalse(self.window.resend_button.isEnabled())
        self.assertFalse(self.window.phone.isEnabled())
        self.assertIn("等待", self.window.code_delivery_label.text())
        error = self.window.auth_status.text()
        self.assertIn("7", error)
        self.window._refresh_resend()
        self.assertEqual(self.window.auth_status.text(), error)
        self.assertEqual((self.code_requests, self.resends), (1, 1))


if __name__ == "__main__":
    unittest.main()
