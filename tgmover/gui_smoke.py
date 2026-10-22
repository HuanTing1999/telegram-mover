"""Offline interaction/visual smoke check, also available in the frozen app."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch


def run_self_test(output_directory):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QObject, QPoint, QPointF, Qt, Signal
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialog, QScrollArea
    from .gui import MainWindow, TopicPickerDialog
    from .acceleration import backend_info, benchmark_current_backend

    media_backend = backend_info()
    assert media_backend["backend"] == "cryptg", "Telethon 未激活 cryptg 本地加速。"
    crypto_check = benchmark_current_backend(size_bytes=131072, repeats=1)
    assert crypto_check["roundtrip_correct"] and crypto_check["backend"]["backend"] == "cryptg"

    def session_path(value):
        path = Path(value)
        return path if str(path).endswith(".session") else Path(str(path) + ".session")

    class OfflineController(QObject):
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
            self.actions = []
            self.session = None
            self.session_users = {}
            self.needs_password = False
            self.pending_records = [{"route": "a" * 64, "key": "post:0:102,103", "scope": "post",
                                     "source_ids": [102, 103], "created_at": "离线演示时间",
                                     "source": -1000000000001, "target": -1000000000002,
                                     "source_topic_id": 42, "target_topic_id": 1, "mode": "download_then_forward"}]

        def authenticate(self, value):
            path = session_path(value)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            identity = self.session_users.setdefault(str(path), {
                "id": 12345 + len(self.session_users),
                "name": f"离线演示账号 {len(self.session_users) + 1} · 未连接 Telegram",
            })
            self.authenticated.emit(identity)

        def connect_account(self, config):
            self.actions.append("connect")
            self.authenticate(config.session)
            return True

        def request_code(self, config, phone):
            self.actions.append("request_code")
            self.session = config.session
            self.needs_password = phone.startswith("+44")
            self.code_sent.emit("离线测试：验证码步骤已就绪，不会请求真实验证码。")
            self.code_delivery.emit({"can_resend": False, "resend_available": True, "resend_after": 30,
                                     "delivery": "Telegram 客户端（离线演示）", "next_delivery": "由 Telegram 指定（离线演示）",
                                     "message": "此处只演示渠道与等待，不请求真实验证码。"})
            return True

        def resend_code(self, config, phone):
            self.actions.append("resend_code")
            self.session = config.session
            self.code_sent.emit("离线演示：已显式重新申请验证码，不会连接 Telegram。")
            self.code_delivery.emit({"can_resend": True, "resend_available": True, "resend_after": 0,
                                     "delivery": "Telegram 客户端（离线演示）", "next_delivery": "由 Telegram 指定（离线演示）",
                                     "message": "这是离线交互，不会发送短信。"})
            self.finished.emit("code_sent")
            return True

        def submit_code(self, code):
            self.actions.append("submit_code")
            if code == "00000":
                self.failed.emit("离线演示：验证码不正确；输入已清除，可重新输入或等待后重新获取。")
                self.finished.emit("failed")
                return True
            if self.needs_password:
                self.password_required.emit("离线演示：此测试账号需要两步验证密码，输入仅临时使用。")
                self.finished.emit("password_required")
                return True
            self.authenticate(self.session)
            return True

        def submit_password(self, password):
            self.actions.append("submit_password")
            self.needs_password = False
            self.authenticate(self.session)
            return True

        def cancel_authentication(self):
            self.actions.append("cancel_authentication")
            self.needs_password = False
            self.challenge_ended.emit()
            self.finished.emit("auth_cancelled")
            return True

        def preview(self, config, limit=50):
            self.actions.append("preview")
            self.preview_ready.emit([
                {"source_id": 104, "chat_id": -1000000000001, "grouped_id": None, "text": "商务合作 · 推广请联系 https://ads.example.com", "media": "文字", "blocked": True, "reasons": ["关键词：商务合作"]},
                {"source_id": 103, "chat_id": -1000000000001, "grouped_id": 777, "text": "今天的旅行照片（2）", "media": "图片", "blocked": False, "reasons": []},
                {"source_id": 102, "chat_id": -1000000000001, "grouped_id": 777, "text": "今天的旅行照片（1）", "media": "图片", "blocked": False, "reasons": []},
                {"source_id": 101, "chat_id": -1000000000001, "grouped_id": None, "text": "本周更新与读者交流，欢迎查看完整内容。", "media": "文字", "blocked": False, "reasons": []},
            ])
            self.finished.emit("preview")
            return True

        def list_chats(self, config):
            self.actions.append("list_chats")
            self.chats_ready.emit([{"id": -1000000000001, "title": "离线话题演示群", "forum": True}])
            return True

        def list_topics(self, config, peer):
            self.actions.append("list_topics")
            self.topics_ready.emit({"chat_id": peer, "topics": [
                {"id": 1, "title": "综合", "closed": False},
                {"id": 42, "title": "摄影与旅行", "closed": False},
                {"id": 99, "title": "往期归档", "closed": True},
            ]})
            self.task_phase.emit({"phase": "topics", "message": "离线演示：话题列表已就绪，未读取真实群组。"})
            self.finished.emit("topics")
            return True

        def cleanup_downloads(self, config):
            self.actions.append("cleanup_downloads")
            self.task_phase.emit({"phase": "cleanup", "message": "离线演示：正在核对成功转发的文件记录。"})
            self.cleanup_ready.emit({"message": "离线清理演示已完成；没有删除真实文件。", "deleted_files": 0, "freed_bytes": 0})
            self.finished.emit("cleanup")
            return True

        def list_pending(self, config):
            self.actions.append("list_pending")
            self.pending_ready.emit(list(self.pending_records))
            self.finished.emit("pending")
            return True

        def resolve_pending(self, config, route, key, scope):
            self.actions.append("resolve_pending")
            self.pending_records = [row for row in self.pending_records if (row["route"], row["key"], row["scope"]) != (route, key, scope)]
            self.finished.emit("pending_resolved")
            return True

        def run_task(self, config, once=True, check_only=False):
            self.actions.append("check" if check_only else "run")
            self.task_phase.emit({"phase": "prepare", "message": "离线演示：正在检查任务配置与当前账号。"})
            counts = {"processed": 4, "sent": 2, "filtered": 2, "files": 2}
            self.stats.emit(counts)
            self.log.emit("离线界面测试：真实 Telegram 网络与发送均未启用。")
            self.task_phase.emit({"phase": "completed", "message": "离线演示任务已完成。", "counts": counts, "reason": "测试样例中 2 条已模拟发送、2 条广告已拦截；没有实际发送。"})
            self.finished.emit("checked" if check_only else "completed")
            return True

        def stop(self):
            self.actions.append("stop")

        def shutdown(self):
            self.finished.emit("closed")

    output = Path(output_directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    checks = []
    clean_environment = {key: value for key, value in os.environ.items() if key not in {"API_ID", "API_HASH", "TGMOVER_APP_CREDENTIALS"}}
    with tempfile.TemporaryDirectory(dir=output, prefix="gui-smoke-") as folder, \
            patch.dict(os.environ, clean_environment, clear=True), \
            patch("tgmover.credentials.application_directory", return_value=Path(folder)), \
            patch("tgmover.credentials.bundle_directory", return_value=None):
        config_path = Path(folder) / "config.toml"
        controller = OfflineController()
        window = MainWindow(config_path=config_path, controller=controller)
        window.show()
        app.processEvents()
        checks.append("window_opens_without_credentials")
        assert not window._application_ready
        assert window.app_settings_panel.isVisible()
        assert "尚未配置" in window.application_status.text()
        assert window.fields["storage.download_connections"].value() == 4
        assert window.grab().save(str(output / "gui-first-setup.png"))
        checks.append("missing_application_setup_is_shown_honestly")
        window.api_id.setText("12345")
        window.api_hash.setText("a" * 32)
        window.fields["sync.source"].setText("@example_source")
        window.fields["sync.target"].setText("@example_destination")
        window.phone.setText("+63 917-123-4567")
        QTest.mouseClick(window.save_button, Qt.MouseButton.LeftButton)
        assert config_path.is_file()
        assert window._application_ready and not window.app_settings_panel.isVisible()
        assert "本机配置" in window.application_status.text()
        content = config_path.read_text(encoding="utf-8")
        assert "+63 917-123-4567" not in content
        checks.append("configuration_saved_without_phone_or_code")
        QTest.mouseClick(window.code_button, Qt.MouseButton.LeftButton)
        assert window.phone.text() == "+639171234567"
        assert not window.api_hash.isEnabled() and not window.phone.isEnabled()
        assert window.resend_button.isVisible() and not window.resend_button.isEnabled()
        window.code.setText("00000")
        QTest.keyClick(window.code, Qt.Key.Key_Return)
        assert not window.code.text() and window._auth_challenge
        assert "不正确" in window.auth_status.text()
        window._refresh_resend()
        assert "不正确" in window.auth_status.text()
        app.processEvents()
        assert window.grab().save(str(output / "gui-code-resend.png"))
        requests = len(controller.actions)
        window._resend_deadline = 0
        window._refresh_resend()
        assert window.resend_button.isEnabled() and len(controller.actions) == requests
        QTest.mouseClick(window.resend_button, Qt.MouseButton.LeftButton)
        assert controller.actions[-1] == "resend_code"
        assert not window.phone.isEnabled() and not window.account_selector.isEnabled()
        assert not window._resend_request_pending
        checks.append("wrong_code_clear_explicit_resend_and_server_channel_countdown")
        window.code.setText("12345")
        QTest.keyClick(window.code, Qt.Key.Key_Return)
        assert "submit_code" in controller.actions
        assert window.code.text() == ""
        checks.append("window_phone_code_flow_and_challenge_lock")
        assert not window.login_details.isVisible() and not window.phone.isVisible()
        checks.append("authenticated_account_collapses_login_management")
        first_session = str(window._session_path())
        first_api = (window.api_id.text(), window.api_hash.text())
        QTest.mouseClick(window.add_account_button, Qt.MouseButton.LeftButton)
        assert window._new_account_mode
        assert str(window._session_path()) == first_session
        window.phone.setText("+44 7700 900123")
        QTest.mouseClick(window.code_button, Qt.MouseButton.LeftButton)
        second_session = str(window._session_path())
        assert first_session != second_session
        window.code.setText("12345")
        QTest.keyClick(window.code, Qt.Key.Key_Return)
        assert window._password_pending and window.password_row.isVisible()
        assert not window.code.isVisible() and not window.switch_account_button.isEnabled()
        assert "第2步" in window.password_row.title()
        assert "验证码已通过" in window.password_instruction.text()
        assert not window.phone.isVisible() and not window.code_button.isVisible()
        assert not window.resend_button.isVisible() and not window.code_delivery_label.isVisible()
        assert not window._resend_timer.isActive()
        assert window.connect_button.isVisible() and window.connect_button.parentWidget() is window.password_row
        assert window.connect_button.text() == "取消本次登录"
        app.processEvents()
        assert window.grab().save(str(output / "gui-two-factor.png"))
        checks.append("two_factor_step_two_panel_hides_code_and_countdown")
        fake_password = "  offline demonstration password  "
        window.password.setText(fake_password)
        QTest.keyClick(window.password, Qt.Key.Key_Return)
        assert "submit_password" in controller.actions
        assert window.password.text() == "" and not window._password_pending
        assert fake_password not in config_path.read_text(encoding="utf-8")
        assert fake_password not in window.account_store.path.read_text(encoding="utf-8")
        checks.append("two_factor_password_is_transient_and_submitted_by_enter")
        assert len(window.account_store.list_accounts()) == 2
        requests = controller.actions.count("request_code")
        window.account_selector.setCurrentIndex(window.account_selector.findData(first_session))
        QTest.mouseClick(window.switch_account_button, Qt.MouseButton.LeftButton)
        assert controller.actions[-1] == "connect"
        assert controller.actions.count("request_code") == requests
        assert (window.api_id.text(), window.api_hash.text()) == first_api
        checks.append("account_sessions_are_isolated_and_switch_without_repeating_application_setup")
        forum = {"id": -1000000000001, "title": "离线话题演示群", "forum": True}
        selected_topic = {"id": 42, "title": "摄影与旅行", "closed": False}
        with patch("tgmover.gui.ChatPickerDialog.exec", return_value=QDialog.DialogCode.Accepted), \
                patch("tgmover.gui.ChatPickerDialog.selected_chat", return_value=forum), \
                patch("tgmover.gui.TopicPickerDialog.exec", return_value=QDialog.DialogCode.Accepted), \
                patch("tgmover.gui.TopicPickerDialog.selected_topic", return_value=selected_topic):
            QTest.mouseClick(window.choose_source_button, Qt.MouseButton.LeftButton)
            for _ in range(3):
                app.processEvents()
        assert window.collect_draft()["sync"]["source_topic_id"] == 42
        window._save()
        assert "source_topic_id = 42" in config_path.read_text(encoding="utf-8")
        assert "摄影与旅行" not in config_path.read_text(encoding="utf-8")
        checks.append("forum_chat_then_topic_selection_persists_only_ids")
        topic_dialog = TopicPickerDialog([
            {"id": 1, "title": "综合"}, selected_topic, {"id": 99, "title": "往期归档", "closed": True},
        ], side="source", chat_title=forum["title"], parent=window)
        topic_dialog.show()
        topic_dialog.results.setCurrentRow(2)
        app.processEvents()
        assert topic_dialog.grab().save(str(output / "gui-topic-picker.png"))
        topic_dialog.close()
        app.processEvents()
        window.navigation.setCurrentRow(1)
        app.processEvents()
        QTest.mouseClick(window.sample_button, Qt.MouseButton.LeftButton)
        assert "将被屏蔽" in window.sample_result.text(), window.sample_result.text()
        window.sample_text.setPlainText("这是正常的更新说明，欢迎阅读。")
        QTest.mouseClick(window.sample_button, Qt.MouseButton.LeftButton)
        assert "未命中" in window.sample_result.text()
        checks.append("advertisement_and_normal_text_have_distinct_results")
        window._action("preview")
        assert window.preview_table.rowCount() == 4
        window.preview_table.selectRow(1)
        window._block_selected()
        assert "-1000000000001:102" in window.fields["filters.blocked_message_keys"].toPlainText()
        assert "-1000000000001:103" in window.fields["filters.blocked_message_keys"].toPlainText()
        checks.append("preview_and_whole_album_manual_block")
        window.navigation.setCurrentRow(0)
        window._action("run")
        app.processEvents()
        assert window.stat_labels["filtered"].text() == "2"
        assert "离线演示任务已完成" in window.task_status.text()
        assert "没有实际发送" in window.task_detail.text()
        assert "本次搬运任务" in window.log_view.toPlainText().splitlines()[0]
        checks.append("task_status_card_and_true_completion_reason_are_visible")
        checks.append("run_controls_and_progress_counters")
        media_folder = Path(folder) / "演示媒体目录"
        media_folder.mkdir()
        with patch("tgmover.gui.QFileDialog.getExistingDirectory", return_value=str(media_folder)):
            QTest.mouseClick(window.choose_download_button, Qt.MouseButton.LeftButton)
        assert window._download_directory() == media_folder.resolve()
        with patch("tgmover.gui.QDesktopServices.openUrl", return_value=True) as open_folder:
            QTest.mouseClick(window.open_download_button, Qt.MouseButton.LeftButton)
        assert Path(open_folder.call_args.args[0].toLocalFile()) == media_folder.resolve()
        mode = window.fields["sync.mode"]
        mode.setCurrentIndex(mode.findData("download_then_forward"))
        assert window.auto_cleanup.isEnabled() and not window.auto_cleanup.isChecked()
        QTest.mouseClick(window.auto_cleanup, Qt.MouseButton.LeftButton, pos=QPoint(8, window.auto_cleanup.height() // 2))
        assert window.auto_cleanup.isChecked()
        mode.setCurrentIndex(mode.findData("reupload"))
        assert not window.auto_cleanup.isEnabled() and window.auto_cleanup.isChecked()
        mode.setCurrentIndex(mode.findData("download_then_forward"))
        window._save()
        assert "auto_cleanup_forwarded = true" in config_path.read_text(encoding="utf-8")
        checks.append("download_folder_selection_open_and_mode_scoped_auto_cleanup")
        QTest.mouseClick(window.cleanup_button, Qt.MouseButton.LeftButton)
        assert "cleanup_downloads" in controller.actions
        assert "没有删除真实文件" in window.task_status.text()
        assert window.stat_labels["sent"].text() == "2"
        assert not window._task_active
        checks.append("local_cleanup_dispatch_result_and_preserved_send_statistics")
        QTest.qWait(180)  # Let native checkbox transitions settle before screenshots.
        window._feedback("离线界面验收：配置、登录步骤、广告规则和预览已检查；没有连接或发送 Telegram 消息。")
        app.processEvents()
        for height in (600, 720):
            window.resize(1100, height)
            window.navigation.setCurrentRow(0)
            area = window.stack.currentWidget()
            area.verticalScrollBar().setValue(0)
            app.processEvents()
            assert window.height() == height
            assert not window.login_details.isVisible()
            for key in ("sync.source", "sync.target", "sync.mode", "sync.comment_mode", "storage.download_dir", "storage.auto_cleanup_forwarded"):
                field = window.fields[key]
                assert area.viewport().rect().contains(field.mapTo(area.viewport(), QPoint(0, 0))), key
                assert area.viewport().rect().contains(field.mapTo(area.viewport(), field.rect().bottomRight())), key
            assert window.rect().contains(window.start_button.mapTo(window, window.start_button.rect().bottomRight()))
            assert area.viewport().rect().contains(window.cleanup_button.mapTo(area.viewport(), window.cleanup_button.rect().bottomRight()))
            assert window.grab().save(str(output / f"gui-main-{height}.png"))
            if height == 600:
                assert window.grab().save(str(output / "gui-main.png"))
                assert window.grab().save(str(output / "gui-downloads.png"))
        checks.append("task_inputs_and_actions_fit_600_and_720_pixel_windows")
        window.resize(1180, 780)
        QTest.mouseClick(window.paths_toggle, Qt.MouseButton.LeftButton)
        connections = window.fields["storage.download_connections"]
        assert connections.isVisible() and connections.minimum() == 1 and connections.maximum() == 8
        connections.setValue(6)
        window._save()
        connections.setValue(2)
        window._load()
        assert connections.value() == 6
        connections.setValue(4)
        window._save()
        window.stack.currentWidget().ensureWidgetVisible(connections)
        app.processEvents()
        assert window.grab().save(str(output / "gui-download-connections.png"))
        QTest.mouseClick(window.paths_toggle, Qt.MouseButton.LeftButton)
        checks.append("bounded_download_connections_default_and_saved_setting")
        window.navigation.setCurrentRow(3)
        app.processEvents()
        QTest.mouseClick(window.pending_button, Qt.MouseButton.LeftButton)
        app.processEvents()
        pending = window._pending_dialog
        assert pending.isVisible() and pending.table.rowCount() == 1
        pending.table.selectRow(0)
        assert pending.table.item(0, 3).text() == "102, 103"
        assert not pending.resolve_button.isEnabled()
        QTest.mouseClick(pending.acknowledge, Qt.MouseButton.LeftButton, pos=QPoint(8, pending.acknowledge.height() // 2))
        assert pending.resolve_button.isEnabled()
        QTest.qWait(180)
        app.processEvents()
        assert pending.grab().save(str(output / "gui-pending.png"))
        actions_before_resolve = len(controller.actions)
        QTest.mouseClick(pending.resolve_button, Qt.MouseButton.LeftButton)
        for _ in range(3):
            app.processEvents()
        assert controller.actions[actions_before_resolve:] == ["resolve_pending", "list_pending"]
        assert pending.table.rowCount() == 0 and not pending.acknowledge.isChecked()
        pending.close()
        app.processEvents()
        checks.append("pending_album_manual_acknowledgement_local_resolution_and_refresh")
        window.navigation.setCurrentRow(1)
        window.sample_text.setPlainText("商务合作请联系 https://ads.example.com")
        window._sample()
        area = window.stack.currentWidget()
        if isinstance(area, QScrollArea):
            area.verticalScrollBar().setValue(0)
        app.processEvents()
        assert window.grab().save(str(output / "gui-filters.png"))
        if isinstance(area, QScrollArea):
            area.verticalScrollBar().setValue(area.verticalScrollBar().maximum())
        app.processEvents()
        assert window.grab().save(str(output / "gui-preview.png"))
        window.navigation.setCurrentRow(2)
        app.processEvents()
        field = window.fields["limits.min_send_interval"]
        previous = field.value()
        field.setFocus()
        point = field.rect().center()
        wheel = QWheelEvent(QPointF(point), QPointF(field.mapToGlobal(point)), QPoint(), QPoint(0, -120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        app.sendEvent(field, wheel)
        assert field.value() == previous
        window.stack.currentWidget().verticalScrollBar().setValue(0)
        app.processEvents()
        checks.append("wheel_over_numeric_input_does_not_change_settings")
        assert window.grab().save(str(output / "gui-limits.png"))
        window.close()
        for _ in range(5):
            app.processEvents()
        assert not window.isVisible()
        checks.append("graceful_window_close")
    result = {"passed": True, "offline": True, "network_used": False, "qt_platform": app.platformName(),
              "media_backend": media_backend["backend"], "media_crypto_roundtrip": True, "checks": checks}
    (output / "gui-smoke-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0
