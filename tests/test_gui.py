"""Offscreen Qt interaction tests with no Telegram worker or root config I/O."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QObject, QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QGroupBox, QLineEdit, QScrollArea, QSpinBox, QTimeEdit

from tgmover.gui import MainWindow, TopicPickerDialog


class FakeController(QObject):
    """The same signal boundary as the worker, without a thread or event loop."""

    status = Signal(str)
    log = Signal(str)
    authenticated = Signal(dict)
    code_sent = Signal(str)
    code_delivery = Signal(dict)
    password_required = Signal(str)
    challenge_ended = Signal()
    preview_ready = Signal(list)
    stats = Signal(dict)
    finished = Signal(str)
    failed = Signal(str)
    busy_changed = Signal(bool)
    task_phase = Signal(dict)
    chats_ready = Signal(list)
    topics_ready = Signal(dict)
    cleanup_ready = Signal(dict)
    pending_ready = Signal(list)

    def __init__(self):
        super().__init__()
        self.busy = False
        self.calls = []
        self.stop_count = 0
        self.shutdown_count = 0
        self.cancel_count = 0

    def connect_account(self, config):
        self.calls.append(("connect", config))
        return True

    def request_code(self, config, phone):
        self.calls.append(("code", config, phone))
        return True

    def submit_code(self, code):
        self.calls.append(("login", code))
        return True

    def resend_code(self, config, phone):
        self.calls.append(("resend", config, phone))
        return True

    def submit_password(self, password):
        self.calls.append(("password", password))
        return True

    def cancel_authentication(self):
        self.cancel_count += 1
        return True

    def preview(self, config, limit=50):
        self.calls.append(("preview", config, limit))
        return True

    def list_chats(self, config):
        self.calls.append(("list_chats", config))
        return True

    def list_topics(self, config, peer):
        self.calls.append(("list_topics", config, peer))
        return True

    def cleanup_downloads(self, config):
        self.calls.append(("cleanup_downloads", config))
        return True

    def list_pending(self, config):
        self.calls.append(("list_pending", config))
        return True

    def resolve_pending(self, config, route, key, scope):
        self.calls.append(("resolve_pending", config, route, key, scope))
        return True

    def run_task(self, config, once=True, check_only=False):
        self.calls.append(("run", config, once, check_only))
        return True

    def stop(self):
        self.stop_count += 1

    def shutdown(self):
        self.shutdown_count += 1

    def set_busy(self, busy):
        self.busy = busy
        self.busy_changed.emit(busy)


class MainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.base = Path(self.temp.name)
        self.path = self.base / "local-test-config.toml"
        clean_environment = {key: value for key, value in os.environ.items() if key not in {"API_ID", "API_HASH", "TGMOVER_APP_CREDENTIALS"}}
        self.environment = patch.dict(os.environ, clean_environment, clear=True)
        self.environment.start()
        self.application_directory = patch("tgmover.credentials.application_directory", return_value=self.base)
        self.application_directory.start()
        self.bundle_directory = patch("tgmover.credentials.bundle_directory", return_value=None)
        self.bundle_directory.start()
        self.controller = FakeController()
        # If construction accidentally ignores the injected controller, fail
        # before any real worker/thread/Telegram connection is constructed.
        self.worker_guard = patch("tgmover.gui.GuiController", side_effect=AssertionError("A GUI test must not start the network worker"))
        self.worker_guard.start()
        self.window = MainWindow(config_path=self.path, controller=self.controller)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window._closed = True
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.worker_guard.stop()
        self.bundle_directory.stop()
        self.application_directory.stop()
        self.environment.stop()
        self.temp.cleanup()

    def click(self, widget):
        parent = widget.parentWidget()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                parent.ensureWidgetVisible(widget)
                break
            parent = parent.parentWidget()
        self.app.processEvents()
        # A wide checkbox only treats its indicator/text as clickable; its
        # geometric center can fall in otherwise inactive whitespace.
        point = QPoint(8, widget.height() // 2) if isinstance(widget, QCheckBox) else widget.rect().center()
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=point)
        self.app.processEvents()

    def credentials(self):
        self.window.api_id.setText("12345")
        self.window.api_hash.setText("a" * 32)

    def valid_task(self):
        self.credentials()
        self.window.fields["sync.source"].setText("@actual_test_source")
        self.window.fields["sync.target"].setText("@actual_test_target")

    def saved(self):
        with self.path.open("rb") as handle:
            return tomllib.load(handle)

    def touch_session(self):
        path = self.window._session_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def authenticated_account(self, identifier, label):
        path = self.touch_session()
        self.controller.authenticated.emit({"id": identifier, "name": label})
        return path

    def test_first_launch_opens_without_credentials_or_automatic_file_writes(self):
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.window.api_id.text())
        self.assertFalse(self.window.api_hash.text())
        self.assertFalse(self.path.exists())
        self.assertFalse(self.controller.calls)
        self.assertEqual(self.window.config_path, self.path.resolve())
        self.assertTrue(self.window.fields["filters.enabled"].isChecked())
        self.assertTrue(self.window.app_settings_panel.isVisible())
        self.assertFalse(self.window._application_ready)
        self.assertIn("尚未配置", self.window.application_status.text())

    def test_adding_account_cannot_accidentally_start_a_task_as_previous_account(self):
        self.valid_task()
        original = self.authenticated_account(42, "Account A")
        self.click(self.window.add_account_button)
        calls = len(self.controller.calls)
        for action in ("run", "check", "preview"):
            with self.subTest(action=action):
                self.window._action(action)
                self.assertEqual(len(self.controller.calls), calls)
                self.assertEqual(self.window._session_path(), original)
                self.assertIn("完成新账号登录", self.window.auth_status.text())

    def test_unfinished_code_challenge_cannot_start_other_network_tasks(self):
        self.valid_task()
        self.window.phone.setText("+639171234567")
        self.click(self.window.code_button)
        self.controller.code_sent.emit("已请求验证码")
        self.window.code.setText("12345")
        calls = len(self.controller.calls)
        for action in ("run", "check", "preview"):
            with self.subTest(action=action):
                self.window._action(action)
                self.assertEqual(len(self.controller.calls), calls)
                self.assertEqual(self.window.code.text(), "12345")
                self.assertTrue(self.window._auth_challenge)

    def test_saved_application_settings_collapse_and_second_window_reuses_them(self):
        self.credentials()
        self.assertTrue(self.window.app_settings_panel.isVisible())
        self.click(self.window.save_app_settings_button)
        self.assertTrue(self.window._application_ready)
        self.assertFalse(self.window.app_settings_panel.isVisible())
        self.assertIn("本机配置", self.window.application_status.text())
        second = MainWindow(config_path=self.path, controller=FakeController())
        second.show()
        self.app.processEvents()
        try:
            self.assertTrue(second._application_ready)
            self.assertFalse(second.app_settings_panel.isVisible())
            self.assertIn("本机配置", second.application_status.text())
            self.assertFalse(second.phone.text())
            self.assertFalse(second.code.text())
            self.assertTrue(second.api_hash.text() == "a" * 32)
        finally:
            second._closed = True
            second.close()
            second.deleteLater()

    def test_environment_credentials_enable_phone_only_login_without_prefilling_fields(self):
        with patch.dict(os.environ, {"API_ID": "12345", "API_HASH": "b" * 32}):
            self.window._load()
            self.assertTrue(self.window._application_ready)
            self.assertFalse(self.window.app_settings_panel.isVisible())
            self.assertIn("环境变量", self.window.application_status.text())
            self.assertFalse(self.window.api_id.text())
            self.assertFalse(self.window.api_hash.text())
            self.window.phone.setText("+639171234567")
            self.click(self.window.code_button)
            self.assertEqual(self.controller.calls[-1][0], "code")
            self.assertTrue(self.saved()["telegram"]["api_id"] == 0)
            self.assertFalse(self.saved()["telegram"]["api_hash"])

    def test_software_credentials_are_used_without_copying_values_into_task_configuration(self):
        (self.base / "app_credentials.toml").write_text('[telegram]\napi_id = 12345\napi_hash = "' + "c" * 32 + '"\n', encoding="utf-8")
        self.window._load()
        self.assertTrue(self.window._application_ready)
        self.assertFalse(self.window.app_settings_panel.isVisible())
        self.assertIn("软件预置", self.window.application_status.text())
        self.assertFalse(self.window.api_id.text())
        self.assertFalse(self.window.api_hash.text())
        self.window.phone.setText("+639171234567")
        self.click(self.window.code_button)
        self.assertEqual(self.controller.calls[-1][0], "code")
        self.assertTrue(self.controller.calls[-1][1].api_hash == "c" * 32)
        self.assertFalse(self.saved()["telegram"]["api_hash"])

    def test_application_settings_can_be_expanded_without_revealing_hash_by_default(self):
        self.credentials()
        self.click(self.window.save_button)
        self.assertFalse(self.window.app_settings_panel.isVisible())
        self.click(self.window.app_settings_toggle)
        self.assertTrue(self.window.app_settings_panel.isVisible())
        self.assertEqual(self.window.api_hash.echoMode(), QLineEdit.EchoMode.Password)
        self.click(self.window.show_hash)
        self.assertEqual(self.window.api_hash.echoMode(), QLineEdit.EchoMode.Normal)
        self.click(self.window.app_settings_toggle)
        self.assertFalse(self.window.app_settings_panel.isVisible())
        self.assertEqual(self.window.api_hash.echoMode(), QLineEdit.EchoMode.Password)

    def test_navigation_switches_all_four_pages_with_keyboard(self):
        self.window.navigation.setFocus()
        for expected in (1, 2, 3):
            QTest.keyClick(self.window.navigation, Qt.Key.Key_Down)
            self.app.processEvents()
            self.assertEqual(self.window.stack.currentIndex(), expected)
        QTest.keyClick(self.window.navigation, Qt.Key.Key_Home)
        self.assertEqual(self.window.stack.currentIndex(), 0)
        self.assertFalse(self.controller.calls)

    def test_save_keeps_application_credentials_but_excludes_phone_and_code(self):
        self.credentials()
        self.window.phone.setText("+15550001111")
        self.window.code.setText("24680")
        self.click(self.window.save_button)
        saved = self.saved()
        self.assertTrue(saved["telegram"]["api_id"] == 12345)
        self.assertTrue(saved["telegram"]["api_hash"] == "a" * 32)
        content = self.path.read_text(encoding="utf-8")
        self.assertFalse(self.window.phone.text() in content)
        self.assertFalse(self.window.code.text() in content)
        self.assertNotIn("phone", saved["telegram"])
        self.assertNotIn("code", saved["telegram"])
        self.assertIn("已保存", self.window.feedback.text())
        self.assertFalse(self.controller.calls)

    def test_incomplete_first_draft_can_be_saved_from_interface(self):
        self.click(self.window.save_button)
        self.assertTrue(self.path.is_file())
        self.assertEqual(self.saved()["telegram"]["api_id"], 0)
        self.assertEqual(self.saved()["telegram"]["api_hash"], "")
        self.assertFalse(self.controller.calls)

    def test_application_hash_display_toggle_changes_echo_mode(self):
        self.assertEqual(self.window.api_hash.echoMode(), QLineEdit.EchoMode.Password)
        self.click(self.window.show_hash)
        self.assertEqual(self.window.api_hash.echoMode(), QLineEdit.EchoMode.Normal)
        self.click(self.window.show_hash)
        self.assertEqual(self.window.api_hash.echoMode(), QLineEdit.EchoMode.Password)

    def test_download_only_disables_target_and_forces_comment_backup(self):
        mode = self.window.fields["sync.mode"]
        comments = self.window.fields["sync.comment_mode"]
        mode.setCurrentIndex(mode.findData("download_only"))
        self.assertFalse(self.window.fields["sync.target"].isEnabled())
        self.assertFalse(comments.isEnabled())
        self.assertEqual(comments.currentData(), "download_only")
        self.assertEqual(self.window.collect_draft()["sync"]["comment_mode"], "download_only")
        mode.setCurrentIndex(mode.findData("reupload"))
        self.assertTrue(self.window.fields["sync.target"].isEnabled())
        self.assertTrue(comments.isEnabled())

    def test_offline_ad_sample_is_blocked_and_keyword_allowlist_can_release_it(self):
        self.window.navigation.setCurrentRow(1)
        self.window.sample_text.setPlainText("官方测试公告：商务合作请联系 https://ads.example.com")
        self.click(self.window.sample_button)
        self.assertIn("将被屏蔽", self.window.sample_result.text())
        self.window.fields["filters.allowed_keywords"].setPlainText("官方测试公告")
        self.click(self.window.sample_button)
        self.assertIn("未命中", self.window.sample_result.text())
        self.assertFalse(self.controller.calls)
        self.assertFalse(self.path.exists())

    def test_offline_filter_can_be_disabled_and_invalid_regex_is_reported(self):
        self.window.navigation.setCurrentRow(1)
        self.window.fields["filters.enabled"].setChecked(False)
        self.click(self.window.sample_button)
        self.assertIn("未命中", self.window.sample_result.text())
        self.window.fields["filters.enabled"].setChecked(True)
        self.window.fields["filters.regex_patterns"].setPlainText("[")
        self.click(self.window.sample_button)
        self.assertIn("正则", self.window.sample_result.text())
        self.assertFalse(self.controller.calls)

    def preview_rows(self):
        return [
            {"chat_id": -1001234567, "source_id": 10, "grouped_id": 777, "text": "first photo", "media": "photo", "blocked": False, "reasons": []},
            {"chat_id": -1001234567, "source_id": 11, "grouped_id": 777, "text": "second photo", "media": "photo", "blocked": False, "reasons": []},
            {"chat_id": -1001234567, "source_id": 12, "grouped_id": None, "text": "广告合作", "media": "文字", "blocked": True, "reasons": ["命中关键词"]},
        ]

    def test_preview_displays_decisions_without_triggering_transmission(self):
        self.window.navigation.setCurrentRow(1)
        self.controller.preview_ready.emit(self.preview_rows())
        table = self.window.preview_table
        self.assertEqual(table.rowCount(), 3)
        self.assertEqual(table.item(0, 3).text(), "通过")
        self.assertEqual(table.item(2, 3).text(), "屏蔽")
        self.assertIn("关键词", table.item(2, 4).text())
        self.assertEqual(table.item(0, 0).data(Qt.ItemDataRole.UserRole)["source_id"], 10)
        self.assertIn("尚未发送", self.window.feedback.text())
        self.assertFalse(self.controller.calls)

    def test_manual_block_expands_to_entire_album_and_persists_source_scoped_keys(self):
        self.window.navigation.setCurrentRow(1)
        self.controller.preview_ready.emit(self.preview_rows())
        self.window.preview_table.selectRow(0)
        self.click(self.window.block_selected_button)
        keys = self.saved()["filters"]["blocked_message_keys"]
        self.assertEqual(set(keys), {"-1001234567:10", "-1001234567:11"})
        self.assertEqual(self.window.preview_table.item(0, 3).text(), "屏蔽")
        self.assertEqual(self.window.preview_table.item(1, 3).text(), "屏蔽")
        self.assertIn("手动", self.window.preview_table.item(1, 4).text())
        self.assertNotIn("-1001234567:12", keys)
        self.assertFalse(self.controller.calls)

    def test_manual_block_with_no_selection_shows_feedback_without_saving(self):
        self.window.navigation.setCurrentRow(1)
        self.click(self.window.block_selected_button)
        self.assertIn("选中", self.window.feedback.text())
        self.assertFalse(self.path.exists())
        self.assertFalse(self.controller.calls)

    def test_missing_credentials_never_submit_network_operations(self):
        for name in ("connect_button", "code_button", "login_button", "check_button", "start_button", "task_preview_button"):
            with self.subTest(action=name):
                self.window.navigation.setCurrentRow(0)
                self.click(getattr(self.window, name))
                self.assertFalse(self.controller.calls)
                self.assertTrue(self.window.feedback.text())
        self.window.navigation.setCurrentRow(1)
        self.click(self.window.preview_button)
        self.assertFalse(self.controller.calls)

    def test_send_code_and_submit_code_only_send_transient_login_input_to_controller(self):
        self.credentials()
        self.window.phone.setText("+15550001111")
        self.click(self.window.code_button)
        self.assertEqual(len(self.controller.calls), 1)
        self.assertEqual(self.controller.calls[0][0], "code")
        self.assertTrue(self.controller.calls[0][2] == self.window.phone.text())
        self.controller.code_sent.emit("已请求验证码")
        self.assertIn("验证码", self.window.feedback.text())
        QTest.keyClicks(self.window.code, "24680")
        self.click(self.window.login_button)
        self.assertEqual(self.controller.calls[-1][0], "login")
        self.assertTrue(self.controller.calls[-1][1] == "24680")
        self.assertFalse(self.window.code.text())
        saved = self.path.read_text(encoding="utf-8")
        self.assertFalse("+15550001111" in saved)
        self.assertFalse("24680" in saved)

    def test_global_phone_formats_including_philippines_are_canonicalized_before_request(self):
        self.credentials()
        for original, canonical in (
            ("+63 917-123-4567", "+639171234567"),
            ("+1 (202) 555-0123", "+12025550123"),
            ("0044 7700 900123", "+447700900123"),
            ("+7 (912) 345-67-89", "+79123456789"),
            ("+49 151 12345678", "+4915112345678"),
            ("＋８５２ ５１２３ ４５６７", "+85251234567"),
        ):
            with self.subTest(prefix=canonical[:3]):
                before = len(self.controller.calls)
                self.window.phone.setText(original)
                self.assertEqual(len(self.controller.calls), before)
                self.click(self.window.code_button)
                self.assertEqual(self.controller.calls[-1][0], "code")
                self.assertTrue(self.controller.calls[-1][2] == canonical)
                self.assertTrue(self.window.phone.text() == canonical)
                self.assertIn("正在", self.window.auth_status.text())
                self.assertFalse(canonical in self.path.read_text(encoding="utf-8"))

    def test_get_code_without_api_credentials_shows_error_in_account_area(self):
        self.window.phone.setText("+63 917-123-4567")
        self.click(self.window.code_button)
        self.assertFalse(self.controller.calls)
        self.assertIn("应用设置", self.window.auth_status.text())
        self.assertNotIn("must be", self.window.auth_status.text())
        self.assertIn("#b63145", self.window.auth_status.styleSheet())
        self.assertTrue(self.window.code_button.isEnabled())

    def test_invalid_national_phone_is_explained_inline_without_a_request(self):
        self.credentials()
        self.window.phone.setText("09171234567")
        self.click(self.window.code_button)
        self.assertFalse(self.controller.calls)
        self.assertIn("国际区号", self.window.auth_status.text())
        self.assertFalse(self.path.exists())

    def test_enter_explicitly_submits_normalized_code_and_shows_inline_progress(self):
        self.begin_challenge()
        before = len(self.controller.calls)
        self.window.code.setText("１２ ３-４５")
        self.assertEqual(len(self.controller.calls), before)
        self.window.code.setFocus()
        QTest.keyClick(self.window.code, Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertEqual(len(self.controller.calls), before + 1)
        self.assertEqual(self.controller.calls[-1][0], "login")
        self.assertTrue(self.controller.calls[-1][1] == "12345")
        self.assertFalse(self.window.code.text())
        self.assertIn("正在验证", self.window.auth_status.text())

    def test_rejected_login_dispatch_retains_code_and_shows_inline_reason(self):
        self.begin_challenge()
        self.window.code.setText("12 345")
        with patch.object(self.controller, "submit_code", return_value=False) as submit:
            self.click(self.window.login_button)
            submit.assert_called_once()
        self.assertTrue(self.window.code.text() == "12 345")
        self.assertIn("未提交", self.window.auth_status.text())
        self.assertIn("已保留", self.window.auth_status.text())

    def delivery(self, *, wait=0, available=True, channel="Telegram 官方客户端", next_channel="短信"):
        self.controller.code_delivery.emit({"can_resend": available and wait == 0,
                                            "resend_available": available, "resend_after": wait,
                                            "delivery": channel, "next_delivery": next_channel,
                                            "message": "由 Telegram 决定发送渠道，请查看对应客户端。"})

    def test_code_delivery_countdown_unblocks_explicit_resend_without_automatic_requests(self):
        self.begin_challenge()
        self.delivery(wait=30)
        before = len(self.controller.calls)
        self.assertTrue(self.window.resend_button.isVisible())
        self.assertFalse(self.window.resend_button.isEnabled())
        self.assertIn("30", self.window.resend_button.text())
        self.assertIn("Telegram 官方客户端", self.window.code_delivery_label.text())
        self.assertIn("短信", self.window.code_delivery_label.text())
        self.window._resend_deadline = 0
        self.window._refresh_resend()
        self.assertTrue(self.window.resend_button.isEnabled())
        self.assertEqual(len(self.controller.calls), before)
        self.window.code.setText("00000")
        self.click(self.window.resend_button)
        self.assertEqual(self.controller.calls[-1][0], "resend")
        self.assertEqual(self.controller.calls[-1][2], self.window.phone.text())
        self.assertFalse(self.window.code.text())
        self.assertFalse(self.window.phone.isEnabled())
        self.assertFalse(self.window.api_hash.isEnabled())
        self.assertFalse(self.window.account_selector.isEnabled())
        self.assertFalse(self.window.resend_button.isEnabled())
        self.controller.code_sent.emit("验证码已按 Telegram 指定渠道重新申请。")
        self.delivery(wait=20)
        self.assertIn("20", self.window.resend_button.text())

    def test_wrong_code_clears_attempt_and_keeps_resend_available_without_hiding_error(self):
        self.begin_challenge()
        self.delivery()
        self.window.code.setText("00000")
        self.click(self.window.login_button)
        self.assertFalse(self.window.code.text())
        message = "验证码不正确，还可尝试2次；未收到请重新获取。"
        self.controller.failed.emit(message)
        self.controller.set_busy(False)
        self.window._refresh_resend()
        self.assertEqual(self.window.auth_status.text(), message)
        self.assertTrue(self.window.resend_button.isEnabled())
        self.assertTrue(self.window._auth_challenge)

    def test_busy_or_rejected_resend_preserves_input_and_identity(self):
        self.begin_challenge()
        self.delivery()
        self.window.code.setText("12345")
        before = len(self.controller.calls)
        self.controller.set_busy(True)
        self.window._resend_code()
        self.assertEqual(len(self.controller.calls), before)
        self.assertEqual(self.window.code.text(), "12345")
        self.assertTrue(self.window.auth_stop_button.isEnabled())
        self.controller.set_busy(False)
        with patch.object(self.controller, "resend_code", return_value=False):
            self.click(self.window.resend_button)
        self.assertEqual(self.window.code.text(), "12345")
        self.assertTrue(self.window._auth_challenge)
        self.assertTrue(self.window.resend_button.isEnabled())

    def test_cancel_preserves_server_wait_and_restores_fresh_request_entry_after_countdown(self):
        self.begin_challenge()
        self.delivery(wait=30)
        self.click(self.window.connect_button)
        self.assertFalse(self.window._auth_challenge)
        self.assertFalse(self.window.resend_button.isVisible())
        self.assertFalse(self.window.code_button.isEnabled())
        self.assertTrue(self.window.code_delivery_label.isVisible())
        self.assertIn("30", self.window.code_delivery_label.text())
        self.assertIn("发送验证码", self.window.auth_status.text())
        self.window._resend_deadline = 0
        self.window._refresh_resend()
        self.assertTrue(self.window.code_button.isEnabled())
        self.assertIn("重新申请", self.window.code_delivery_label.text())
        before = len(self.controller.calls)
        self.click(self.window.code_button)
        self.assertEqual(len(self.controller.calls), before + 1)
        self.assertEqual(self.controller.calls[-1][0], "code")

    def test_no_next_channel_and_expired_challenge_leave_a_visible_way_to_start_again(self):
        self.begin_challenge()
        self.delivery(available=False, next_channel="")
        self.assertFalse(self.window.resend_button.isEnabled())
        self.assertIn("没有可用的下一渠道", self.window.code_delivery_label.text())
        self.controller.challenge_ended.emit()
        self.controller.failed.emit("验证码已过期，请重新申请。")
        self.controller.set_busy(False)
        self.assertTrue(self.window.code_button.isEnabled())
        self.assertFalse(self.window.resend_button.isVisible())
        self.assertIn("重新申请", self.window.code_delivery_label.text())
        self.assertIn("已过期", self.window.auth_status.text())

    def test_two_factor_step_hides_resend_and_delivery_then_success_clears_transient_state(self):
        self.begin_challenge()
        self.delivery(wait=30)
        self.controller.password_required.emit("请输入两步验证密码。")
        self.assertFalse(self.window.resend_button.isVisible())
        self.assertFalse(self.window.code_delivery_label.isVisible())
        before = len(self.controller.calls)
        self.window._resend_code()
        self.assertEqual(len(self.controller.calls), before)
        self.authenticated_account(101, "Account A")
        self.assertIsNone(self.window._code_delivery_state)
        self.assertFalse(self.window._resend_timer.isActive())
        self.assertFalse(self.window.login_details.isVisible())

    def test_countdown_uses_available_state_and_legacy_metadata_fallback(self):
        self.begin_challenge()
        self.controller.code_delivery.emit({"can_resend": True, "resend_after": 0, "delivery": "Telegram 客户端"})
        self.assertTrue(self.window.resend_button.isEnabled())
        self.delivery(wait=10)
        self.assertFalse(self.window.resend_button.isEnabled())
        self.window._resend_deadline = 0
        self.window._refresh_resend()
        self.assertTrue(self.window.resend_button.isEnabled())

    def test_busy_login_attempt_preserves_code_and_current_operation_tracking(self):
        self.begin_challenge()
        self.window.code.setText("12345")
        self.window._auth_operation = "code"
        self.controller.set_busy(True)
        before = len(self.controller.calls)
        self.window._action("login")
        self.assertTrue(self.window.code.text() == "12345")
        self.assertEqual(len(self.controller.calls), before)
        self.assertEqual(self.window._auth_operation, "code")
        self.assertIn("验证码已保留", self.window.auth_status.text())

    def test_invalid_code_is_reported_beside_login_without_reaching_controller(self):
        self.begin_challenge()
        self.window.code.setText("Your code is 12345")
        before = len(self.controller.calls)
        self.click(self.window.login_button)
        self.assertEqual(len(self.controller.calls), before)
        self.assertIn("验证码格式无效", self.window.auth_status.text())
        self.assertTrue(self.window.code.text())

    def test_request_progress_and_network_error_stay_visible_in_account_area(self):
        self.credentials()
        self.window.phone.setText("+639171234567")
        self.click(self.window.code_button)
        self.controller.set_busy(True)
        self.assertIn("正在获取", self.window.code_button.text())
        self.assertFalse(self.window.code_button.isEnabled())
        self.assertTrue(self.window.auth_stop_button.isEnabled())
        self.controller.status.emit("正在连接 Telegram 并请求验证码…")
        self.assertIn("正在连接", self.window.auth_status.text())
        self.click(self.window.auth_stop_button)
        self.assertEqual(self.controller.stop_count, 1)
        self.assertIn("停止", self.window.auth_status.text())
        self.controller.failed.emit("连接超时，请检查网络后手动重试。")
        self.controller.set_busy(False)
        self.assertIn("连接超时", self.window.auth_status.text())
        self.assertIn("#b63145", self.window.auth_status.styleSheet())
        self.assertTrue(self.window.code_button.isEnabled())
        self.assertEqual(self.window.code_button.text(), "发送验证码")
        self.assertFalse(self.window.auth_stop_button.isEnabled())

    def test_login_progress_changes_disabled_button_text_until_operation_finishes(self):
        self.begin_challenge()
        self.window.code.setText("12345")
        self.click(self.window.login_button)
        self.controller.set_busy(True)
        self.assertIn("正在验证", self.window.login_button.text())
        self.assertFalse(self.window.login_button.isEnabled())
        self.controller.failed.emit("验证码无效，还可尝试 2 次。")
        self.controller.set_busy(False)
        self.assertEqual(self.window.login_button.text(), "验证并登录")
        self.assertIn("验证码无效", self.window.auth_status.text())
        self.assertTrue(self.window._auth_challenge)

    def test_login_without_a_requested_challenge_is_rejected_even_with_a_code(self):
        self.credentials()
        self.window.code.setText("24680")
        self.click(self.window.login_button)
        self.assertFalse(self.controller.calls)
        self.assertIn("先发送验证码", self.window.feedback.text())

    def begin_challenge(self):
        self.credentials()
        self.window._show_login_management(True)
        self.window.phone.setText("+15550001111")
        self.click(self.window.code_button)
        self.controller.code_sent.emit("已请求验证码")

    def begin_password(self):
        self.begin_challenge()
        self.controller.password_required.emit("此账号开启了两步验证，请输入密码。")
        self.controller.set_busy(False)

    def test_two_factor_prompt_hides_code_and_keeps_account_identity_locked(self):
        self.assertFalse(self.window.password_row.isVisible())
        self.begin_password()
        self.assertTrue(self.window._password_pending)
        self.assertTrue(self.window._auth_challenge)
        self.assertTrue(self.window.password_row.isVisible())
        self.assertIsInstance(self.window.password_row, QGroupBox)
        self.assertIn("第2步", self.window.password_row.title())
        self.assertIn("验证码已通过", self.window.password_instruction.text())
        self.assertIn("Telegram", self.window.password_instruction.text())
        self.assertFalse(self.window.phone.isVisible())
        self.assertFalse(self.window.phone_label.isVisible())
        self.assertFalse(self.window.code_button.isVisible())
        self.assertFalse(self.window.application_status.isVisible())
        self.assertFalse(self.window.app_settings_toggle.isVisible())
        self.assertFalse(self.window.app_settings_panel.isVisible())
        self.assertTrue(self.window.password.isVisible())
        self.assertTrue(self.window.connect_button.isVisible())
        self.assertIs(self.window.connect_button.parentWidget(), self.window.password_row)
        self.assertFalse(self.window.code.isVisible())
        self.assertFalse(self.window.login_button.isVisible())
        self.assertFalse(self.window.code.isEnabled())
        self.assertFalse(self.window.login_button.isEnabled())
        self.assertEqual(self.window.password.echoMode(), QLineEdit.EchoMode.Password)
        self.assertFalse(self.window.account_selector.isEnabled())
        self.assertFalse(self.window.add_account_button.isEnabled())
        self.assertFalse(self.window.switch_account_button.isEnabled())
        self.assertFalse(self.window.api_hash.isEnabled())
        self.assertEqual(self.window.connect_button.text(), "取消本次登录")

    def test_two_factor_step_survives_busy_completion_and_keeps_first_step_hidden(self):
        self.begin_challenge()
        self.delivery(wait=60)
        self.controller.set_busy(True)
        self.controller.password_required.emit("请输入两步验证密码。")
        self.assertTrue(self.window.password_row.isVisible())
        self.assertFalse(self.window.password.isEnabled())
        self.assertFalse(self.window._resend_timer.isActive())
        self.assertFalse(self.window.code_delivery_label.text())
        self.controller.set_busy(False)
        self.controller.finished.emit("password_required")
        for widget in (self.window.phone, self.window.code, self.window.code_button,
                       self.window.login_button, self.window.resend_button, self.window.code_delivery_label):
            self.assertFalse(widget.isVisible())
        self.assertTrue(self.window.password_button.isEnabled())
        self.assertTrue(self.window.connect_button.isEnabled())
        self.assertIs(self.window.connect_button.parentWidget(), self.window.password_row)
        self.assertIn("验证码已通过", self.window.auth_status.text())
        before = len(self.controller.calls)
        self.window._action("code")
        self.window._resend_code()
        self.assertEqual(len(self.controller.calls), before)
        self.assertIn("第2步", self.window.auth_status.text())

    def test_two_factor_cancel_restores_first_step_layout_and_fresh_code_entry(self):
        self.begin_password()
        self.window.password.setText("  retained before rejected cancel  ")
        with patch.object(self.controller, "cancel_authentication", return_value=False):
            self.click(self.window.connect_button)
        self.assertTrue(self.window.password_row.isVisible())
        self.assertIs(self.window.connect_button.parentWidget(), self.window.password_row)
        self.assertEqual(self.window.password.text(), "  retained before rejected cancel  ")
        self.click(self.window.connect_button)
        self.assertFalse(self.window.password_row.isVisible())
        self.assertIs(self.window.connect_button.parentWidget(), self.window.login_details)
        for widget in (self.window.phone, self.window.code, self.window.code_button,
                       self.window.login_button, self.window.connect_button, self.window.app_settings_toggle):
            self.assertTrue(widget.isVisible())
        self.assertTrue(self.window.code_button.isEnabled())
        self.assertFalse(self.window.password.text())
        before = len(self.controller.calls)
        self.click(self.window.code_button)
        self.assertEqual(len(self.controller.calls), before + 1)
        self.assertEqual(self.controller.calls[-1][0], "code")

    def test_two_factor_terminal_failure_restores_first_step_and_keeps_error_after_busy(self):
        self.begin_password()
        self.controller.set_busy(True)
        self.controller.challenge_ended.emit()
        message = "两步验证已超时，请重新登录。"
        self.controller.failed.emit(message)
        self.controller.set_busy(False)
        self.controller.finished.emit("failed")
        self.assertFalse(self.window.password_row.isVisible())
        self.assertTrue(self.window.code_button.isVisible())
        self.assertIs(self.window.connect_button.parentWidget(), self.window.login_details)
        self.assertTrue(self.window.connect_button.isVisible())
        self.assertEqual(self.window.auth_status.text(), message)

    def test_delivery_label_uses_only_dynamic_wait_and_keeps_original_message_in_tooltip(self):
        self.begin_challenge()
        self.controller.code_delivery.emit({"can_resend": False, "resend_available": True,
                                            "resend_after": 45, "delivery": "Telegram 客户端",
                                            "message": "再次请求需等待60秒。"})
        message = "验证码不正确，请重新输入。"
        self.controller.failed.emit(message)
        self.window._refresh_resend()
        self.assertIn("45", self.window.code_delivery_label.text())
        self.assertNotIn("60", self.window.code_delivery_label.text())
        self.assertIn("60", self.window.code_delivery_label.toolTip())
        self.assertEqual(self.window.auth_status.text(), message)

    def test_password_enter_preserves_outer_spaces_and_never_saves_password_in_draft(self):
        self.begin_password()
        sample = "  offline sample password  "
        self.window.password.setText(sample)
        draft = self.window.collect_draft()
        self.assertFalse(any("password" in key for key in self.window.fields))
        self.assertFalse(sample in str(draft))
        self.click(self.window.save_button)
        self.assertFalse(sample in self.path.read_text(encoding="utf-8"))
        before = len(self.controller.calls)
        self.window.password.setFocus()
        QTest.keyClick(self.window.password, Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertEqual(len(self.controller.calls), before + 1)
        self.assertEqual(self.controller.calls[-1][0], "password")
        self.assertTrue(self.controller.calls[-1][1] == sample)
        self.assertFalse(self.window.password.text())
        self.assertIn("正在验证两步验证", self.window.auth_status.text())

    def test_wrong_password_retains_prompt_and_allows_manual_retry(self):
        self.begin_password()
        self.window.password.setText("offline test one")
        self.click(self.window.password_button)
        self.controller.failed.emit("两步验证密码不正确，还可尝试 2 次。")
        self.controller.set_busy(False)
        self.assertTrue(self.window._password_pending)
        self.assertTrue(self.window._auth_challenge)
        self.assertTrue(self.window.password_row.isVisible())
        self.assertTrue(self.window.password.isEnabled())
        self.assertFalse(self.window.switch_account_button.isEnabled())
        self.assertIn("密码不正确", self.window.auth_status.text())
        before = len(self.controller.calls)
        self.window.password.setText("offline test two")
        self.click(self.window.password_button)
        self.assertEqual(len(self.controller.calls), before + 1)
        self.assertEqual(self.controller.calls[-1][0], "password")

    def test_terminal_password_failure_clears_prompt_and_releases_identity(self):
        self.begin_password()
        self.window.password.setText("offline terminal test")
        self.click(self.window.password_button)
        self.controller.set_busy(True)
        self.controller.challenge_ended.emit()
        self.controller.failed.emit("两步验证连续 3 次失败，本次登录停止。")
        self.controller.set_busy(False)
        self.assertFalse(self.window._password_pending)
        self.assertFalse(self.window._auth_challenge)
        self.assertFalse(self.window.password.text())
        self.assertFalse(self.window.password_row.isVisible())
        self.assertTrue(self.window.code.isVisible())
        self.assertTrue(self.window.code.isEnabled())
        self.assertTrue(self.window.account_selector.isEnabled())
        self.assertTrue(self.window.add_account_button.isEnabled())
        self.assertIn("3 次", self.window.auth_status.text())

    def test_successful_two_factor_authentication_hides_and_clears_password(self):
        self.begin_password()
        self.window.password.setText("offline success test")
        self.authenticated_account(101, "Account A")
        self.assertFalse(self.window._password_pending)
        self.assertFalse(self.window._auth_challenge)
        self.assertFalse(self.window.password.text())
        self.assertFalse(self.window.password_row.isVisible())
        self.assertFalse(self.window.code.isVisible())
        self.assertFalse(self.window.login_details.isVisible())
        self.assertTrue(self.window.switch_account_button.isEnabled())
        self.assertIn("已连接", self.window.account_label.text())

    def test_cancel_password_clears_backend_challenge_and_all_transient_inputs(self):
        self.begin_password()
        self.window.password.setText("offline cancelled password")
        self.click(self.window.connect_button)
        self.assertEqual(self.controller.cancel_count, 1)
        self.assertFalse(self.window._password_pending)
        self.assertFalse(self.window._auth_challenge)
        self.assertFalse(self.window.password.text())
        self.assertFalse(self.window.password_row.isVisible())
        self.assertTrue(self.window.add_account_button.isEnabled())

    def test_rejected_password_or_cancel_dispatch_preserves_input_and_challenge(self):
        self.begin_password()
        sample = "  offline retained password  "
        self.window.password.setText(sample)
        with patch.object(self.controller, "submit_password", return_value=False):
            self.click(self.window.password_button)
        self.assertTrue(self.window.password.text() == sample)
        self.assertTrue(self.window._password_pending)
        with patch.object(self.controller, "cancel_authentication", return_value=False):
            self.click(self.window.connect_button)
        self.assertTrue(self.window.password.text() == sample)
        self.assertTrue(self.window._password_pending)
        self.assertTrue(self.window._auth_challenge)

    def test_busy_password_attempt_and_cancel_do_not_discard_input(self):
        self.begin_password()
        sample = "  offline busy password  "
        self.window.password.setText(sample)
        self.controller.set_busy(True)
        self.assertFalse(self.window.password.isEnabled())
        self.assertFalse(self.window.password_button.isEnabled())
        self.assertTrue(self.window.auth_stop_button.isEnabled())
        before = len(self.controller.calls)
        self.window._action("password")
        self.window._connect_or_cancel()
        self.assertEqual(len(self.controller.calls), before)
        self.assertEqual(self.controller.cancel_count, 0)
        self.assertTrue(self.window.password.text() == sample)
        self.controller.set_busy(False)
        self.assertTrue(self.window.password.isEnabled())

    def test_code_submission_is_rejected_during_password_step(self):
        self.begin_password()
        before = len(self.controller.calls)
        self.window.code.setText("12345")
        self.window._action("login")
        self.assertEqual(len(self.controller.calls), before)
        self.assertIn("两步验证密码", self.window.auth_status.text())

    def test_local_error_redacts_current_password_even_when_short(self):
        self.begin_password()
        self.window.password.setText("xy")
        message = self.window._safe_error(ValueError("offline xy example"))
        self.assertNotIn("xy", message)
        self.assertIn("已隐藏", message)

    def test_requested_challenge_locks_credentials_phone_and_session_through_busy_reset(self):
        self.begin_challenge()
        self.assertTrue(self.window._auth_challenge)
        locked = (self.window.api_id, self.window.api_hash, self.window.phone, self.window.fields["telegram.session"], self.window.code_button)
        for widget in locked:
            self.assertFalse(widget.isEnabled())
        self.assertIn("取消", self.window.connect_button.text())
        self.controller.set_busy(True)
        self.controller.set_busy(False)
        for widget in locked:
            self.assertFalse(widget.isEnabled())
        self.assertTrue(self.window.code.isEnabled())
        self.assertTrue(self.window.login_button.isEnabled())

    def test_cancel_challenge_unlocks_identity_and_prevents_stale_code_submission(self):
        self.begin_challenge()
        self.window.code.setText("24680")
        before = len(self.controller.calls)
        self.click(self.window.connect_button)
        self.assertFalse(self.window._auth_challenge)
        self.assertIsNone(self.window._challenge_identity)
        self.assertFalse(self.window.code.text())
        self.assertEqual(len(self.controller.calls), before)
        for widget in (self.window.api_id, self.window.api_hash, self.window.phone, self.window.fields["telegram.session"], self.window.code_button):
            self.assertTrue(widget.isEnabled())
        self.window.code.setText("24680")
        self.click(self.window.login_button)
        self.assertEqual(len(self.controller.calls), before)
        self.assertIn("先发送验证码", self.window.feedback.text())

    def test_authentication_clears_challenge_before_idle_restores_identity_editing(self):
        self.begin_challenge()
        self.touch_session()
        self.controller.authenticated.emit({"id": 1, "name": "Offline Test User"})
        self.controller.set_busy(False)
        self.assertFalse(self.window._auth_challenge)
        self.assertIsNone(self.window._challenge_identity)
        self.assertEqual(self.window.connect_button.text(), "恢复已有会话")
        self.assertTrue(self.window.api_id.isEnabled())
        self.assertTrue(self.window.api_hash.isEnabled())
        self.assertTrue(self.window.phone.isEnabled())
        self.assertTrue(self.window.fields["telegram.session"].isEnabled())

    def test_two_accounts_use_independent_sessions_and_switch_back_without_requesting_code(self):
        self.credentials()
        self.click(self.window.save_button)
        first = self.authenticated_account(101, "Account A")
        api_before = (self.window.api_id.text(), self.window.api_hash.text())
        self.window.phone.setText("+639171234567")
        self.window.code.setText("12345")
        self.click(self.window.add_account_button)
        self.assertTrue(self.window._new_account_mode)
        self.assertFalse(self.window.phone.text())
        self.assertFalse(self.window.code.text())
        self.assertEqual(self.window._session_path(), first)
        self.assertTrue((self.window.api_id.text(), self.window.api_hash.text()) == api_before)
        self.window.phone.setText("+44 7700 900123")
        self.click(self.window.code_button)
        second = self.window._session_path()
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists())
        self.assertEqual(self.controller.calls[-1][1].session, second)
        self.authenticated_account(202, "Account B")
        self.assertFalse(self.window._new_account_mode)
        accounts = self.window.account_store.list_accounts()
        self.assertEqual({entry["id"] for entry in accounts}, {101, 202})
        self.assertEqual(self.window.account_selector.currentData(), str(second))
        requests = sum(call[0] == "code" for call in self.controller.calls)
        self.window.account_selector.setCurrentIndex(self.window.account_selector.findData(str(first)))
        self.click(self.window.switch_account_button)
        self.assertEqual(self.controller.calls[-1][0], "connect")
        self.assertEqual(self.controller.calls[-1][1].session, first)
        self.assertEqual(sum(call[0] == "code" for call in self.controller.calls), requests)
        self.assertTrue((self.window.api_id.text(), self.window.api_hash.text()) == api_before)
        self.assertEqual(Path(self.saved()["telegram"]["session"]), first)
        self.assertTrue(first.exists() and second.exists())

    def test_account_changes_are_guarded_while_busy_or_waiting_for_a_code(self):
        self.credentials()
        first = self.authenticated_account(101, "Account A")
        self.controller.set_busy(True)
        self.assertFalse(self.window.account_selector.isEnabled())
        self.assertFalse(self.window.switch_account_button.isEnabled())
        self.assertFalse(self.window.add_account_button.isEnabled())
        self.window._add_account()
        self.window._switch_account()
        self.assertFalse(self.window._new_account_mode)
        self.assertEqual(self.window._session_path(), first)
        self.assertIn("停止", self.window.auth_status.text())
        self.controller.set_busy(False)
        self.begin_challenge()
        self.assertFalse(self.window.switch_account_button.isEnabled())
        self.assertFalse(self.window.add_account_button.isEnabled())
        self.assertFalse(self.window.app_settings_toggle.isEnabled())
        before = len(self.controller.calls)
        self.window._add_account()
        self.window._switch_account()
        self.assertEqual(len(self.controller.calls), before)
        self.assertIn("取消本次验证码", self.window.auth_status.text())
        self.assertEqual(self.window._session_path(), first)

    def test_new_account_request_failure_can_retry_without_changing_application_credentials(self):
        self.credentials()
        first = self.authenticated_account(101, "Account A")
        api_before = (self.window.api_id.text(), self.window.api_hash.text())
        self.click(self.window.add_account_button)
        self.window.phone.setText("+639171234567")
        self.click(self.window.code_button)
        attempted = self.window._session_path()
        self.controller.failed.emit("请求验证码超时，请检查网络后手动重试。")
        self.controller.set_busy(False)
        self.assertTrue(self.window._new_account_mode)
        self.window.phone.setText("+4915112345678")
        self.click(self.window.code_button)
        self.assertNotEqual(self.window._session_path(), attempted)
        self.assertNotEqual(self.window._session_path(), first)
        self.assertTrue(first.exists())
        self.assertTrue((self.window.api_id.text(), self.window.api_hash.text()) == api_before)
        self.assertEqual(len(self.window.account_store.list_accounts()), 1)

    def test_successful_login_with_account_registry_failure_remains_logged_in(self):
        self.credentials()
        self.touch_session()
        with patch.object(self.window.account_store, "remember", side_effect=OSError("storage")):
            self.controller.authenticated.emit({"id": 101, "name": "Account A"})
        self.assertIn("已连接", self.window.account_label.text())
        self.assertIn("已登录", self.window.auth_status.text())
        self.assertIn("账号列表", self.window.auth_status.text())
        self.assertFalse(self.window._new_account_mode)
        self.assertFalse(self.window._auth_challenge)

    def test_terminal_challenge_error_unlocks_resend_after_worker_becomes_idle(self):
        self.begin_challenge()
        self.window.code.setText("24680")
        self.controller.set_busy(True)
        self.controller.challenge_ended.emit()
        self.controller.failed.emit("验证码已过期；请点击获取验证码重新申请，不会自动重发。")
        self.assertFalse(self.window._auth_challenge)
        self.assertIsNone(self.window._challenge_identity)
        self.assertFalse(self.window.code.text())
        self.assertFalse(self.window.code_button.isEnabled())
        self.controller.set_busy(False)
        self.assertTrue(self.window.code_button.isEnabled())
        self.assertTrue(self.window.api_id.isEnabled())
        self.assertTrue(self.window.api_hash.isEnabled())
        self.assertTrue(self.window.phone.isEnabled())
        self.assertTrue(self.window.fields["telegram.session"].isEnabled())
        self.assertEqual(self.window.connect_button.text(), "恢复已有会话")
        self.assertIn("过期", self.window.feedback.text())
        self.click(self.window.code_button)
        self.assertEqual(self.controller.calls[-1][0], "code")

    def test_retryable_code_error_keeps_challenge_for_another_attempt(self):
        self.begin_challenge()
        self.controller.set_busy(True)
        self.controller.failed.emit("验证码无效，还可尝试 2 次。")
        self.controller.set_busy(False)
        self.assertTrue(self.window._auth_challenge)
        self.assertFalse(self.window.code_button.isEnabled())
        self.assertFalse(self.window.api_id.isEnabled())
        self.assertTrue(self.window.code.isEnabled())
        self.window.code.setText("24680")
        self.click(self.window.login_button)
        self.assertEqual(self.controller.calls[-1][0], "login")

    def test_preview_requires_source_and_credentials_but_no_target(self):
        self.credentials()
        self.window.fields["sync.source"].setText("@actual_test_source")
        self.window.fields["sync.target"].clear()
        self.click(self.window.task_preview_button)
        self.assertEqual(len(self.controller.calls), 1)
        self.assertEqual(self.controller.calls[0][0], "preview")
        self.assertEqual(self.controller.calls[0][1].source, "@actual_test_source")
        self.assertEqual(self.window.stack.currentIndex(), 1)

    def test_preview_without_real_source_does_not_dispatch(self):
        self.credentials()
        self.window.fields["sync.source"].clear()
        self.window.fields["sync.target"].clear()
        self.click(self.window.task_preview_button)
        self.assertFalse(self.controller.calls)
        self.assertIn("实际的源", self.window.feedback.text())

    def test_valid_run_check_and_preview_dispatch_selected_options(self):
        self.valid_task()
        self.window.once.setChecked(False)
        self.click(self.window.start_button)
        self.assertEqual(self.controller.calls[-1][0], "run")
        self.assertEqual(self.controller.calls[-1][2:], (False, False))
        self.click(self.window.check_button)
        self.assertEqual(self.controller.calls[-1][2:], (True, True))
        self.click(self.window.task_preview_button)
        self.assertEqual(self.window.stack.currentIndex(), 1)
        self.assertEqual(self.controller.calls[-1][0], "preview")
        self.assertEqual(self.controller.calls[-1][2], 50)

    def test_start_immediately_shows_task_progress_and_begins_a_fresh_log_section(self):
        self.valid_task()
        self.controller.log.emit("old login AuthRestartError must not be mistaken for this run")
        self.click(self.window.start_button)
        self.assertIn("检查登录状态", self.window.task_status.text())
        self.assertEqual(self.window.task_progress.maximum(), 0)
        logs = self.window.log_view.toPlainText().splitlines()
        self.assertIn("本次搬运任务", logs[0])
        self.assertNotIn("AuthRestartError", self.window.log_view.toPlainText())
        self.controller.set_busy(True)
        self.assertEqual(self.window.start_button.text(), "搬运中…")
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertTrue(self.window.stop_button.isEnabled())
        self.window.navigation.setCurrentRow(3)
        self.assertTrue(self.window.task_status.isVisible())

    def test_task_phases_show_download_percentages_without_flooding_log(self):
        self.valid_task()
        self.click(self.window.start_button)
        self.controller.task_phase.emit({"phase": "resolve_source", "message": "正在解析源频道…"})
        self.assertEqual(self.window.task_status.text(), "正在解析源频道…")
        self.controller.task_phase.emit({"phase": "download", "message": "正在下载消息 8 · 20%", "scope": "post", "source_id": 8})
        lines_before = len(self.window.log_view.toPlainText().splitlines())
        self.controller.task_phase.emit({"phase": "download", "message": "正在下载消息 8 · 75%", "scope": "post", "source_id": 8})
        self.assertIn("75%", self.window.task_status.text())
        self.assertEqual(len(self.window.log_view.toPlainText().splitlines()), lines_before)
        self.assertEqual(self.window.task_progress.maximum(), 0)

    def test_explicit_zero_result_reason_survives_generic_completion_signals(self):
        self.valid_task()
        self.click(self.window.start_button)
        reason = "源最新消息 ID 12 未超过已同步游标 12，本轮没有新增消息。"
        self.controller.task_phase.emit({"phase": "completed", "message": "本轮扫描完成，处理 0 条。", "reason": reason,
                                         "counts": {"processed": 0, "sent": 0, "filtered": 0, "files": 0}})
        self.controller.status.emit("本次任务完成，进度已保存。")
        self.controller.set_busy(False)
        self.controller.finished.emit("completed")
        self.controller.finished.emit("completed")
        self.assertEqual(self.window.task_status.text(), "本轮扫描完成，处理 0 条。")
        self.assertIn(reason, self.window.task_detail.text())
        self.assertIn(reason, self.window.feedback.text())
        self.assertIn(reason, self.window.log_view.toPlainText())
        self.assertEqual(self.window.task_progress.maximum(), 100)
        self.assertEqual(self.window.task_progress.value(), 100)
        self.assertFalse(self.window._task_active)

    def test_known_machine_reasons_and_phase_keys_are_displayed_in_chinese(self):
        self.valid_task()
        self.click(self.window.start_button)
        self.controller.task_phase.emit({"phase": "scan_posts", "message": "正在读取源历史消息。"})
        self.assertIn("[读取正文]", self.window.log_view.toPlainText())
        self.assertNotIn("[scan_posts]", self.window.log_view.toPlainText())
        for reason in ("source_empty", "no_new", "start_after_latest", "no_visible_messages", "settling", "filtered_all", "download_only", "processed"):
            with self.subTest(reason=reason):
                self.controller.task_phase.emit({"phase": "completed", "message": "本轮扫描结果已返回。", "reason": reason})
                for text in (self.window.task_detail.text(), self.window.feedback.text(), self.window.log_view.toPlainText()):
                    self.assertNotIn(reason, text)

    def test_real_task_failure_is_visible_in_status_card_and_current_log(self):
        self.valid_task()
        self.click(self.window.start_button)
        self.controller.set_busy(True)
        self.controller.task_phase.emit({"phase": "scan_posts", "message": "正在读取源频道历史消息…"})
        message = "源频道不可读：ChannelPrivateError；请检查当前账号是否已加入。"
        self.controller.failed.emit(message)
        self.controller.set_busy(False)
        self.controller.finished.emit("failed")
        self.assertEqual(self.window.task_status.text(), message)
        self.assertIn(message, self.window.log_view.toPlainText())
        self.assertIn(message, self.window.feedback.text())
        self.assertIn("#b63145", self.window.task_status.styleSheet())
        self.assertTrue(self.window.start_button.isEnabled())
        self.assertEqual(self.window.start_button.text(), "开始搬运")
        self.assertTrue(self.window.fields["sync.source"].isEnabled())
        self.assertFalse(self.window.stop_button.isEnabled())

    def test_explicit_error_reason_and_saved_counts_are_not_replaced_by_finished(self):
        self.valid_task()
        self.click(self.window.start_button)
        counts = {"processed": 2, "sent": 2, "filtered": 0, "files": 1}
        reason = "发送结果待人工核对 post:0:3；已停止避免重复发送。"
        self.controller.task_phase.emit({"phase": "failed", "message": "搬运未完成。", "reason": reason, "counts": counts})
        self.controller.failed.emit("SyncError：发送结果待人工核对。")
        self.controller.finished.emit("failed")
        self.assertIn(reason, self.window.task_detail.text())
        self.assertIn(reason, self.window.feedback.text())
        self.assertEqual(self.window.stat_labels["processed"].text(), "2")
        self.assertEqual(self.window.stat_labels["sent"].text(), "2")
        self.assertIn("SyncError", self.window.log_view.toPlainText())

    def test_stop_preserves_the_actual_quota_reason_instead_of_generic_completion(self):
        self.valid_task()
        self.click(self.window.start_button)
        self.controller.task_phase.emit({"phase": "wait", "message": "发送前检查滚动额度…", "wait_seconds": 15})
        reason = "滚动 1 小时发送额度已用尽，本次已停止，冷却仍保留。"
        self.controller.status.emit(reason)
        self.controller.set_busy(False)
        self.controller.finished.emit("stopped")
        self.assertEqual(self.window.task_status.text(), reason)
        self.assertIn(reason, self.window.feedback.text())
        self.assertIn(reason, self.window.log_view.toPlainText())
        self.assertEqual(self.window.task_progress.maximum(), 100)
        self.assertEqual(self.window.task_progress.value(), 0)

    def test_round_complete_remains_busy_while_waiting_for_more_messages(self):
        self.valid_task()
        self.window.once.setChecked(False)
        self.click(self.window.start_button)
        self.controller.set_busy(True)
        reason = "当前没有新增内容，60 秒后继续检查。"
        self.controller.task_phase.emit({"phase": "round_complete", "message": "本轮检查完成，正在等待下一轮。", "reason": reason,
                                         "counts": {"processed": 0, "sent": 0, "filtered": 0, "files": 0}})
        self.assertTrue(self.window._task_active)
        self.assertFalse(self.window._task_terminal_seen)
        self.assertIn(reason, self.window.task_detail.text())
        self.assertEqual(self.window.task_progress.maximum(), 0)
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertTrue(self.window.stop_button.isEnabled())

    def test_task_preflight_or_dispatch_rejection_ends_visible_progress_without_network(self):
        self.valid_task()
        self.window.fields["sync.target"].clear()
        self.click(self.window.start_button)
        self.assertFalse(self.controller.calls)
        self.assertIn("target", self.window.task_status.text())
        self.assertFalse(self.window._task_active)
        self.assertIsNone(self.window._task_operation)
        self.assertEqual(self.window.task_progress.maximum(), 100)
        self.window.fields["sync.target"].setText("@actual_test_target")
        with patch.object(self.controller, "run_task", return_value=False):
            self.click(self.window.start_button)
        self.assertIn("未开始", self.window.task_status.text())
        self.assertIn("未开始", self.window.log_view.toPlainText())
        self.assertFalse(self.window._task_active)

    def test_logged_in_layout_is_compact_and_task_fields_fit_a_600_pixel_window(self):
        self.credentials()
        self.authenticated_account(101, "Account A")
        self.window.resize(1100, 600)
        self.app.processEvents()
        area = self.window.stack.widget(0)
        area.verticalScrollBar().setValue(0)
        self.app.processEvents()
        self.assertEqual(self.window.height(), 600)
        self.assertFalse(self.window.login_details.isVisible())
        self.assertFalse(self.window.phone.isVisible())
        self.assertFalse(self.window.code.isVisible())
        self.assertFalse(self.window.password_row.isVisible())
        self.assertFalse(self.window.app_settings_panel.isVisible())
        for key in ("sync.source", "sync.target", "sync.mode", "sync.comment_mode", "storage.download_dir", "storage.auto_cleanup_forwarded"):
            field = self.window.fields[key]
            top = field.mapTo(area.viewport(), QPoint(0, 0))
            bottom = field.mapTo(area.viewport(), field.rect().bottomRight())
            self.assertTrue(area.viewport().rect().contains(top), key)
            self.assertTrue(area.viewport().rect().contains(bottom), key)
        for button in (self.window.start_button, self.window.stop_button):
            self.assertTrue(self.window.rect().contains(button.mapTo(self.window, button.rect().bottomRight())))
        self.assertEqual(self.window.navigation.verticalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def test_login_management_opens_only_when_requested_and_add_account_reopens_it(self):
        self.credentials()
        self.authenticated_account(101, "Account A")
        self.assertFalse(self.window.login_details.isVisible())
        self.click(self.window.login_management_button)
        self.assertTrue(self.window.login_details.isVisible())
        self.assertTrue(self.window.phone.isVisible())
        self.assertFalse(self.window.app_settings_panel.isVisible())
        self.click(self.window.login_management_button)
        self.assertFalse(self.window.login_details.isVisible())
        self.click(self.window.add_account_button)
        self.assertTrue(self.window.login_details.isVisible())
        self.assertTrue(self.window._new_account_mode)

    def wheel(self, widget, delta=-120):
        point = widget.rect().center()
        event = QWheelEvent(QPointF(point), QPointF(widget.mapToGlobal(point)), QPoint(0, 0), QPoint(0, delta),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        self.app.sendEvent(widget, event)
        self.app.processEvents()

    def test_wheel_cannot_change_any_numeric_time_or_combo_field_even_with_focus(self):
        for field in self.window.fields.values():
            if isinstance(field, (QSpinBox, QDoubleSpinBox)):
                before = field.value()
                field.setFocus()
                field.lineEdit().selectAll()
                self.wheel(field)
                self.wheel(field.lineEdit(), 120)
                self.assertEqual(field.value(), before)
            elif isinstance(field, QTimeEdit):
                before = field.time()
                field.setFocus()
                self.wheel(field)
                self.assertEqual(field.time(), before)
            elif isinstance(field, QComboBox):
                before = field.currentIndex()
                field.setFocus()
                self.wheel(field)
                self.assertEqual(field.currentIndex(), before)
        mode = self.window.fields["sync.mode"]
        mode.setCurrentIndex(0)
        QTest.keyClick(mode, Qt.Key.Key_Down)
        self.assertEqual(mode.currentIndex(), 1)

    def test_wheel_over_a_number_scrolls_page_without_editing_the_value(self):
        self.window.navigation.setCurrentRow(2)
        area = self.window.stack.currentWidget()
        area.verticalScrollBar().setValue(0)
        field = self.window.fields["limits.min_send_interval"]
        before = field.value()
        field.setFocus()
        self.wheel(field)
        self.assertEqual(field.value(), before)
        self.assertGreater(area.verticalScrollBar().value(), 0)

    def test_chat_picker_fills_selected_id_and_disambiguates_identical_titles(self):
        self.credentials()
        self.authenticated_account(101, "Account A")
        rows = [{"id": -1001111111, "title": "同名频道", "username": "first"},
                {"id": -1002222222, "title": "同名频道", "username": "second"}]
        with patch("tgmover.gui.ChatPickerDialog") as picker:
            picker.return_value.exec.return_value = QDialog.DialogCode.Accepted
            picker.return_value.selected_chat.return_value = rows[1]
            self.click(self.window.choose_source_button)
            self.assertEqual(self.controller.calls[-1][0], "list_chats")
            self.controller.chats_ready.emit(rows)
            QTest.qWait(1)
            self.app.processEvents()
            self.assertEqual(self.window.fields["sync.source"].text(), str(rows[1]["id"]))
            self.assertEqual(picker.call_args.kwargs["side"], "source")

    def test_chat_picker_ignores_results_from_a_different_session(self):
        self.credentials()
        self.authenticated_account(101, "Account A")
        old_target = self.window.fields["sync.target"].text()
        self.click(self.window.choose_target_button)
        self.window.fields["telegram.session"].setText(str(self.base / "other.session"))
        with patch("tgmover.gui.ChatPickerDialog") as picker:
            self.controller.chats_ready.emit([{"id": -1002222222, "title": "Target"}])
            QTest.qWait(1)
            picker.assert_not_called()
        self.assertEqual(self.window.fields["sync.target"].text(), old_target)
        self.assertIn("会话已变化", self.window.feedback.text())

    def test_chat_listing_signal_order_finishes_progress_without_resetting_send_statistics(self):
        self.credentials()
        self.authenticated_account(101, "Account A")
        self.controller.stats.emit({"processed": 5, "sent": 3, "filtered": 2, "files": 1})
        rows = [{"id": -1002222222, "title": "Chosen Target"}]
        with patch("tgmover.gui.ChatPickerDialog") as picker:
            picker.return_value.exec.return_value = QDialog.DialogCode.Accepted
            picker.return_value.selected_chat.return_value = rows[0]
            self.click(self.window.choose_target_button)
            self.controller.set_busy(True)
            self.controller.task_phase.emit({"phase": "loading_chats", "message": "正在读取已加入的频道和群组…",
                                             "counts": {"processed": 0, "sent": 0, "filtered": 0, "files": 0}})
            self.controller.chats_ready.emit(rows)
            self.controller.task_phase.emit({"phase": "chats", "message": "已读取 1 个频道/群组；未发送内容。",
                                             "counts": {"processed": 0, "sent": 0, "filtered": 0, "files": 0}})
            self.controller.set_busy(False)
            self.controller.finished.emit("chats")
            QTest.qWait(1)
            self.app.processEvents()
        self.assertFalse(self.window._task_active)
        self.assertEqual(self.window._task_phase_key, "chats")
        self.assertEqual(self.window.task_progress.maximum(), 100)
        self.assertEqual(self.window.task_progress.value(), 100)
        self.assertIn("已读取 1 个", self.window.task_status.text())
        self.assertNotIn("搬运已结束", self.window.task_status.text())
        self.assertEqual(self.window.stat_labels["sent"].text(), "3")
        self.assertEqual(self.window.fields["sync.target"].text(), str(rows[0]["id"]))
        self.assertIn("[列表就绪]", self.window.log_view.toPlainText())

    def test_chat_picker_is_guarded_by_login_busy_and_challenge_state(self):
        self.click(self.window.choose_source_button)
        self.assertFalse(self.controller.calls)
        self.assertIn("登录", self.window.feedback.text())
        self.credentials()
        self.authenticated_account(101, "Account A")
        self.controller.set_busy(True)
        self.assertFalse(self.window.choose_source_button.isEnabled())
        self.window._pick_chat("source")
        self.assertFalse(any(call[0] == "list_chats" for call in self.controller.calls))
        self.controller.set_busy(False)
        self.begin_challenge()
        self.assertFalse(self.window.choose_source_button.isEnabled())
        self.assertFalse(self.window.login_management_button.isEnabled())
        self.window._pick_chat("target")
        self.assertFalse(any(call[0] == "list_chats" for call in self.controller.calls))

    def test_legacy_controller_without_chat_listing_has_a_clear_fallback(self):
        self.credentials()
        self.authenticated_account(101, "Account A")
        with patch.object(self.controller, "list_chats", None):
            self.click(self.window.choose_target_button)
        self.assertIn("暂不支持聊天列表", self.window.feedback.text())

    def request_forum_topics(self, side="source"):
        self.credentials()
        self.authenticated_account(101, "Account A")
        chat = {"id": -1001234567890, "title": "离线话题群", "forum": True}
        with patch("tgmover.gui.ChatPickerDialog") as dialog_type:
            dialog_type.return_value.exec.return_value = QDialog.DialogCode.Accepted
            dialog_type.return_value.selected_chat.return_value = chat
            self.window._pick_chat(side)
            self.controller.chats_ready.emit([chat])
            self.app.processEvents()
        self.assertEqual(self.controller.calls[-1][0], "list_topics")
        self.assertEqual(self.controller.calls[-1][2], chat["id"])
        return chat

    def test_forum_picker_commits_group_and_topic_together_and_saves_ids_only(self):
        previous = self.window.fields["sync.source"].text()
        chat = self.request_forum_topics()
        self.assertEqual(self.window.fields["sync.source"].text(), previous)
        topic = {"id": 42, "title": "摄影交流", "closed": False}
        with patch("tgmover.gui.TopicPickerDialog") as dialog_type:
            dialog_type.return_value.exec.return_value = QDialog.DialogCode.Accepted
            dialog_type.return_value.selected_topic.return_value = topic
            self.controller.topics_ready.emit({"chat_id": chat["id"], "topics": [topic]})
            self.app.processEvents()
        self.assertEqual(self.window.fields["sync.source"].text(), str(chat["id"]))
        self.assertEqual(self.window.collect_draft()["sync"]["source_topic_id"], 42)
        self.assertTrue(self.window.source_topic_label.text().startswith("话题："))
        self.assertIn("摄影交流", self.window.source_topic_label.toolTip())
        self.window._save()
        saved = tomllib.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["sync"]["source_topic_id"], 42)
        self.assertNotIn("摄影交流", self.path.read_text(encoding="utf-8"))
        self.window._load()
        self.assertEqual(self.window.collect_draft()["sync"]["source_topic_id"], 42)
        self.assertIn("42", self.window.source_topic_label.text())

    def test_forum_picker_cancel_preserves_original_chat_and_topic(self):
        self.window._commit_chat("source", -1000000000001, 7, "原话题")
        chat = self.request_forum_topics()
        with patch("tgmover.gui.TopicPickerDialog") as dialog_type:
            dialog_type.return_value.exec.return_value = QDialog.DialogCode.Rejected
            dialog_type.return_value.return_to_chats = False
            self.controller.topics_ready.emit({"chat_id": chat["id"], "topics": [{"id": 42, "title": "摄影"}]})
            self.app.processEvents()
        self.assertEqual(self.window.fields["sync.source"].text(), "-1000000000001")
        self.assertEqual(self.window.collect_draft()["sync"]["source_topic_id"], 7)

    def test_topic_results_for_wrong_group_or_changed_parent_are_ignored(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                chat = self.request_forum_topics()
                if changed:
                    self.window.fields["sync.source"].setText("@different_parent")
                with patch("tgmover.gui.TopicPickerDialog") as dialog_type:
                    self.controller.topics_ready.emit({"chat_id": chat["id"] if changed else -1009999999999, "topics": []})
                    self.app.processEvents()
                    dialog_type.assert_not_called()
                self.assertIsNone(self.window._topic_selection_request)
                self.assertEqual(self.window.collect_draft()["sync"]["source_topic_id"], 0)

    def test_topic_results_and_final_confirmation_check_the_account_session(self):
        chat = self.request_forum_topics()
        self.window.fields["telegram.session"].setText("data/different_session")
        with patch("tgmover.gui.TopicPickerDialog") as dialog_type:
            self.controller.topics_ready.emit({"chat_id": chat["id"], "topics": []})
            self.app.processEvents()
            dialog_type.assert_not_called()
        chat = self.request_forum_topics()
        with patch("tgmover.gui.TopicPickerDialog") as dialog_type:
            def changed_while_open():
                self.window.fields["telegram.session"].setText("data/third_session")
                return QDialog.DialogCode.Accepted
            dialog_type.return_value.exec.side_effect = changed_while_open
            dialog_type.return_value.selected_topic.return_value = {"id": 1, "title": "综合"}
            self.controller.topics_ready.emit({"chat_id": chat["id"], "topics": [{"id": 1, "title": "综合"}]})
            self.app.processEvents()
        self.assertEqual(self.window.collect_draft()["sync"]["source_topic_id"], 0)

    def test_typing_a_different_parent_clears_only_its_topic_selection(self):
        self.window._commit_chat("source", -1001, 123, "源话题")
        self.window._commit_chat("target", -1002, 456, "目标话题")
        self.window.fields["sync.source"].setText("@new_source")
        draft = self.window.collect_draft()["sync"]
        self.assertEqual((draft["source_topic_id"], draft["target_topic_id"]), (0, 456))
        self.assertEqual(self.window.source_topic_label.text(), "全部话题")
        self.assertIn("已清除", self.window.feedback.text())

    def test_whole_source_group_or_default_target_can_be_chosen_without_topics(self):
        for side in ("source", "target"):
            with self.subTest(side=side):
                chat = self.request_forum_topics(side)
                with patch("tgmover.gui.TopicPickerDialog") as dialog_type:
                    dialog_type.return_value.exec.return_value = QDialog.DialogCode.Accepted
                    dialog_type.return_value.selected_topic.return_value = {"id": 0}
                    self.controller.topics_ready.emit({"chat_id": chat["id"], "topics": []})
                    self.app.processEvents()
                self.assertEqual(self.window.fields[f"sync.{side}"].text(), str(chat["id"]))
                self.assertEqual(self.window.collect_draft()["sync"][f"{side}_topic_id"], 0)

    def test_topic_listing_finishes_progress_and_preserves_previous_send_statistics(self):
        self.controller.stats.emit({"sent": 3})
        chat = self.request_forum_topics("target")
        topic = {"id": 1, "title": "综合"}
        with patch("tgmover.gui.TopicPickerDialog") as dialog_type:
            dialog_type.return_value.exec.return_value = QDialog.DialogCode.Accepted
            dialog_type.return_value.selected_topic.return_value = topic
            self.controller.set_busy(True)
            self.controller.task_phase.emit({"phase": "loading_topics", "message": "正在读取群组话题…", "counts": {"sent": 0}})
            self.controller.topics_ready.emit({"chat_id": chat["id"], "topics": [topic]})
            self.controller.task_phase.emit({"phase": "topics", "message": "已读取群组话题；未发送内容。", "counts": {"sent": 0}})
            self.controller.set_busy(False)
            self.controller.finished.emit("topics")
            self.app.processEvents()
        self.assertEqual(self.window.collect_draft()["sync"]["target_topic_id"], 1)
        self.assertFalse(self.window._task_active)
        self.assertEqual(self.window._task_phase_key, "topics")
        self.assertEqual(self.window.task_progress.value(), 100)
        self.assertEqual(self.window.stat_labels["sent"].text(), "3")

    def test_topic_search_pins_whole_group_and_shows_general_closed_and_hidden_status(self):
        topics = [{"id": 1, "title": "综合"}, {"id": 42, "title": "摄影交流", "closed": True}, {"id": 99, "title": "隐藏", "hidden": True}]
        dialog = TopicPickerDialog(topics, side="source", chat_title="测试群", parent=self.window)
        self.assertEqual(dialog.selected_topic()["id"], 0)
        self.assertIn("整个群", dialog.results.item(0).text())
        self.assertIn("已关闭", dialog.results.item(2).text())
        self.assertIn("已隐藏", dialog.results.item(3).text())
        dialog.search.setText("摄影")
        self.assertEqual(dialog.results.count(), 2)
        self.assertEqual(dialog.results.item(1).data(Qt.ItemDataRole.UserRole)["id"], 42)
        dialog._back()
        self.assertTrue(dialog.return_to_chats)
        dialog.deleteLater()

    def test_topic_read_failure_or_legacy_controller_preserves_old_choice(self):
        self.window._commit_chat("source", -1001, 7, "原话题")
        chat = self.request_forum_topics()
        self.controller.failed.emit("群组话题读取失败，请检查访问权限。")
        self.assertIsNone(self.window._topic_selection_request)
        self.assertEqual(self.window.collect_draft()["sync"]["source_topic_id"], 7)
        with patch.object(self.controller, "list_topics", None):
            self.window._request_topics({"side": "source", "session": os.path.normcase(str(self.window._session_path()))}, chat, [chat])
        self.assertIn("暂不支持话题列表", self.window.feedback.text())

    def test_download_only_disables_target_picker_and_retains_configured_topic(self):
        self.window._commit_chat("target", -1002, 456, "目标话题")
        mode = self.window.fields["sync.mode"]
        mode.setCurrentIndex(mode.findData("download_only"))
        self.assertFalse(self.window.choose_target_button.isEnabled())
        self.assertFalse(self.window.target_topic_label.isEnabled())
        self.assertEqual(self.window.collect_draft()["sync"]["target_topic_id"], 456)

    def test_download_directory_picker_can_choose_another_drive_and_cancel_preserves_value(self):
        original_cwd = Path.cwd()
        self.window.fields["storage.download_dir"].setText("relative-media")
        with patch("tgmover.gui.QFileDialog.getExistingDirectory", return_value="E:/Telegram 备份") as choose:
            self.click(self.window.choose_download_button)
        self.assertEqual(choose.call_args.args[2], str((self.base / "relative-media").resolve()))
        self.assertEqual(self.window.collect_draft()["storage"]["download_dir"], str(Path("E:/Telegram 备份").resolve()))
        before = self.window.fields["storage.download_dir"].text()
        with patch("tgmover.gui.QFileDialog.getExistingDirectory", return_value=""):
            self.click(self.window.choose_download_button)
        self.assertEqual(self.window.fields["storage.download_dir"].text(), before)
        self.assertEqual(Path.cwd(), original_cwd)

    def test_advanced_download_connections_is_bounded_saved_and_protected_from_wheel_changes(self):
        field = self.window.fields["storage.download_connections"]
        self.assertEqual((field.minimum(), field.maximum(), field.value()), (1, 8, 4))
        self.assertFalse(field.isVisible())
        self.click(self.window.paths_toggle)
        self.assertTrue(field.isVisible())
        self.assertIn("仅用于媒体下载", field.toolTip())
        field.setValue(6)
        field.setFocus()
        field.lineEdit().selectAll()
        self.wheel(field)
        self.wheel(field.lineEdit(), 120)
        self.assertEqual(field.value(), 6)
        self.window._save()
        self.assertEqual(self.saved()["storage"]["download_connections"], 6)
        field.setValue(2)
        self.window._load()
        self.assertEqual(field.value(), 6)

    def test_open_download_directory_resolves_relative_to_selected_config_without_creating_paths(self):
        media = self.base / "media"
        media.mkdir()
        self.window.fields["storage.download_dir"].setText("media")
        with patch("tgmover.gui.QDesktopServices.openUrl", return_value=True) as open_url:
            self.click(self.window.open_download_button)
        self.assertEqual(open_url.call_args.args[0].toLocalFile(), str(media.resolve()).replace("\\", "/"))
        self.window.fields["storage.download_dir"].setText("not-created")
        with patch("tgmover.gui.QDesktopServices.openUrl") as open_url:
            self.click(self.window.open_download_button)
        open_url.assert_not_called()
        self.assertFalse((self.base / "not-created").exists())
        self.assertIn("尚未创建", self.window.feedback.text())

    def test_auto_cleanup_only_enabled_for_download_then_forward_and_retains_checked_value(self):
        self.assertFalse(self.window.auto_cleanup.isChecked())
        self.assertFalse(self.window.auto_cleanup.isEnabled())
        mode = self.window.fields["sync.mode"]
        mode.setCurrentIndex(mode.findData("download_then_forward"))
        self.assertTrue(self.window.auto_cleanup.isEnabled())
        self.click(self.window.auto_cleanup)
        for name in ("forward", "reupload", "download_only"):
            mode.setCurrentIndex(mode.findData(name))
            self.assertFalse(self.window.auto_cleanup.isEnabled())
            self.assertTrue(self.window.auto_cleanup.isChecked())
            self.assertTrue(self.window.collect_draft()["storage"]["auto_cleanup_forwarded"])
        mode.setCurrentIndex(mode.findData("download_then_forward"))
        self.assertTrue(self.window.auto_cleanup.isEnabled())

    def test_invalid_hand_edited_cleanup_value_never_enables_file_deletion(self):
        self.credentials()
        self.window._save()
        original = self.path.read_text(encoding="utf-8")
        for value in ('"false"', '"true"', "1"):
            with self.subTest(value=value):
                self.path.write_text(original.replace("auto_cleanup_forwarded = false", f"auto_cleanup_forwarded = {value}"), encoding="utf-8")
                self.window._load()
                self.assertFalse(self.window.auto_cleanup.isChecked())
                self.assertFalse(self.window.collect_draft()["storage"]["auto_cleanup_forwarded"])
                self.assertIn("当前保持关闭", self.window.feedback.text())

    def test_cleanup_runs_without_source_or_target_and_displays_real_result_without_resetting_stats(self):
        self.credentials()
        self.window.fields["sync.source"].clear()
        self.window.fields["sync.target"].clear()
        self.controller.stats.emit({"processed": 4, "sent": 3})
        self.click(self.window.cleanup_button)
        self.assertEqual(self.controller.calls[-1][0], "cleanup_downloads")
        self.assertEqual(self.controller.calls[-1][1].download_dir, (self.base / "downloads").resolve())
        self.assertEqual(self.window._task_operation, "cleanup")
        self.assertIn("清理", self.window.task_status.text())
        self.controller.set_busy(True)
        for widget in (self.window.choose_download_button, self.window.cleanup_button, self.window.fields["storage.download_dir"], self.window.switch_account_button):
            self.assertFalse(widget.isEnabled())
        self.assertTrue(self.window.stop_button.isEnabled())
        message = "已清理 2 个已确认转发的文件，释放 12 MB；1 个待确认文件已保留。"
        self.controller.cleanup_ready.emit({"message": message, "deleted_files": 2, "freed_bytes": 12582912})
        self.controller.set_busy(False)
        self.controller.finished.emit("cleanup")
        self.assertEqual(self.window.task_status.text(), message)
        self.assertIn(message, self.window.log_view.toPlainText())
        self.assertEqual(self.window.stat_labels["sent"].text(), "3")
        self.assertFalse(self.window._task_active)
        self.assertTrue(self.window.cleanup_button.isEnabled())

    def test_busy_challenge_or_rejected_cleanup_does_not_dispatch_an_extra_job(self):
        self.credentials()
        self.controller.set_busy(True)
        self.window._cleanup_downloads()
        self.assertFalse(self.controller.calls)
        self.controller.set_busy(False)
        self.begin_challenge()
        before = len(self.controller.calls)
        self.assertFalse(self.window.cleanup_button.isEnabled())
        self.window._cleanup_downloads()
        self.assertEqual(len(self.controller.calls), before)
        self.window._challenge_ended()
        with patch.object(self.controller, "cleanup_downloads", return_value=False):
            self.window._cleanup_downloads()
        self.assertIn("清理未开始", self.window.task_status.text())
        self.assertFalse(self.window._task_active)

    def test_cleanup_failure_remains_visible_and_legacy_controller_reports_unavailable(self):
        self.credentials()
        self.click(self.window.cleanup_button)
        message = "文件被占用，部分媒体未清理；原记录保留。"
        self.controller.cleanup_ready.emit({"success": False, "message": message})
        self.controller.finished.emit("cleanup")
        self.assertEqual(self.window._task_phase_key, "failed")
        self.assertEqual(self.window.task_status.text(), message)
        with patch.object(self.controller, "cleanup_downloads", None):
            self.click(self.window.cleanup_button)
        self.assertIn("暂不支持清理", self.window.feedback.text())

    def pending_rows(self):
        self.credentials()
        record = {"route": "a" * 64, "key": "post:0:7,8", "scope": "post", "source_ids": [7, 8],
                  "created_at": "2026-10-21 12:00:00", "source": -100111, "target": -100222,
                  "source_topic_id": 42, "target_topic_id": 43, "mode": "download_then_forward"}
        self.window.navigation.setCurrentRow(3)
        self.click(self.window.pending_button)
        self.assertEqual(self.controller.calls[-1][0], "list_pending")
        self.controller.pending_ready.emit([record])
        self.app.processEvents()
        return record, self.window._pending_dialog

    def test_pending_dialog_shows_whole_album_and_requires_selection_and_acknowledgement(self):
        self.valid_task()
        before = {key: self.window.fields[key].text() for key in ("sync.source", "sync.target")}
        mode = self.window.fields["sync.mode"].currentData()
        record, dialog = self.pending_rows()
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog.table.item(0, 3).text(), "7, 8")
        self.assertIn("42", dialog.table.item(0, 0).text())
        self.assertIn("43", dialog.table.item(0, 1).text())
        self.assertFalse(dialog.resolve_button.isEnabled())
        self.window._resolve_pending()
        self.assertFalse(any(call[0] == "resolve_pending" for call in self.controller.calls))
        dialog.table.selectRow(0)
        self.assertFalse(dialog.resolve_button.isEnabled())
        self.click(dialog.acknowledge)
        self.assertTrue(dialog.resolve_button.isEnabled())
        self.click(dialog.resolve_button)
        resolves = [call for call in self.controller.calls if call[0] == "resolve_pending"]
        self.assertEqual(len(resolves), 1)
        self.assertEqual(resolves[0][2:], (record["route"], record["key"], "post"))
        self.assertFalse(dialog.acknowledge.isChecked())
        self.assertFalse(dialog.resolve_button.isEnabled())
        self.window._resolve_pending()
        self.assertEqual(len([call for call in self.controller.calls if call[0] == "resolve_pending"]), 1)
        self.assertFalse(any(call[0] in {"run", "preview", "code", "login"} for call in self.controller.calls))
        self.assertEqual({key: self.window.fields[key].text() for key in before}, before)
        self.assertEqual(self.window.fields["sync.mode"].currentData(), mode)

    def test_resolving_pending_refreshes_only_after_worker_is_idle_and_preserves_send_counts(self):
        self.controller.stats.emit({"processed": 5, "sent": 4})
        _, dialog = self.pending_rows()
        dialog.table.selectRow(0)
        self.click(dialog.acknowledge)
        self.click(dialog.resolve_button)
        self.controller.set_busy(True)
        self.controller.finished.emit("pending_resolved")
        self.app.processEvents()
        self.assertEqual(len([call for call in self.controller.calls if call[0] == "list_pending"]), 1)
        self.controller.set_busy(False)
        self.app.processEvents()
        self.assertEqual(len([call for call in self.controller.calls if call[0] == "list_pending"]), 2)
        self.controller.pending_ready.emit([])
        self.app.processEvents()
        self.assertEqual(dialog.table.rowCount(), 0)
        self.assertFalse(dialog.resolve_button.isEnabled())
        self.assertEqual(self.window.stat_labels["sent"].text(), "4")

    def test_busy_or_changed_database_cannot_resolve_the_selected_old_pending_record(self):
        _, dialog = self.pending_rows()
        dialog.table.selectRow(0)
        self.click(dialog.acknowledge)
        self.controller.set_busy(True)
        self.assertFalse(dialog.resolve_button.isEnabled())
        self.window._resolve_pending()
        self.assertFalse(any(call[0] == "resolve_pending" for call in self.controller.calls))
        self.controller.set_busy(False)
        self.window.fields["storage.database"].setText("data/other.sqlite3")
        self.window._resolve_pending()
        self.assertFalse(any(call[0] == "resolve_pending" for call in self.controller.calls))
        self.assertIn("数据库位置已变化", dialog.message.text())
        self.assertFalse(dialog.acknowledge.isChecked())

    def test_pending_selection_changes_and_worker_failure_require_a_new_acknowledgement(self):
        record, dialog = self.pending_rows()
        dialog.set_rows([record, {**record, "key": "post:0:9,10", "source_ids": [9, 10]}])
        dialog.table.selectRow(0)
        self.click(dialog.acknowledge)
        self.assertTrue(dialog.resolve_button.isEnabled())
        dialog.table.selectRow(1)
        self.assertFalse(dialog.acknowledge.isChecked())
        self.click(dialog.acknowledge)
        self.click(dialog.resolve_button)
        self.controller.failed.emit("记录已变化，请重新核对；本地阻塞保留。")
        self.assertFalse(dialog.acknowledge.isChecked())
        self.assertFalse(dialog.resolve_button.isEnabled())
        self.assertIn("本地阻塞保留", dialog.message.text())

    def test_uncertain_send_is_a_visible_terminal_state_not_replaced_by_generic_failure(self):
        self.valid_task()
        self.click(self.window.start_button)
        self.controller.set_busy(True)
        self.controller.stats.emit({"processed": 2, "sent": 2, "files": 2})
        message = "转发等待90秒仍未返回，结果待核对；请在运行记录中核对整批后解除阻塞。"
        self.controller.task_phase.emit({"phase": "uncertain", "message": message})
        self.controller.failed.emit("发送结果待核对，pending已保留。")
        self.controller.task_phase.emit({"phase": "failed", "message": "SyncError：发送未完成。"})
        self.controller.status.emit("后台发送已停止。")
        self.controller.set_busy(False)
        self.controller.finished.emit("failed")
        self.assertEqual(self.window._task_phase_key, "uncertain")
        self.assertEqual(self.window.task_status.text(), message)
        self.assertIn(message, self.window.feedback.text())
        self.assertIn("#b63145", self.window.task_status.styleSheet())
        self.assertEqual(self.window.stat_labels["sent"].text(), "2")
        self.assertFalse(self.window._task_active)
        self.assertFalse(self.window.task_progress.isVisible())

    def test_legacy_pending_controller_has_a_clear_fallback_without_a_job(self):
        with patch.object(self.controller, "list_pending", None):
            self.window._open_pending()
        self.assertIsNone(self.window._pending_dialog)
        self.assertIn("暂不支持查看待核对", self.window.feedback.text())

    def test_known_protected_content_failure_after_an_uncertain_run_stays_an_ordinary_failure(self):
        self.valid_task()
        self.click(self.window.start_button)
        self.controller.task_phase.emit({"phase": "uncertain", "message": "上次发送结果未知，请核对。"})
        self.controller.finished.emit("uncertain")
        self.click(self.window.start_button)
        message = "源聊天已启用内容保护，Telegram 禁止直接转发；ChatForwardsRestrictedError。"
        self.controller.task_phase.emit({"phase": "failed", "message": message})
        self.controller.failed.emit(message)
        self.controller.finished.emit("failed")
        self.assertEqual(self.window._task_phase_key, "failed")
        self.assertEqual(self.window.task_status.text(), message)
        self.assertIsNone(self.window._pending_dialog)
        self.assertFalse(any(call[0] == "list_pending" for call in self.controller.calls))

    def test_busy_reclick_does_not_clear_running_task_progress_or_logs(self):
        self.valid_task()
        self.click(self.window.start_button)
        self.controller.set_busy(True)
        self.controller.task_phase.emit({"phase": "upload", "message": "正在上传当前相册 · 40%", "source_id": 8})
        before_calls = len(self.controller.calls)
        before_log = self.window.log_view.toPlainText()
        self.window._action("run")
        self.assertEqual(len(self.controller.calls), before_calls)
        self.assertEqual(self.window.task_status.text(), "正在上传当前相册 · 40%")
        self.assertIn(before_log, self.window.log_view.toPlainText())
        self.assertTrue(self.window._task_active)

    def test_legacy_completion_without_reason_is_explicit_about_missing_detail(self):
        self.valid_task()
        self.click(self.window.start_button)
        self.controller.finished.emit("completed")
        self.assertIn("处理 0 条", self.window.task_detail.text())
        self.assertIn("未提供具体扫描原因", self.window.task_detail.text())

    def test_busy_disables_editing_and_network_actions_while_stop_still_works(self):
        self.controller.set_busy(True)
        for field in self.window.fields.values():
            self.assertFalse(field.isEnabled())
        for name in ("connect_button", "code_button", "login_button", "start_button", "save_button", "config_button", "preview_button"):
            self.assertFalse(getattr(self.window, name).isEnabled())
        self.assertFalse(self.window.phone.isEnabled())
        self.assertFalse(self.window.code.isEnabled())
        self.assertTrue(self.window.stop_button.isEnabled())
        self.click(self.window.stop_button)
        self.assertEqual(self.controller.stop_count, 1)
        self.assertIn("安全停止", self.window.feedback.text())
        self.controller.set_busy(False)
        self.assertTrue(self.window.fields["sync.source"].isEnabled())
        self.assertTrue(self.window.start_button.isEnabled())
        self.assertFalse(self.window.stop_button.isEnabled())

    def test_busy_release_preserves_download_mode_field_restrictions(self):
        mode = self.window.fields["sync.mode"]
        mode.setCurrentIndex(mode.findData("download_only"))
        self.controller.set_busy(True)
        self.controller.set_busy(False)
        self.assertFalse(self.window.fields["sync.target"].isEnabled())
        self.assertFalse(self.window.fields["sync.comment_mode"].isEnabled())
        self.assertTrue(mode.isEnabled())

    def test_worker_signals_update_statistics_authentication_logs_and_feedback(self):
        expected = {"processed": 13, "sent": 8, "filtered": 5, "files": 4}
        self.controller.stats.emit(expected)
        for key, value in expected.items():
            self.assertEqual(self.window.stat_labels[key].text(), str(value))
        self.controller.authenticated.emit({"id": 1, "name": "Offline Test User"})
        self.assertIn("Offline Test User", self.window.account_label.text())
        self.controller.log.emit("offline log entry")
        self.assertIn("offline log entry", self.window.log_view.toPlainText())
        self.controller.status.emit("offline status")
        self.assertEqual(self.window.feedback.text(), "offline status")
        self.controller.failed.emit("offline friendly error")
        self.assertEqual(self.window.feedback.text(), "offline friendly error")
        self.window.navigation.setCurrentRow(3)
        self.click(self.window.clear_log_button)
        self.assertFalse(self.window.log_view.toPlainText())

    def test_close_waits_for_controller_closed_signal_before_window_disappears(self):
        self.window.close()
        self.app.processEvents()
        self.assertEqual(self.controller.shutdown_count, 1)
        self.assertTrue(self.window._close_requested)
        self.assertFalse(self.window._closed)
        self.assertTrue(self.window.isVisible())
        self.controller.finished.emit("closed")
        QTest.qWait(1)
        self.app.processEvents()
        self.assertTrue(self.window._closed)
        self.assertFalse(self.window.isVisible())
        self.assertEqual(self.controller.shutdown_count, 1)


if __name__ == "__main__":
    unittest.main()
