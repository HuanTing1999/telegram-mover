"""Desktop workspace. All Telegram traffic stays in GuiController's worker."""
from __future__ import annotations

import copy
from datetime import datetime
import json
import math
import os
from pathlib import Path
import sqlite3
import time

from PySide6.QtCore import QEvent, Qt, QTime, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontDatabase, QIntValidator
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSpinBox, QStackedWidget, QTableWidget,
    QTableWidgetItem, QTimeEdit, QVBoxLayout, QWidget,
)
from telethon import types

from .accounts import AccountStore
from .config import ConfigError, load_config
from .credentials import credentials_status
from .filters import AdFilter, FilterSettings, validate_filter_settings
from .gui_config import (default_config_path, effective_credentials_hint,
                         read_draft, write_draft)
from .gui_worker import GuiController
from .login_input import normalize_code, normalize_phone


STYLE = """
QMainWindow, QWidget#workspace { background: #f3f6fb; color: #20304b; }
QWidget { font-family: 'Microsoft YaHei', 'Microsoft YaHei UI', 'Segoe UI'; font-size: 13px; }
QWidget#page { background: #f3f6fb; }
QFrame#header, QFrame#sidebar, QFrame#stat { background: white; border: 1px solid #e3e9f2; border-radius: 8px; }
QLabel#title { font-size: 19px; font-weight: 700; color: #152947; }
QLabel#muted { color: #6b7a90; }
QLabel#pill { background: #eaf2ff; color: #2563c7; border-radius: 12px; padding: 5px 13px; }
QLabel#statValue { font-size: 19px; font-weight: 700; color: #225fc9; }
QGroupBox { background: white; border: 1px solid #dfe7f1; border-radius: 8px; margin-top: 10px; padding: 11px 10px 7px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 15px; padding: 0 5px; color: #294264; }
QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTimeEdit { background: white; border: 1px solid #cfd9e7; border-radius: 5px; padding: 4px; min-height: 18px; selection-background-color: #dbeafe; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus { border: 1px solid #4a82e0; }
QPushButton { background: white; border: 1px solid #ccd8e8; border-radius: 5px; padding: 5px 10px; color: #284364; }
QPushButton:hover { background: #edf4ff; border-color: #7ca5e4; }
QPushButton#primary { background: #2c6bdb; border-color: #2c6bdb; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #245ec6; }
QPushButton#stop { background: #fff1f2; color: #b53445; border-color: #f5c4cb; }
QPushButton:disabled { color: #9ba9bb; background: #eef1f6; border-color: #dde3ec; }
QListWidget { border: none; background: transparent; outline: none; }
QListWidget::item { padding: 10px 8px; margin: 2px 4px; border-radius: 6px; color: #54677f; }
QListWidget::item:selected { background: #e9f1ff; color: #245fc8; font-weight: 600; }
QTableWidget { background: white; border: 1px solid #dfe7f1; border-radius: 6px; gridline-color: #edf1f7; }
QHeaderView::section { background: #f5f8fc; color: #52667f; padding: 8px; border: none; border-bottom: 1px solid #e1e8f2; }
QCheckBox { spacing: 7px; }
QScrollArea { border: none; background: transparent; }
QStatusBar { background: white; border-top: 1px solid #e3e9f2; }
"""

_TASK_PHASE_LABELS = {
    "authorizing": "检查登录", "prepare": "准备任务", "starting": "开始",
    "resolve_source": "解析源", "resolve_target": "解析目标",
    "scan_posts": "读取正文", "scan_comments": "读取评论",
    "download": "下载", "upload": "上传", "forward": "转发",
    "wait": "等待", "waiting": "等待", "round_complete": "本轮结果",
    "completed": "完成", "failed": "失败", "error": "错误", "uncertain": "发送结果待核对",
    "stopping": "正在停止", "stopped": "已停止", "cancelled": "已取消", "checked": "检查通过",
    "loading_chats": "读取聊天列表", "chats": "列表就绪",
    "loading_topics": "读取群组话题", "topics": "话题就绪",
    "cleanup": "清理本地文件", "cleanup_result": "自动清理结果", "cleaned": "清理结果",
}
_TASK_REASON_LABELS = {
    "source_empty": "源频道或群组当前没有可见消息。",
    "no_new": "没有超过当前同步游标的新消息。",
    "start_after_latest": "起始消息 ID 已达到或超过源最新消息 ID。",
    "no_visible_messages": "本轮范围内没有可见消息。",
    "settling": "末尾消息或相册尚未稳定，本轮暂缓处理。",
    "filtered_all": "本轮可处理内容已全部命中广告规则。",
    "download_only": "本次使用纯下载备份模式，未发送到目标。",
    "processed": "本轮处理结果已保存。",
}


def button(text, name=None):
    result = QPushButton(text)
    if name:
        result.setObjectName(name)
    result.setCursor(Qt.CursorShape.PointingHandCursor)
    return result


def note(text):
    result = QLabel(text)
    result.setObjectName("muted")
    result.setWordWrap(True)
    return result


def scroll_page():
    area = QScrollArea()
    area.setWidgetResizable(True)
    widget = QWidget()
    widget.setObjectName("page")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 6, 3)
    layout.setSpacing(6)
    area.setWidget(widget)
    return area, layout


def lines(editor):
    return [line.strip() for line in editor.toPlainText().splitlines() if line.strip()]


def configure_fonts(app):
    """Qt's offscreen platform has no system font database on Windows."""
    if not QFontDatabase.families():
        windows = Path(os.environ.get("WINDIR", "C:/Windows"))
        for name in ("msyh.ttc", "segoeui.ttf"):
            path = windows / "Fonts" / name
            if path.is_file():
                QFontDatabase.addApplicationFont(str(path))
    app.setFont(QFont("Microsoft YaHei", 9))


class ChatPickerDialog(QDialog):
    def __init__(self, chats, *, side, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择源频道 / 群组" if side == "source" else "选择目标频道 / 群组")
        self.resize(580, 400)
        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索名称、用户名或聊天 ID")
        layout.addWidget(self.search)
        self.results = QListWidget()
        self.results.setObjectName("chat_picker_results")
        layout.addWidget(self.results, 1)
        self._chats = chats
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("选择")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.results.itemDoubleClicked.connect(lambda *_: self.accept())
        self.search.textChanged.connect(self._filter)
        layout.addWidget(buttons)
        self._filter("")

    def _filter(self, text):
        self.results.clear()
        needle = text.casefold().strip()
        for chat in self._chats:
            identifier = chat.get("id")
            if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier >= 0:
                continue
            title = str(chat.get("title") or "未命名聊天")
            username = str(chat.get("username") or "").lstrip("@")
            label = f"{title}  ·  {identifier}" + (f"  ·  @{username}" if username else "")
            if chat.get("forum"):
                label += "  ·  话题群 ›"
            if needle and needle not in label.casefold():
                continue
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, chat)
            self.results.addItem(item)
        if self.results.count():
            self.results.setCurrentRow(0)

    def selected_chat(self):
        item = self.results.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None


class TopicPickerDialog(QDialog):
    def __init__(self, topics, *, side, chat_title, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择源群话题" if side == "source" else "选择目标群话题")
        self.resize(580, 400)
        layout = QVBoxLayout(self)
        self.back_button = button("‹ 返回聊天列表")
        self.back_button.clicked.connect(self._back)
        layout.addWidget(self.back_button)
        heading = QLabel(str(chat_title))
        heading.setWordWrap(True)
        heading.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(heading)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索这个群的话题名称或 ID")
        layout.addWidget(self.search)
        self.results = QListWidget()
        self.results.setObjectName("topic_picker_results")
        layout.addWidget(self.results, 1)
        self._topics = topics
        self._default_label = "整个群 / 所有话题" if side == "source" else "默认位置 / 综合"
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("选择")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.results.itemDoubleClicked.connect(lambda *_: self.accept())
        self.search.textChanged.connect(self._filter)
        layout.addWidget(buttons)
        self._filter("")
        self.return_to_chats = False

    def _back(self):
        self.return_to_chats = True
        self.reject()

    def _filter(self, text):
        self.results.clear()
        default = QListWidgetItem(self._default_label)
        default.setData(Qt.ItemDataRole.UserRole, {"id": 0, "title": self._default_label})
        self.results.addItem(default)
        needle = text.casefold().strip()
        seen = {0}
        for topic in self._topics:
            if not isinstance(topic, dict):
                continue
            identifier = topic.get("id")
            if isinstance(identifier, bool) or not isinstance(identifier, int) or not 1 <= identifier <= 2147483647 or identifier in seen:
                continue
            seen.add(identifier)
            title = str(topic.get("title") or "未命名话题")
            label = f"{title}  ·  #{identifier}" + ("  ·  已关闭" if topic.get("closed") else "")
            if topic.get("hidden"):
                label += "  ·  已隐藏"
            if needle and needle not in label.casefold():
                continue
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, topic)
            self.results.addItem(item)
        self.results.setCurrentRow(0)

    def selected_topic(self):
        item = self.results.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None


class PendingOperationsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("待核对发送 · 本地记录")
        self.resize(980, 460)
        self._busy = False
        layout = QVBoxLayout(self)
        instructions = QLabel("先在 Telegram 核对目标，并清理该批可能已发送的全部内容（相册须整批核对）。解除只清本地阻塞，不删除目标消息，也不发送内容；下一次开始搬运时才会重试。")
        instructions.setWordWrap(True)
        instructions.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(instructions)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["源 / 话题", "目标 / 话题", "模式", "源消息 ID（整批）", "创建时间（UTC）", "范围"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setStyleSheet("QTableWidget::item:selected { background:#e9f1ff; color:#245fc8; }")
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(52)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)
        self.message = QLabel("正在读取本地待核对记录…")
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.message)
        self.acknowledge = QCheckBox("我已核对目标并清理这批可能已发送的内容，允许重新发送")
        self.acknowledge.toggled.connect(self._update_actions)
        layout.addWidget(self.acknowledge)
        actions = QHBoxLayout()
        self.refresh_button = button("刷新")
        self.resolve_button = button("解除所选批次阻塞")
        self.resolve_button.setEnabled(False)
        close_button = button("关闭")
        close_button.clicked.connect(self.reject)
        actions.addWidget(self.refresh_button)
        actions.addStretch()
        actions.addWidget(self.resolve_button)
        actions.addWidget(close_button)
        layout.addLayout(actions)

    def selected_operation(self):
        rows = self.table.selectionModel().selectedRows()
        item = self.table.item(rows[0].row(), 0) if len(rows) == 1 else None
        data = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(data, dict):
            return None
        ids = data.get("source_ids")
        if (not all(isinstance(data.get(key), str) and data[key] for key in ("route", "key", "scope"))
                or data["scope"] not in {"post", "posts", "comment", "comments"}
                or not isinstance(ids, list) or not ids
                or not all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in ids)):
            return None
        return data

    def set_rows(self, rows):
        self.table.clearSelection()
        self.acknowledge.setChecked(False)
        self.table.setRowCount(len(rows))
        modes = {"download_then_forward": "下载后转发", "forward": "直接转发", "reupload": "重传", "download_only": "纯备份"}
        for index, data in enumerate(rows):
            def peer(side):
                topic = data.get(f"{side}_topic_id", 0)
                scope = ("评论线程" if data.get("scope") in {"comment", "comments"}
                         else f"话题 #{topic}" if topic else "未限定话题" if side == "source" else "默认位置")
                return str(data.get(side, "未知聊天")) + "\n" + scope
            ids = data.get("source_ids", [])
            values = [peer("source"), peer("target"), modes.get(data.get("mode"), "原任务模式"),
                      ", ".join(str(value) for value in ids) if isinstance(ids, list) else "无效记录",
                      str(data.get("created_at", "")), "评论" if data.get("scope") in {"comment", "comments"} else "正文"]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, data)
                self.table.setItem(index, column, item)
        self._update_actions()

    def set_busy(self, busy):
        self._busy = bool(busy)
        self.refresh_button.setEnabled(not busy)
        self.table.setEnabled(not busy)
        self.acknowledge.setEnabled(not busy)
        self._update_actions()

    def _selection_changed(self):
        self.acknowledge.setChecked(False)
        self._update_actions()

    def _update_actions(self, *_):
        self.resolve_button.setEnabled(not self._busy and self.acknowledge.isChecked() and self.selected_operation() is not None)


class MainWindow(QMainWindow):
    def __init__(self, *, config_path=None, controller=None):
        super().__init__()
        configure_fonts(QApplication.instance())
        self.setWindowTitle("Telegram 搬运工作台 · 广告过滤与限速")
        self.resize(1180, 780)
        self.setMinimumSize(900, 540)
        self.setStyleSheet(STYLE)
        self.config_path = Path(config_path or default_config_path()).resolve()
        self.controller = controller or GuiController()
        self.draft = {}
        self.preview_rows = []
        self._close_requested = self._closed = False
        self._auth_challenge = False
        self._challenge_identity = None
        self._password_pending = False
        self._auth_operation = None
        self._auth_end_pending = False
        self._code_delivery_state = None
        self._code_delivery_identity = None
        self._resend_deadline = 0.0
        self._resend_request_pending = False
        self._resend_timer = QTimer(self)
        self._resend_timer.setInterval(1000)
        self._resend_timer.timeout.connect(self._refresh_resend)
        self._application_ready = False
        self._new_account_mode = False
        self._account_authenticated = False
        self._chat_selection_request = None
        self._pending_chat_choices = None
        self._topic_selection_request = None
        self._pending_topic_choices = None
        self._pending_dialog = None
        self._pending_request = None
        self._pending_context = None
        self._pending_refresh_after_resolve = False
        self._topic_ids = {"source": 0, "target": 0}
        self._topic_titles = {"source": "", "target": ""}
        self._loading_draft = False
        self._task_operation = None
        self._task_active = False
        self._task_terminal_seen = False
        self._task_phase_key = "idle"
        self._task_reason = ""
        self._task_last_message = ""
        self._last_worker_task_status = ""
        self._last_task_log_key = None
        self._task_counts = {"processed": 0, "sent": 0, "filtered": 0, "files": 0}
        self.account_store = AccountStore(self.config_path)
        self.fields = {}
        self._wheel_protected = set()
        self._build()
        self._wire()
        self._load()
        self.statusBar().showMessage("就绪 · 输入国际手机号并获取验证码；首次应用设置完成后会自动保存")

    def _build(self):
        workspace = QWidget()
        workspace.setObjectName("workspace")
        outer = QVBoxLayout(workspace)
        outer.setContentsMargins(10, 8, 10, 4)
        outer.setSpacing(7)
        header = QFrame()
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 7, 12, 7)
        badge = QLabel("TG")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(34, 34)
        badge.setStyleSheet("background:#2c6bdb;color:white;border-radius:9px;font-size:16px;font-weight:700;")
        header_layout.addWidget(badge)
        heading = QVBoxLayout()
        title = QLabel("Telegram 搬运工作台")
        title.setObjectName("title")
        heading.addWidget(title)
        header_layout.addLayout(heading, 1)
        self.account_label = QLabel("未连接账号")
        self.account_label.setObjectName("pill")
        header_layout.addWidget(self.account_label)
        outer.addWidget(header)
        body = QHBoxLayout()
        body.setSpacing(8)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(142)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(5, 7, 5, 7)
        self.navigation = QListWidget()
        self.navigation.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.navigation.addItems(["01  搬运任务", "02  广告过滤", "03  发送限速", "04  运行记录"])
        side.addWidget(self.navigation)
        side.addStretch()
        self.folder_button = button("打开数据目录")
        side.addWidget(self.folder_button)
        body.addWidget(sidebar)
        content = QVBoxLayout()
        config_bar = QHBoxLayout()
        config_bar.addWidget(QLabel("当前配置"))
        self.path_label = QLineEdit(str(self.config_path))
        self.path_label.setReadOnly(True)
        config_bar.addWidget(self.path_label, 1)
        self.config_button = button("切换配置")
        self.save_button = button("保存配置")
        config_bar.addWidget(self.config_button)
        config_bar.addWidget(self.save_button)
        content.addLayout(config_bar)
        stats = QHBoxLayout()
        self.stat_labels = {}
        for key, text in (("processed", "本次已处理"), ("sent", "已发送消息"), ("filtered", "已屏蔽广告"), ("files", "媒体文件")):
            card = QFrame()
            card.setObjectName("stat")
            row = QVBoxLayout(card)
            row.setContentsMargins(10, 5, 10, 5)
            row.setSpacing(0)
            row.addWidget(note(text))
            value = QLabel("0")
            value.setObjectName("statValue")
            row.addWidget(value)
            self.stat_labels[key] = value
            stats.addWidget(card)
        content.addLayout(stats)
        task_card = QFrame()
        task_card.setObjectName("stat")
        task_card_layout = QVBoxLayout(task_card)
        task_card_layout.setContentsMargins(10, 6, 10, 6)
        task_card_layout.setSpacing(2)
        self.task_status = QLabel("就绪")
        self.task_status.setTextFormat(Qt.TextFormat.PlainText)
        self.task_status.setWordWrap(True)
        self.task_status.setStyleSheet("font-weight:600;color:#294264;")
        self.task_detail = note("")
        self.task_detail.setVisible(False)
        self.task_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.task_progress = QProgressBar()
        self.task_progress.setRange(0, 100)
        self.task_progress.setValue(0)
        self.task_progress.setTextVisible(False)
        self.task_progress.setFixedHeight(4)
        self.task_progress.setVisible(False)
        self.task_progress.setStyleSheet("QProgressBar{background:#e4eaf4;border:0;border-radius:3px;} QProgressBar::chunk{background:#2c6bdb;border-radius:3px;}")
        task_card_layout.addWidget(self.task_status)
        task_card_layout.addWidget(self.task_detail)
        task_card_layout.addWidget(self.task_progress)
        content.addWidget(task_card)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._task_page())
        self.stack.addWidget(self._filter_page())
        self.stack.addWidget(self._limits_page())
        self.stack.addWidget(self._log_page())
        content.addWidget(self.stack, 1)
        content.addLayout(self.task_actions)
        self.feedback = QLabel("")
        self.feedback.setWordWrap(True)
        self.feedback.setStyleSheet("padding:2px;color:#52677f;")
        self.feedback.setVisible(False)
        content.addWidget(self.feedback)
        body.addLayout(content, 1)
        outer.addLayout(body, 1)
        self.setCentralWidget(workspace)
        self.navigation.setCurrentRow(0)

    def _line(self, key, placeholder=""):
        field = QLineEdit()
        field.setPlaceholderText(placeholder)
        field.setObjectName(key.replace(".", "_"))
        self.fields[key] = field
        return field

    def _check(self, key, text):
        field = QCheckBox(text)
        self.fields[key] = field
        return field

    def _combo(self, key, choices):
        field = QComboBox()
        self._protect_wheel(field)
        for label, value in choices:
            field.addItem(label, value)
        self.fields[key] = field
        return field

    def _number(self, key, maximum, *, decimal=False):
        field = QDoubleSpinBox() if decimal else QSpinBox()
        self._protect_wheel(field)
        field.setRange(0, maximum)
        if decimal:
            field.setDecimals(1)
        self.fields[key] = field
        return field

    def _protect_wheel(self, field):
        self._wheel_protected.add(field)
        field.installEventFilter(self)
        for editor in field.findChildren(QLineEdit):
            self._wheel_protected.add(editor)
            editor.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Wheel and watched in self._wheel_protected:
            parent = watched.parentWidget()
            while parent is not None and not isinstance(parent, QScrollArea):
                parent = parent.parentWidget()
            if parent is not None:
                scrollbar = parent.verticalScrollBar()
                pixels = event.pixelDelta().y()
                delta = pixels if pixels else round(event.angleDelta().y() / 120 * scrollbar.singleStep() * 3)
                scrollbar.setValue(scrollbar.value() - delta)
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def _multiline(self, key, placeholder, height=94):
        field = QPlainTextEdit()
        field.setPlaceholderText(placeholder)
        field.setFixedHeight(height)
        self.fields[key] = field
        return field

    def _task_page(self):
        area, layout = scroll_page()
        account = QGroupBox("账号")
        account_layout = QVBoxLayout(account)
        account_layout.setContentsMargins(7, 3, 7, 3)
        account_layout.setSpacing(5)
        account_bar = QHBoxLayout()
        account_bar.setSpacing(5)
        self.account_selector = QComboBox()
        self._protect_wheel(self.account_selector)
        self.account_selector.setObjectName("account_selector")
        self.switch_account_button = button("切换")
        self.add_account_button = button("添加账号")
        self.login_management_button = button("收起登录")
        self.login_management_button.setCheckable(True)
        self.login_management_button.setChecked(True)
        account_bar.addWidget(self.account_selector, 1)
        account_bar.addWidget(self.switch_account_button)
        account_bar.addWidget(self.add_account_button)
        account_bar.addWidget(self.login_management_button)
        account_layout.addLayout(account_bar)
        self.login_details = QWidget()
        form = QGridLayout(self.login_details)
        self._login_form = form
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(5)
        account_layout.addWidget(self.login_details)
        self.application_status = note("正在检查本机应用设置…")
        self.application_status.setTextFormat(Qt.TextFormat.PlainText)
        self.app_settings_toggle = button("首次应用设置")
        self.app_settings_toggle.setCheckable(True)
        self.app_settings_toggle.setChecked(True)
        form.addWidget(self.application_status, 0, 0, 1, 4)
        form.addWidget(self.app_settings_toggle, 0, 4)
        self.app_settings_panel = QWidget()
        self.app_settings_panel.setObjectName("application_settings")
        app_form = QGridLayout(self.app_settings_panel)
        app_form.setContentsMargins(0, 2, 0, 8)
        self.api_id = self._line("telegram.api_id", "你在 my.telegram.org/apps 申请的 API_ID")
        self.api_id.setValidator(QIntValidator(0, 2147483647, self))
        self.api_hash = self._line("telegram.api_hash", "API_HASH（保存在当前本机配置）")
        self.api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        app_form.addWidget(QLabel("应用 ID"), 0, 0)
        app_form.addWidget(self.api_id, 0, 1)
        app_form.addWidget(QLabel("应用密钥"), 0, 2)
        app_form.addWidget(self.api_hash, 0, 3)
        self.show_hash = QCheckBox("显示")
        app_form.addWidget(self.show_hash, 0, 4)
        self.environment_hint = note(effective_credentials_hint())
        app_form.addWidget(self.environment_hint, 1, 0, 1, 4)
        app_link = button("申请应用参数 ↗")
        app_link.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://my.telegram.org/apps")))
        app_form.addWidget(app_link, 1, 4)
        app_form.addWidget(note("所有账号共用这套应用设置。保存一次后，添加或切换账号无需重新填写。"), 2, 0, 1, 4)
        self.save_app_settings_button = button("保存应用设置")
        app_form.addWidget(self.save_app_settings_button, 2, 4)
        form.addWidget(self.app_settings_panel, 1, 0, 1, 5)
        self.phone = QLineEdit()
        self.phone.setPlaceholderText("国际手机号：菲律宾 +63… / 美国 +1… / 英国 +44…；不会保存")
        self.phone.setObjectName("phone")
        self.code = QLineEdit()
        self.code.setPlaceholderText("Telegram 验证码")
        self.code.setEchoMode(QLineEdit.EchoMode.Password)
        self.code.setObjectName("verification_code")
        self.connect_button = button("恢复已有会话")
        self.code_button = button("发送验证码")
        self.login_button = button("验证并登录", "primary")
        self.phone_label = QLabel("手机号")
        form.addWidget(self.phone_label, 2, 0)
        form.addWidget(self.phone, 2, 1, 1, 2)
        form.addWidget(self.code_button, 2, 3)
        form.addWidget(self.connect_button, 2, 4)
        self.code_label = QLabel("验证码")
        form.addWidget(self.code_label, 3, 0)
        form.addWidget(self.code, 3, 1, 1, 2)
        form.addWidget(self.login_button, 3, 3)
        self.resend_button = button("重新获取验证码")
        self.resend_button.setObjectName("resend_code")
        self.resend_button.setVisible(False)
        form.addWidget(self.resend_button, 3, 4)
        self.code_delivery_label = QLabel()
        self.code_delivery_label.setObjectName("code_delivery")
        self.code_delivery_label.setTextFormat(Qt.TextFormat.PlainText)
        self.code_delivery_label.setWordWrap(True)
        self.code_delivery_label.setStyleSheet("color:#52667f;padding:2px;")
        self.code_delivery_label.setVisible(False)
        form.addWidget(self.code_delivery_label, 4, 0, 1, 5)
        self.password_row = QGroupBox("第2步 · 两步验证密码")
        self.password_row.setObjectName("two_factor_step")
        self.password_row.setStyleSheet(
            "QGroupBox#two_factor_step { background:#fff7df; border:2px solid #e3ab3c;"
            "border-radius:8px; margin-top:13px; padding:15px 12px 11px; }"
            "QGroupBox#two_factor_step::title { color:#845008; font-size:17px; font-weight:700;"
            "subcontrol-origin:margin; left:12px; padding:0 5px; }"
        )
        password_layout = QVBoxLayout(self.password_row)
        password_layout.setContentsMargins(0, 0, 0, 0)
        self.password_instruction = QLabel("验证码已通过，请输入你在 Telegram 设置的两步验证密码。")
        self.password_instruction.setObjectName("two_factor_instruction")
        self.password_instruction.setTextFormat(Qt.TextFormat.PlainText)
        self.password_instruction.setWordWrap(True)
        self.password_instruction.setStyleSheet("color:#704500;font-size:14px;font-weight:600;")
        password_layout.addWidget(self.password_instruction)
        password_actions = QHBoxLayout()
        self._password_actions = password_actions
        self._password_cancel_embedded = False
        password_layout.addLayout(password_actions)
        self.password = QLineEdit()
        self.password.setObjectName("two_factor_password")
        self.password.setPlaceholderText("输入 Telegram 两步验证密码；前后空格会保留，不会保存")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        password_actions.addWidget(self.password, 1)
        self.password_button = button("验证密码", "primary")
        password_actions.addWidget(self.password_button)
        self.password_row.setVisible(False)
        form.addWidget(self.password_row, 4, 0, 1, 5)
        self.auth_status = QLabel("支持任意国家和地区。输入带国际区号的手机号，点击“发送验证码”开始登录。")
        self.auth_status.setObjectName("auth_status")
        self.auth_status.setWordWrap(True)
        self.auth_status.setTextFormat(Qt.TextFormat.PlainText)
        self.auth_status.setStyleSheet("padding:10px;background:#edf4ff;color:#345c86;border-radius:6px;")
        self.auth_stop_button = button("停止当前操作", "stop")
        self.auth_stop_button.setEnabled(False)
        form.addWidget(self.auth_status, 5, 0, 1, 4)
        form.addWidget(self.auth_stop_button, 5, 4)
        layout.addWidget(account)
        task = QGroupBox("搬运任务")
        grid = QGridLayout(task)
        grid.addWidget(QLabel("源频道 / 群组"), 0, 0)
        source_row = QWidget()
        source_layout = QHBoxLayout(source_row)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.setSpacing(4)
        source_field = self._line("sync.source", "频道/帖子链接、@用户名或 ID")
        source_field.setToolTip("支持频道链接、帖子链接、@用户名或聊天 ID。帖子链接只识别所属聊天，不会修改起始消息 ID。")
        source_layout.addWidget(source_field, 1)
        self.source_topic_label = QLabel("全部话题")
        self.source_topic_label.setTextFormat(Qt.TextFormat.PlainText)
        self.source_topic_label.setObjectName("muted")
        self.source_topic_label.setMaximumWidth(92)
        source_layout.addWidget(self.source_topic_label)
        self.choose_source_button = button("选择")
        self.choose_source_button.setFixedWidth(48)
        source_layout.addWidget(self.choose_source_button)
        grid.addWidget(source_row, 0, 1)
        grid.addWidget(QLabel("目标频道 / 群组"), 0, 2)
        target_row = QWidget()
        target_layout = QHBoxLayout(target_row)
        target_layout.setContentsMargins(0, 0, 0, 0)
        target_layout.setSpacing(4)
        target_field = self._line("sync.target", "频道/帖子链接、@用户名或 ID")
        target_field.setToolTip("支持频道链接、帖子链接、@用户名或聊天 ID。帖子链接仅识别所属聊天。")
        target_layout.addWidget(target_field, 1)
        self.target_topic_label = QLabel("默认位置")
        self.target_topic_label.setTextFormat(Qt.TextFormat.PlainText)
        self.target_topic_label.setObjectName("muted")
        self.target_topic_label.setMaximumWidth(92)
        target_layout.addWidget(self.target_topic_label)
        self.choose_target_button = button("选择")
        self.choose_target_button.setFixedWidth(48)
        target_layout.addWidget(self.choose_target_button)
        grid.addWidget(target_row, 0, 3)
        mode = self._combo("sync.mode", [
            ("下载后重新上传", "reupload"), ("直接转发", "forward"),
            ("只下载备份", "download_only"), ("下载留档后转发", "download_then_forward")])
        grid.addWidget(QLabel("正文模式"), 1, 0)
        grid.addWidget(mode, 1, 1)
        self.comments = self._check("sync.sync_comments", "同步频道评论区")
        grid.addWidget(self.comments, 1, 2)
        grid.addWidget(self._combo("sync.comment_mode", [("评论下载重传", "reupload"), ("评论只备份", "download_only")]), 1, 3)
        grid.addWidget(QLabel("下载文件夹"), 2, 0)
        download_row = QWidget()
        download_layout = QHBoxLayout(download_row)
        download_layout.setContentsMargins(0, 0, 0, 0)
        download_layout.setSpacing(4)
        download_field = self._line("storage.download_dir", "选择可用硬盘上的文件夹")
        download_field.setToolTip("所有媒体先保存到此目录；相对路径以当前配置文件所在文件夹为基准。可选择另一硬盘。")
        download_layout.addWidget(download_field, 1)
        self.choose_download_button = button("选择文件夹")
        self.open_download_button = button("打开")
        download_layout.addWidget(self.choose_download_button)
        download_layout.addWidget(self.open_download_button)
        grid.addWidget(download_row, 2, 1, 1, 3)
        cleanup_row = QWidget()
        cleanup_layout = QHBoxLayout(cleanup_row)
        cleanup_layout.setContentsMargins(0, 0, 0, 0)
        self.auto_cleanup = self._check("storage.auto_cleanup_forwarded", "转发成功后自动清理本次下载文件")
        self.auto_cleanup.setToolTip("仅“下载留档后转发”模式生效；确认成功转发后才清理，失败、待确认和纯备份媒体保留。")
        cleanup_layout.addWidget(self.auto_cleanup)
        self.once = QCheckBox("本次只运行一轮")
        self.once.setChecked(True)
        cleanup_layout.addWidget(self.once)
        cleanup_layout.addStretch()
        self.cleanup_button = button("一键清理已转发文件")
        self.cleanup_button.setToolTip("只清理当前下载目录中已确认成功转发的本地媒体；失败、待确认和纯备份文件保留。清理旧目录时，请先切回对应下载文件夹。")
        cleanup_layout.addWidget(self.cleanup_button)
        grid.addWidget(cleanup_row, 3, 0, 1, 4)
        self.comments.setToolTip("同步普通频道评论区时，目标频道需开启链接讨论组。群组话题在聊天选择中指定。")
        layout.addWidget(task)
        self.paths_toggle = button("高级设置：起点、循环与会话")
        self.paths_toggle.setCheckable(True)
        layout.addWidget(self.paths_toggle)
        paths = QWidget()
        self.paths_panel = paths
        paths.setVisible(False)
        path_form = QFormLayout(paths)
        path_form.setContentsMargins(5, 0, 5, 3)
        path_form.addRow("起始消息 ID", self._number("sync.start_id", 2147483647))
        path_form.addRow("循环间隔（秒）", self._number("sync.poll_interval", 86400, decimal=True))
        connections = self._number("storage.download_connections", 8)
        connections.setMinimum(1)
        connections.setToolTip("仅用于媒体下载，默认4路，可设1到8路；不改变转发/上传限速，也不会重发历史消息。实际速度仍取决于网络和 Telegram 服务端。")
        path_form.addRow("下载并行数", connections)
        path_form.addRow("登录会话", self._line("telegram.session", "data/telegram_user"))
        path_form.addRow("映射与限速记录", self._line("storage.database", "data/state.sqlite3"))
        layout.addWidget(paths)
        actions = QHBoxLayout()
        self.check_button = button("检查连接与评论区")
        self.task_preview_button = button("预览最近消息")
        self.start_button = button("开始搬运", "primary")
        self.stop_button = button("安全停止", "stop")
        self.stop_button.setEnabled(False)
        for widget in (self.check_button, self.task_preview_button):
            actions.addWidget(widget)
        actions.addStretch()
        actions.addWidget(self.start_button)
        actions.addWidget(self.stop_button)
        self.task_actions = actions
        layout.addStretch()
        mode.currentIndexChanged.connect(self._mode_changed)
        return area

    def _filter_page(self):
        area, layout = scroll_page()
        controls = QGroupBox("广告过滤规则")
        grid = QGridLayout(controls)
        grid.addWidget(self._check("filters.enabled", "启用广告过滤"), 0, 0)
        grid.addWidget(self._check("filters.apply_to_comments", "同时过滤评论"), 0, 1)
        grid.addWidget(self._check("filters.apply_to_downloads", "纯下载备份也过滤"), 0, 2)
        grid.addWidget(QLabel("屏蔽关键词 · 每行一个"), 1, 0)
        grid.addWidget(QLabel("屏蔽域名 · 每行一个"), 1, 1)
        grid.addWidget(QLabel("白名单关键词 · 纠正自动误判"), 1, 2)
        grid.addWidget(self._multiline("filters.keywords", "如：商务合作、广告投放\n不建议只填“广告”这类宽泛词"), 2, 0)
        grid.addWidget(self._multiline("filters.blocked_domains", "如：ads.example.com\n同时匹配其真实子域名"), 2, 1)
        grid.addWidget(self._multiline("filters.allowed_keywords", "自动规则命中时整组放行\n不能覆盖手动屏蔽，谨慎设置"), 2, 2)
        grid.addWidget(QLabel("正则表达式 · 每行一条"), 3, 0)
        grid.addWidget(QLabel("允许的链接域名"), 3, 1)
        grid.addWidget(QLabel("手动屏蔽 · 群ID:消息ID"), 3, 2)
        grid.addWidget(self._multiline("filters.regex_patterns", "可选高级规则；执行设有超时保护", 70), 4, 0)
        grid.addWidget(self._multiline("filters.allowed_domains", "仅豁免“拦截全部链接”规则", 70), 4, 1)
        grid.addWidget(self._multiline("filters.blocked_message_keys", "也可从下方消息预览中选取", 70), 4, 2)
        grid.addWidget(self._check("filters.block_all_links", "严格：屏蔽全部外链"), 5, 0)
        grid.addWidget(self._check("filters.block_buttons", "严格：屏蔽带按钮消息"), 5, 1, 1, 2)
        layout.addWidget(controls)
        layout.addWidget(note("会检查正文、字幕、文件名、隐藏链接与按钮。相册任一成员命中时整组跳过；仅文字规则不识别图片或视频中的广告。"))
        sample = QGroupBox("离线试一下 · 不需要登录")
        sample_row = QVBoxLayout(sample)
        self.sample_text = QPlainTextEdit("商务合作请联系 https://ads.example.com")
        self.sample_text.setFixedHeight(62)
        sample_row.addWidget(self.sample_text)
        sample_actions = QHBoxLayout()
        self.sample_button = button("测试这段内容")
        self.sample_result = QLabel("修改上面的规则后，点击测试查看判定原因。")
        self.sample_result.setWordWrap(True)
        sample_actions.addWidget(self.sample_button)
        sample_actions.addWidget(self.sample_result, 1)
        sample_row.addLayout(sample_actions)
        layout.addWidget(sample)
        preview = QGroupBox("源消息预览 · 只读，不发送")
        preview_layout = QVBoxLayout(preview)
        row = QHBoxLayout()
        self.preview_button = button("预览最近 50 条")
        self.block_selected_button = button("屏蔽所选消息 / 相册")
        row.addWidget(self.preview_button)
        row.addWidget(self.block_selected_button)
        row.addStretch()
        preview_layout.addLayout(row)
        self.preview_table = self._table(["消息 ID", "内容摘要", "媒体", "判定", "原因"], height=235)
        preview_layout.addWidget(self.preview_table)
        preview_layout.addWidget(note("规则修改后请重新预览。未命中不等于一定没有广告；纯图片广告可在预览中手动屏蔽。"))
        layout.addWidget(preview)
        return area

    def _limits_page(self):
        area, layout = scroll_page()
        limits = QGroupBox("发送次数与节奏")
        form = QFormLayout(limits)
        form.addRow(self._check("limits.enabled", "启用本地发送额度与间隔"))
        labels = [
            ("两次发送至少间隔（秒）", "min_send_interval", 3600, True),
            ("读取请求间隔（秒）", "min_request_interval", 60, True),
            ("滚动 60 秒最多消息数", "max_messages_per_minute", 100000, False),
            ("滚动 1 小时最多消息数", "max_messages_per_hour", 1000000, False),
            ("滚动 24 小时最多消息数", "max_messages_per_day", 10000000, False),
            ("本次运行最多消息数", "max_messages_per_run", 1000000, False),
            ("本次最长运行（分钟）", "max_runtime_minutes", 10080, True),
            ("最长自动等待（秒）", "max_auto_wait", 86400, True),
            ("明确等待错误最多重试次数", "retry_wait_errors", 10, False),
            ("服务器等待额外余量（秒）", "wait_padding", 60, True),
        ]
        for label, key, maximum, decimal in labels:
            form.addRow(label, self._number("limits." + key, maximum, decimal=decimal))
        for key, label in (("active_start", "允许发送开始"), ("active_end", "允许发送结束")):
            edit = QTimeEdit()
            self._protect_wheel(edit)
            edit.setDisplayFormat("HH:mm")
            self.fields["limits." + key] = edit
            form.addRow(label, edit)
        form.addRow("时区（固定 UTC 偏移）", self._line("limits.timezone", "+08:00"))
        layout.addWidget(limits)
        layout.addWidget(note("开始与结束相同表示全天；支持跨午夜。条数预算为 0 表示关闭该项。正文和评论共享额度，相册按成员数计数；冷却和账号停止状态重启后仍保留。"))
        layout.addWidget(note("这些数值是工具策略，Telegram 没有公开统一安全配额。减慢发送不能保证账号不被举报或限制。"))
        status_box = QGroupBox("本机限速与账号状态")
        status_layout = QVBoxLayout(status_box)
        self.refresh_limits_button = button("刷新本机状态（不联网）")
        self.limit_status = QPlainTextEdit()
        self.limit_status.setReadOnly(True)
        self.limit_status.setFixedHeight(165)
        status_layout.addWidget(self.refresh_limits_button)
        status_layout.addWidget(self.limit_status)
        layout.addWidget(status_box)
        return area

    def _table(self, columns, height=220):
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.setMinimumHeight(height)
        return table

    def _log_page(self):
        area, layout = scroll_page()
        row = QHBoxLayout()
        row.addWidget(QLabel("实时运行日志"))
        row.addStretch()
        self.clear_log_button = button("清空窗口日志")
        self.refresh_history_button = button("刷新广告拦截记录")
        self.pending_button = button("待核对发送")
        row.addWidget(self.pending_button)
        row.addWidget(self.clear_log_button)
        row.addWidget(self.refresh_history_button)
        layout.addLayout(row)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setMinimumHeight(200)
        layout.addWidget(self.log_view, 1)
        layout.addWidget(note("拦截记录保留原消息与原因；规则修改只影响未处理的新消息，已跳过的历史消息不会自动补发。"))
        self.history_table = self._table(["源 ID", "内容摘要", "范围", "拦截原因"], height=185)
        layout.addWidget(self.history_table, 1)
        return area

    def _wire(self):
        self.navigation.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.save_button.clicked.connect(self._save_clicked)
        self.save_app_settings_button.clicked.connect(self._save_clicked)
        self.app_settings_toggle.toggled.connect(self._toggle_app_settings)
        self.login_management_button.toggled.connect(self._toggle_login_management)
        self.paths_toggle.toggled.connect(self.paths_panel.setVisible)
        self.choose_source_button.clicked.connect(lambda: self._pick_chat("source"))
        self.choose_target_button.clicked.connect(lambda: self._pick_chat("target"))
        self.api_id.textChanged.connect(lambda: self._refresh_application_settings())
        self.api_hash.textChanged.connect(lambda: self._refresh_application_settings())
        self.config_button.clicked.connect(self._choose_config)
        self.show_hash.toggled.connect(lambda show: self.api_hash.setEchoMode(QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password))
        self.connect_button.clicked.connect(self._connect_or_cancel)
        self.code_button.clicked.connect(lambda: self._action("code"))
        self.resend_button.clicked.connect(self._resend_code)
        self.login_button.clicked.connect(lambda: self._action("login"))
        self.code.returnPressed.connect(lambda: self._action("login"))
        self.password_button.clicked.connect(lambda: self._action("password"))
        self.password.returnPressed.connect(lambda: self._action("password"))
        self.auth_stop_button.clicked.connect(self._stop)
        self.switch_account_button.clicked.connect(self._switch_account)
        self.add_account_button.clicked.connect(self._add_account)
        self.check_button.clicked.connect(lambda: self._action("check"))
        self.start_button.clicked.connect(lambda: self._action("run"))
        self.stop_button.clicked.connect(self._stop)
        self.task_preview_button.clicked.connect(lambda: self._action("preview"))
        self.preview_button.clicked.connect(lambda: self._action("preview"))
        self.sample_button.clicked.connect(self._sample)
        self.block_selected_button.clicked.connect(self._block_selected)
        self.refresh_history_button.clicked.connect(self._history)
        self.pending_button.clicked.connect(self._open_pending)
        self.clear_log_button.clicked.connect(self.log_view.clear)
        self.refresh_limits_button.clicked.connect(self._limit_status)
        self.folder_button.clicked.connect(self._open_data)
        self.choose_download_button.clicked.connect(self._choose_download_directory)
        self.open_download_button.clicked.connect(self._open_download_directory)
        self.cleanup_button.clicked.connect(self._cleanup_downloads)
        self.controller.status.connect(self._worker_status)
        self.controller.log.connect(self.log_view.appendPlainText)
        self.controller.authenticated.connect(self._authenticated)
        self.controller.code_sent.connect(self._code_sent)
        if hasattr(self.controller, "code_delivery"):
            self.controller.code_delivery.connect(self._code_delivery)
        self.controller.password_required.connect(self._password_required)
        self.controller.challenge_ended.connect(self._challenge_ended)
        self.controller.preview_ready.connect(self._preview_ready)
        self.controller.stats.connect(self._stats)
        self.controller.failed.connect(self._worker_failed)
        self.controller.finished.connect(self._finished)
        self.controller.busy_changed.connect(self._busy)
        if hasattr(self.controller, "task_phase"):
            self.controller.task_phase.connect(self._task_phase_event)
        if hasattr(self.controller, "chats_ready"):
            self.controller.chats_ready.connect(self._chats_ready)
        if hasattr(self.controller, "topics_ready"):
            self.controller.topics_ready.connect(self._topics_ready)
        if hasattr(self.controller, "cleanup_ready"):
            self.controller.cleanup_ready.connect(self._cleanup_ready)
        if hasattr(self.controller, "pending_ready"):
            self.controller.pending_ready.connect(self._pending_ready)
        for side in ("source", "target"):
            self.fields[f"sync.{side}"].textChanged.connect(lambda _, side=side: self._peer_changed(side))
        for field in (self.phone, self.api_id, self.api_hash, self.fields["telegram.session"]):
            field.textChanged.connect(self._refresh_resend)

    def _load(self):
        self._loading_draft = True
        invalid_cleanup = False
        try:
            self.draft = read_draft(self.config_path)
            for key, field in self.fields.items():
                section, name = key.split(".", 1)
                value = self.draft.get(section, {}).get(name, "")
                if isinstance(field, QCheckBox):
                    if key == "storage.auto_cleanup_forwarded":
                        field.setChecked(value is True)
                        invalid_cleanup = not isinstance(value, bool)
                    else:
                        field.setChecked(bool(value))
                elif isinstance(field, QComboBox):
                    index = field.findData(value)
                    if index >= 0:
                        field.setCurrentIndex(index)
                elif isinstance(field, QTimeEdit):
                    field.setTime(QTime.fromString(str(value), "HH:mm"))
                elif isinstance(field, (QSpinBox, QDoubleSpinBox)):
                    field.setValue(float(value or 0) if isinstance(field, QDoubleSpinBox) else int(value or 0))
                elif isinstance(field, QPlainTextEdit):
                    field.setPlainText("\n".join(str(item) for item in value) if isinstance(value, (tuple, list)) else str(value or ""))
                else:
                    field.setText("" if key == "telegram.api_id" and not value else str(value or ""))
            for side in ("source", "target"):
                self._topic_ids[side] = self.draft.get("sync", {}).get(f"{side}_topic_id", 0)
                self._topic_titles[side] = ""
                self._update_topic_label(side)
            self.path_label.setText(str(self.config_path))
            self._mode_changed()
            self._refresh_application_settings(collapse=True)
            self.account_store = AccountStore(self.config_path)
            self._refresh_accounts()
            if invalid_cleanup:
                self._feedback("自动清理配置必须是 true 或 false；当前保持关闭，请检查后保存设置。", error=True)
        except Exception as exc:
            self._feedback(f"读取配置失败（{type(exc).__name__}）。请切换到有效配置文件。", error=True)
        finally:
            self._loading_draft = False

    def collect_draft(self):
        draft = copy.deepcopy(self.draft)
        for key, field in self.fields.items():
            section, name = key.split(".", 1)
            if isinstance(field, QCheckBox):
                value = field.isChecked()
            elif isinstance(field, QComboBox):
                value = field.currentData()
            elif isinstance(field, QTimeEdit):
                value = field.time().toString("HH:mm")
            elif isinstance(field, (QSpinBox, QDoubleSpinBox)):
                value = field.value()
            elif isinstance(field, QPlainTextEdit):
                value = lines(field)
            else:
                value = field.text().strip()
                if key == "telegram.api_id":
                    value = int(value) if value else 0
                elif key in {"sync.source", "sync.target"} and value.lstrip("-").isdigit():
                    value = int(value)
            draft.setdefault(section, {})[name] = value
        if draft["sync"]["mode"] == "download_only":
            draft["sync"]["comment_mode"] = "download_only"
        for side in ("source", "target"):
            draft["sync"][f"{side}_topic_id"] = self._topic_ids[side]
        return draft

    def _update_topic_label(self, side):
        label = getattr(self, f"{side}_topic_label")
        identifier = self._topic_ids[side]
        if identifier:
            title = self._topic_titles[side]
            text = f"话题：{title}" if title else f"话题 #{identifier}"
            label.setText(label.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, 90))
            label.setToolTip((f"{title}\n" if title else "") + f"话题 ID：{identifier}。点击“选择”可重新读取群组及话题。")
        else:
            label.setText("全部话题" if side == "source" else "默认位置")
            label.setToolTip("当前读取整个群的所有话题。" if side == "source" else "发送到目标的默认位置；话题群可通过“选择”指定话题。")

    def _peer_changed(self, side):
        if self._loading_draft:
            return
        previous = self._topic_ids[side]
        self._topic_ids[side] = 0
        self._topic_titles[side] = ""
        self._update_topic_label(side)
        if previous:
            self._feedback("聊天已修改，原话题选择已清除；如需指定话题，请重新点击“选择”。")

    def filter_settings(self):
        data = self.collect_draft()["filters"]
        return validate_filter_settings(FilterSettings(**{key: tuple(value) if isinstance(value, list) else value for key, value in data.items()}))

    def _save(self):
        self.filter_settings()
        draft = self.collect_draft()
        write_draft(self.config_path, draft)
        self.draft = draft
        self._refresh_application_settings(collapse=True)

    def _save_clicked(self):
        try:
            self._save()
            self._feedback("配置已保存到本机。手机号和验证码不会写入配置。")
            if self._application_ready:
                self._auth_feedback("应用设置已就绪。日常登录只需手机号和验证码，已有会话可直接恢复。")
            else:
                self._auth_feedback(self.application_status.text(), error=True)
        except Exception as exc:
            self._feedback(self._safe_error(exc), error=True)

    def _safe_error(self, exc):
        message = str(exc)
        if self.password.text():
            message = message.replace(self.password.text(), "[已隐藏]")
        if isinstance(exc, ConfigError) and ("API_ID" in message or "API_HASH" in message):
            return "首次应用设置尚未完成或无效。请展开“应用设置”，填写应用 ID 和应用密钥并保存；之后日常只需手机号和验证码。"
        for secret in (self.api_hash.text(), self.phone.text(), self.code.text()):
            if secret and len(secret) >= 4:
                message = message.replace(secret, "[已隐藏]")
        return message[:700] or f"操作失败（{type(exc).__name__}）"

    def _toggle_app_settings(self, expanded):
        self.app_settings_panel.setVisible(expanded)
        self.app_settings_toggle.setText("收起应用设置" if expanded else "展开应用设置")
        if not expanded:
            self.show_hash.setChecked(False)

    def _toggle_login_management(self, expanded):
        if not expanded and self._auth_challenge:
            self.login_management_button.setChecked(True)
            return
        self.login_details.setVisible(expanded)
        self.login_management_button.setText("收起登录" if expanded else "登录管理")

    def _show_login_management(self, expanded):
        self.login_management_button.setChecked(expanded)
        self._toggle_login_management(expanded)

    def _refresh_application_settings(self, *, collapse=False):
        """Show only readiness/source; software credentials never fill edits."""
        try:
            status = credentials_status(self.config_path, telegram=self.collect_draft().get("telegram", {}))
            ready = status.ready
            message = status.message
            source = status.source_label
        except (ValueError, TypeError):
            ready = False
            source = "尚未完成"
            message = "首次应用设置尚未完成或无效，请填写有效的应用 ID 和应用密钥后保存。"
        self._application_ready = ready
        self.application_status.setText(f"已就绪 · {source}。所有账号共用，换号无需重填。" if ready
                                       else "尚未配置应用设置。所有账号共用一套，完成首次设置后，换号无需重填。")
        self.environment_hint.setText(message if ready else "应用 ID 与应用密钥需配对填写，可通过右侧 Telegram 官方页面申请。")
        if collapse:
            self.app_settings_toggle.setChecked(not ready)
            self._toggle_app_settings(not ready)

    def _session_path(self):
        value = Path(self.fields["telegram.session"].text().strip()).expanduser()
        value = value if value.is_absolute() else self.config_path.parent / value
        if str(value).casefold().endswith(".session"):
            value = Path(str(value)[:-8] + ".session")
        else:
            value = Path(str(value) + ".session")
        return value.resolve()

    def _refresh_accounts(self):
        current = str(self._session_path())
        self.account_selector.clear()
        self.account_selector.addItem("当前会话", current)
        try:
            for account in self.account_store.list_accounts():
                self.account_selector.addItem(str(account["label"]), str(account["session"]))
            for index in range(1, self.account_selector.count()):
                if Path(self.account_selector.itemData(index)).resolve() == Path(current):
                    self.account_selector.setCurrentIndex(index)
                    break
        except Exception:
            self.log_view.appendPlainText("账号列表暂时无法读取；当前会话仍可使用，未删除任何登录记录。")

    def _account_change_allowed(self):
        if getattr(self.controller, "busy", False):
            self._auth_feedback("请先停止当前任务或登录操作，待停止后再切换或添加账号。", error=True)
            return False
        if self._auth_challenge:
            cancel = "取消本次登录" if self._password_pending else "取消本次验证码"
            self._auth_feedback(f"请先点击“{cancel}”，再切换或添加账号。", error=True)
            return False
        return True

    def _add_account(self):
        if not self._account_change_allowed():
            return
        self._new_account_mode = True
        self._auth_end_pending = False
        self._account_authenticated = False
        self._show_login_management(True)
        self.phone.clear()
        self.code.clear()
        self.password.clear()
        self.account_label.setText("正在添加新账号")
        self._auth_feedback("添加账号：输入新账号的国际手机号，再获取验证码。应用设置共用，原账号会话会保留。")
        self.phone.setFocus()

    def _switch_account(self):
        if not self._account_change_allowed():
            return
        selected = self.account_selector.currentData()
        if not selected:
            self._auth_feedback("请选择要恢复的用户账号。", error=True)
            return
        self._new_account_mode = False
        self._auth_end_pending = False
        self._account_authenticated = False
        self.phone.clear()
        self.code.clear()
        self.password.clear()
        self.fields["telegram.session"].setText(str(selected))
        self.account_label.setText("正在切换账号…")
        self._action("connect")

    def _pick_chat(self, side):
        if not self._account_authenticated:
            self._feedback("请先恢复已有会话或完成账号登录，再选择已加入的频道 / 群组。", error=True)
            return
        if not self._account_change_allowed():
            return
        if side == "target" and self.fields["sync.mode"].currentData() == "download_only":
            self._feedback("纯下载备份无需选择目标。")
            return
        if not callable(getattr(self.controller, "list_chats", None)):
            self._feedback("当前版本暂不支持聊天列表选择，请填写 @用户名、t.me 链接或聊天 ID。", error=True)
            return
        try:
            self._save()
            config = load_config(self.config_path, account_only=True)
            self._chat_selection_request = {"side": side, "session": os.path.normcase(str(self._session_path()))}
            self._feedback("正在读取当前账号已加入的频道和群组…")
            if not self.controller.list_chats(config):
                self._chat_selection_request = None
                raise ValueError("聊天列表读取未开始，请等待当前操作结束后重试。")
        except Exception as exc:
            self._chat_selection_request = None
            self._feedback(self._safe_error(exc), error=True)

    def _chats_ready(self, chats):
        request = self._chat_selection_request
        if request is None:
            return
        if request["session"] != os.path.normcase(str(self._session_path())):
            self._chat_selection_request = None
            self._feedback("当前会话已变化，请在选定账号后重新读取聊天列表。", error=True)
            return
        if not isinstance(chats, list) or not chats:
            self._chat_selection_request = None
            self._feedback("当前账号未读取到已加入的频道或群组。请先在 Telegram 官方客户端加入。", error=True)
            return
        self._pending_chat_choices = (request, chats)
        if not getattr(self.controller, "busy", False):
            QTimer.singleShot(0, self._show_pending_chats)

    def _show_pending_chats(self):
        pending = self._pending_chat_choices
        if pending is None or getattr(self.controller, "busy", False):
            return
        self._pending_chat_choices = None
        self._chat_selection_request = None
        request, chats = pending
        if self._auth_challenge or request["session"] != os.path.normcase(str(self._session_path())):
            self._feedback("登录状态或会话已变化，请重新选择频道 / 群组。", error=True)
            return
        dialog = ChatPickerDialog(chats, side=request["side"], parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dialog.selected_chat()
        if not isinstance(selected, dict):
            return
        identifier = selected.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier >= 0:
            self._feedback("未取得有效的频道 / 群组 ID，请重新选择。", error=True)
            return
        if request["session"] != os.path.normcase(str(self._session_path())):
            self._feedback("当前会话已变化，本次选择未写入，请重新读取列表。", error=True)
            return
        if selected.get("forum"):
            self._request_topics(request, selected, chats)
        else:
            self._commit_chat(request["side"], identifier, 0, "")

    def _commit_chat(self, side, identifier, topic_id, title):
        self.fields[f"sync.{side}"].setText(str(identifier))
        self._topic_ids[side] = topic_id
        self._topic_titles[side] = title if topic_id else ""
        self._update_topic_label(side)
        self._feedback("已选择聊天和话题范围；点击保存或开始时会保存此选择。")

    def _request_topics(self, request, chat, chats):
        if not self._account_authenticated or not self._account_change_allowed():
            return
        if not callable(getattr(self.controller, "list_topics", None)):
            self._feedback("当前版本暂不支持话题列表，请更新软件后重试；原选择已保留。", error=True)
            return
        side = request["side"]
        try:
            config = load_config(self.config_path, account_only=True)
            self._topic_selection_request = {
                **request, "chat": chat, "chats": chats,
                "parent_peer": self.fields[f"sync.{side}"].text().strip(),
            }
            self._feedback("正在读取所选群组的话题，确认后才会修改原选择…")
            if not self.controller.list_topics(config, chat["id"]):
                raise ValueError("话题读取未开始，请等待当前操作结束后重试；原选择已保留。")
        except Exception as exc:
            self._topic_selection_request = None
            self._feedback(self._safe_error(exc), error=True)

    def _topic_request_matches(self, request):
        return (self._account_authenticated and not self._auth_challenge
                and request["session"] == os.path.normcase(str(self._session_path()))
                and request["parent_peer"] == self.fields[f"sync.{request['side']}"].text().strip())

    def _topics_ready(self, payload):
        request = self._topic_selection_request
        if request is None:
            return
        if not self._topic_request_matches(request):
            self._topic_selection_request = None
            self._feedback("账号或父聊天已变化，本次话题结果未应用，请重新选择。", error=True)
            return
        chat_id = payload.get("chat_id") if isinstance(payload, dict) else None
        topics = payload.get("topics") if isinstance(payload, dict) else None
        if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id != request["chat"]["id"] or not isinstance(topics, list):
            self._topic_selection_request = None
            self._feedback("话题结果与所选群组不一致，原选择已保留，请重新读取。", error=True)
            return
        self._pending_topic_choices = (request, topics)
        if not getattr(self.controller, "busy", False):
            QTimer.singleShot(0, self._show_pending_topics)

    def _show_pending_topics(self):
        pending = self._pending_topic_choices
        if pending is None or getattr(self.controller, "busy", False):
            return
        self._pending_topic_choices = None
        self._topic_selection_request = None
        request, topics = pending
        if not self._topic_request_matches(request):
            self._feedback("账号或父聊天已变化，本次话题选择未应用，请重新选择。", error=True)
            return
        dialog = TopicPickerDialog(topics, side=request["side"], chat_title=request["chat"].get("title", "群组话题"), parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            if getattr(dialog, "return_to_chats", False) and self._topic_request_matches(request):
                self._pending_chat_choices = (request, request["chats"])
                QTimer.singleShot(0, self._show_pending_chats)
            return
        if not self._topic_request_matches(request):
            self._feedback("账号或父聊天已变化，本次话题选择未应用，请重新选择。", error=True)
            return
        selected = dialog.selected_topic()
        identifier = selected.get("id") if isinstance(selected, dict) else None
        if isinstance(identifier, bool) or not isinstance(identifier, int) or not 0 <= identifier <= 2147483647:
            self._feedback("请选择有效的群组话题或整个群。", error=True)
            return
        valid_ids = {0} | {item["id"] for item in topics if isinstance(item, dict) and isinstance(item.get("id"), int) and not isinstance(item["id"], bool)}
        if identifier not in valid_ids:
            self._feedback("话题已变化，请重新读取后选择。", error=True)
            return
        self._commit_chat(request["side"], request["chat"]["id"], identifier, str(selected.get("title") or ""))

    def _action(self, action):
        task_action = action in {"run", "check", "preview"}
        if task_action and getattr(self.controller, "busy", False):
            message = "已有任务或登录操作正在进行，请等待完成，或点击“安全停止”后再开始。"
            self._feedback(message, error=True)
            self.log_view.appendPlainText("[未启动新任务] " + message)
            return
        if task_action:
            self._begin_task(action)
        if action in {"run", "check", "preview"} and (self._new_account_mode or self._auth_challenge):
            message = "请先完成新账号登录，或取消验证码并从列表切回已有账号，再开始检查、预览或搬运。"
            self._feedback(message, error=True)
            self._auth_feedback(message, error=True)
            self._task_phase_event({"phase": "failed", "message": message})
            self._task_operation = None
            return
        account_action = action in {"connect", "code", "login", "password"}
        if account_action and getattr(self.controller, "busy", False):
            kept = "密码" if action == "password" else "验证码"
            message = f"已有操作正在进行，请等待完成，或点击“停止当前操作”。{kept}已保留。"
            self._feedback(message, error=True)
            self._auth_feedback(message, error=True)
            return
        try:
            if account_action:
                self._auth_operation = action
            if action == "password":
                if not self._password_pending or not self._auth_challenge or self._challenge_identity != self._auth_identity():
                    raise ValueError("请先用手机号和验证码继续登录；仅在账号要求时输入两步验证密码。")
                password = self.password.text()
                if not password:
                    raise ValueError("请输入该 Telegram 账号的两步验证密码。")
                self._auth_feedback("正在验证两步验证密码，请稍候；可点击“停止当前操作”结束等待。")
                if self.controller.submit_password(password):
                    self.password.clear()
                else:
                    self._auth_operation = None
                    self._auth_feedback("密码未提交：已有操作正在进行或程序正在关闭。输入已保留。", error=True)
                return
            if action == "login":
                if self._password_pending:
                    raise ValueError("当前账号需要两步验证密码，请在密码输入框继续，或取消本次登录。")
                if not self._auth_challenge or self._challenge_identity != self._auth_identity():
                    raise ValueError("请先发送验证码；更改应用凭证、会话或手机号后需要重新获取。")
                code = normalize_code(self.code.text())
                self._auth_feedback("正在验证 Telegram 验证码，请稍候；可点击“停止当前操作”结束等待。")
                if self.controller.submit_code(code):
                    self.code.clear()
                else:
                    self._auth_operation = None
                    self._auth_feedback("验证码未提交：已有操作正在进行或程序正在关闭。验证码已保留。", error=True)
                return
            if action == "code":
                if self._password_pending:
                    raise ValueError("验证码已经通过，请在第2步输入两步验证密码；如需重新登录，可取消本次登录。")
                self._refresh_resend()
                if self._auth_challenge:
                    raise ValueError("已有验证码等待验证；未收到请使用“重新获取验证码”，或取消本次登录后重新申请。")
                if not self.code_button.isEnabled():
                    raise ValueError("Telegram 要求等待后再申请验证码，请查看账号区域的倒计时。")
                self.phone.setText(normalize_phone(self.phone.text()))
                if self._new_account_mode:
                    self.fields["telegram.session"].setText(str(self.account_store.session_for_phone(self.phone.text())))
                    self.fields["telegram.session"].setText(str(self._session_path()))
                self._auth_feedback("正在准备连接 Telegram 并获取验证码，请稍候；可点击“停止当前操作”。")
            elif action == "connect":
                self._auth_feedback("正在恢复 Telegram 用户会话，请稍候；可点击“停止当前操作”。")
            self._save()
            config = load_config(self.config_path, account_only=action in {"connect", "code", "preview"})
            if action not in {"connect", "code"}:
                if str(config.source) in {"", "@source_channel"}:
                    raise ValueError("请填写实际的源频道或群组。")
                if action != "preview" and config.mode != "download_only" and str(config.target) in {"", "@target_channel"}:
                    raise ValueError("请填写实际的目标频道或群组。")
            if action == "connect":
                if not self.controller.connect_account(config):
                    raise ValueError("连接未开始：请等待当前操作结束，或点击“停止当前操作”。")
            elif action == "code":
                if not self.controller.request_code(config, self.phone.text()):
                    raise ValueError("验证码请求未开始：请等待当前操作结束，或点击“停止当前操作”。")
                self.code.clear()
            elif action == "preview":
                self.navigation.setCurrentRow(1)
                if not self.controller.preview(config, limit=50):
                    raise ValueError("预览未开始：当前操作尚未结束或程序正在关闭。")
            elif action == "check":
                if not self.controller.run_task(config, once=True, check_only=True):
                    raise ValueError("检查未开始：当前操作尚未结束或程序正在关闭。")
            elif action == "run":
                for label in self.stat_labels.values():
                    label.setText("0")
                if not self.controller.run_task(config, once=self.once.isChecked(), check_only=False):
                    raise ValueError("搬运未开始：当前操作尚未结束或程序正在关闭。")
        except Exception as exc:
            message = self._safe_error(exc)
            self._feedback(message, error=True)
            if task_action:
                self._task_phase_event({"phase": "failed", "message": message})
                self._task_operation = None
            if account_action:
                self._auth_operation = None
                self._auth_feedback(message, error=True)

    def _mode_changed(self, *_):
        download = self.fields["sync.mode"].currentData() == "download_only"
        self.fields["sync.target"].setEnabled(not download)
        self.choose_target_button.setEnabled(not download and not getattr(self.controller, "busy", False) and not self._auth_challenge)
        self.target_topic_label.setEnabled(not download)
        self.auto_cleanup.setEnabled(self.fields["sync.mode"].currentData() == "download_then_forward" and not getattr(self.controller, "busy", False))
        self.fields["sync.comment_mode"].setEnabled(not download and not getattr(self.controller, "busy", False))
        if download:
            self.fields["sync.comment_mode"].setCurrentIndex(self.fields["sync.comment_mode"].findData("download_only"))

    def _feedback(self, text, error=False):
        self.feedback.setText(str(text))
        self.feedback.setStyleSheet(f"padding:2px;color:{'#b63145' if error else '#345c86'};")
        self.feedback.setVisible(error and str(text) != self._task_last_message)
        self.statusBar().showMessage(str(text))

    def _auth_feedback(self, text, error=False):
        self.auth_status.setText(str(text))
        self.auth_status.setStyleSheet(
            "padding:6px;border-radius:6px;" +
            ("background:#fff1f2;color:#b63145;" if error else "background:#edf4ff;color:#345c86;")
        )

    def _worker_status(self, text):
        if self._task_operation is not None:
            self._last_worker_task_status = str(text)
            if self._task_terminal_seen:
                self._feedback(self._task_last_message, error=self._task_phase_key in {"failed", "error", "uncertain"})
                return
            if self._task_phase_key == "starting":
                self.task_status.setText(str(text))
        self._feedback(text)
        if self._auth_operation is not None or self._auth_challenge or self._auth_end_pending:
            self._auth_feedback(text)

    def _worker_failed(self, text):
        if self._auth_operation == "login":
            self.code.clear()
        self._resend_request_pending = False
        if self._chat_selection_request is not None:
            self._chat_selection_request = None
            self._pending_chat_choices = None
        self._topic_selection_request = None
        self._pending_topic_choices = None
        if self._pending_request is not None:
            self._pending_request = None
            if self._pending_dialog is not None:
                self._pending_dialog.acknowledge.setChecked(False)
                self._pending_dialog.message.setText(str(text))
                self._pending_dialog.set_busy(bool(getattr(self.controller, "busy", False)))
        if self._task_operation is not None or self._task_active:
            if not (self._task_terminal_seen and self._task_phase_key in {"failed", "error", "uncertain"}):
                self._task_phase_event({"phase": "failed", "message": str(text)})
            elif str(text) not in self._task_last_message:
                self.log_view.appendPlainText("[错误] " + str(text))
        self._feedback(self._task_last_message if self._task_terminal_seen and self._task_phase_key == "uncertain" else text, error=True)
        if self._auth_operation is not None or self._auth_challenge or self._auth_end_pending:
            self._auth_feedback(text, error=True)
        self._auth_end_pending = False
        self._auth_operation = None
        self._refresh_resend()

    def _authenticated(self, data):
        self._challenge_ended()
        self._clear_code_delivery()
        self._auth_end_pending = False
        name = data.get("name") or data.get("username") or str(data.get("id", ""))
        self.account_label.setText(f"已连接 · {name}")
        self._auth_operation = None
        self._new_account_mode = False
        self._account_authenticated = True
        self._refresh_application_settings(collapse=True)
        self._show_login_management(False)
        self._auth_feedback("用户账号已登录，会话已保存在本机；下次可以恢复已有会话。")
        self._feedback("用户会话已就绪。建议先预览广告规则，再开始搬运。")
        try:
            self.account_store.remember(data["id"], name, self._session_path())
            self._refresh_accounts()
        except Exception:
            message = "用户账号已登录，但账号列表暂时无法保存。当前会话仍可使用；请检查目录权限后重试。"
            self._auth_feedback(message, error=True)
            self._feedback(message, error=True)
            self.log_view.appendPlainText(message)

    def _apply_auth_step(self):
        pending = self._password_pending
        for widget in (self.phone_label, self.phone, self.code_button, self.code_label,
                       self.code, self.login_button, self.application_status, self.app_settings_toggle):
            widget.setVisible(not pending)
        self.app_settings_panel.setVisible(not pending and self.app_settings_toggle.isChecked())
        if pending and not self._password_cancel_embedded:
            self._login_form.removeWidget(self.connect_button)
            self._password_actions.addWidget(self.connect_button)
            self._password_cancel_embedded = True
        elif not pending and self._password_cancel_embedded:
            self._password_actions.removeWidget(self.connect_button)
            self._login_form.addWidget(self.connect_button, 2, 4)
            self._password_cancel_embedded = False
        self.password_row.setVisible(pending)
        self.connect_button.setVisible(True)

    def _challenge_ended(self):
        """Release expired/exhausted challenges without hiding the failure text."""
        self._auth_end_pending = (self._auth_end_pending or self._auth_challenge
                                  or self._auth_operation in {"code", "login", "password", "resend"})
        self._auth_challenge = False
        self._challenge_identity = None
        self._password_pending = False
        self._resend_request_pending = False
        self.code.clear()
        self.password.clear()
        self.connect_button.setText("恢复已有会话")
        self._busy(bool(getattr(self.controller, "busy", False)))

    def _clear_code_delivery(self):
        self._code_delivery_state = None
        self._code_delivery_identity = None
        self._resend_deadline = 0.0
        self._resend_request_pending = False
        self._resend_timer.stop()
        self._refresh_resend()

    def _code_delivery(self, state):
        if not isinstance(state, dict):
            return
        self._code_delivery_state = dict(state)
        self._code_delivery_identity = self._auth_identity()
        delay = state.get("resend_after", 0)
        valid_delay = isinstance(delay, (int, float)) and not isinstance(delay, bool) and math.isfinite(delay) and delay >= 0
        if not valid_delay:
            delay = 0
            self._code_delivery_state["resend_available"] = False
        self._resend_deadline = time.monotonic() + float(delay)
        self._refresh_resend()

    def _refresh_resend(self, *_):
        busy = bool(getattr(self.controller, "busy", False))
        state = self._code_delivery_state if self._code_delivery_identity == self._auth_identity() else None
        remaining = max(0, math.ceil(self._resend_deadline - time.monotonic())) if state is not None else 0
        available = state is not None and state.get("resend_available", state.get("can_resend", False)) is True
        challenge = self._auth_challenge and not self._password_pending
        self.resend_button.setVisible(challenge)
        enabled = (challenge and self._challenge_identity == self._auth_identity() and not busy
                   and not self._resend_request_pending and remaining == 0 and available
                   and callable(getattr(self.controller, "resend_code", None)))
        self.resend_button.setEnabled(enabled)
        if (busy and self._auth_operation == "resend") or self._resend_request_pending:
            self.resend_button.setText("正在重新获取…")
        else:
            self.resend_button.setText(f"重新获取（{remaining}秒）" if remaining else "重新获取验证码")
        self.resend_button.setToolTip("按 Telegram 服务端的等待时间和下一发送渠道重新申请，不保证改用短信。")
        self.code_button.setEnabled(not busy and not self._auth_challenge and remaining == 0)
        if busy and self._auth_operation == "code":
            self.code_button.setText("正在获取…")
        else:
            self.code_button.setText(f"发送验证码（{remaining}秒）" if remaining and not self._auth_challenge else "发送验证码")
        parts = []
        if state is not None and not self._password_pending:
            if state.get("delivery"):
                parts.append("发送渠道：" + str(state["delivery"]))
            if state.get("next_delivery"):
                parts.append("下一渠道：" + str(state["next_delivery"]))
            if remaining:
                parts.append(f"请等待 {remaining} 秒后再申请")
            elif not self._auth_challenge:
                parts.append("本次登录已结束，可点击“发送验证码”重新申请")
            elif not available:
                parts.append("当前没有可用的下一渠道，可取消本次登录后重新申请")
            else:
                parts.append("未收到时可点击“重新获取验证码”")
        self.code_delivery_label.setText("；".join(parts))
        self.code_delivery_label.setToolTip(str(state.get("message") or "") if state is not None and not self._password_pending else "")
        self.code_delivery_label.setVisible(bool(parts) and not self._password_pending)
        if remaining and not self._password_pending:
            if not self._resend_timer.isActive():
                self._resend_timer.start()
        else:
            self._resend_timer.stop()

    def _resend_code(self):
        if self._password_pending:
            self._auth_feedback("验证码已经通过，请在第2步输入两步验证密码；如需重新登录，可取消本次登录。", error=True)
            return
        if getattr(self.controller, "busy", False):
            self._auth_feedback("已有操作正在进行，验证码输入已保留；请稍候或先停止当前操作。", error=True)
            return
        self._refresh_resend()
        if not self.resend_button.isEnabled():
            self._auth_feedback("当前不能重新获取验证码，请查看发送渠道和等待提示；也可取消本次登录后重新申请。", error=True)
            return
        try:
            self._save()
            config = load_config(self.config_path, account_only=True)
            self._auth_operation = "resend"
            self._auth_feedback("正在按 Telegram 提供的下一渠道重新申请验证码，请稍候。不会自动循环重发。")
            self._resend_request_pending = True
            if self.controller.resend_code(config, self.phone.text()):
                self.code.clear()
                self._refresh_resend()
            else:
                self._resend_request_pending = False
                self._auth_operation = None
                self._refresh_resend()
                self._auth_feedback("重新获取未开始；当前输入已保留，请等待已有操作结束后重试。", error=True)
        except Exception as exc:
            self._resend_request_pending = False
            self._auth_operation = None
            self._refresh_resend()
            self._auth_feedback(self._safe_error(exc), error=True)

    def _password_required(self, text):
        self._auth_end_pending = False
        self._resend_request_pending = False
        self._password_pending = True
        self._auth_challenge = True
        self._show_login_management(True)
        if self._challenge_identity is None:
            self._challenge_identity = self._auth_identity()
        self.code.clear()
        self.password.clear()
        self._auth_operation = None
        self._busy(bool(getattr(self.controller, "busy", False)))
        detail = text or "请在下方输入两步验证密码，并回车或点击“验证密码”。密码不会保存。"
        self._auth_feedback(detail if "验证码已通过" in detail else "验证码已通过。" + detail)
        self.password.setFocus()

    def _code_sent(self, text):
        self._auth_end_pending = False
        self._resend_request_pending = False
        self.code.clear()
        self._auth_challenge = True
        self._show_login_management(True)
        self._challenge_identity = self._auth_identity()
        self.connect_button.setText("取消本次验证码")
        self._apply_challenge_lock()
        self._refresh_resend()
        self._feedback(text or "验证码已请求，请查看 Telegram 官方客户端或短信。")
        self._auth_feedback(text or "验证码已请求，请查看 Telegram 官方客户端或短信，然后输入验证码并回车或点击“验证并登录”。")
        self.code.setFocus()

    def _auth_identity(self):
        return (self.api_id.text(), self.api_hash.text(), self.fields["telegram.session"].text(), self.phone.text())

    def _apply_challenge_lock(self):
        if self._auth_challenge:
            for widget in (self.api_id, self.api_hash, self.fields["telegram.session"], self.phone, self.code_button,
                           self.account_selector, self.switch_account_button, self.add_account_button,
                           self.app_settings_toggle, self.save_app_settings_button, self.config_button,
                           self.login_management_button, self.choose_source_button, self.choose_target_button,
                           self.cleanup_button, self.pending_button):
                widget.setEnabled(False)

    def _connect_or_cancel(self):
        if self._auth_challenge:
            if getattr(self.controller, "busy", False):
                self._auth_feedback("请等待当前操作结束，或先点击“停止当前操作”。当前输入已保留。", error=True)
                return
            self._auth_operation = "cancel"
            if not self.controller.cancel_authentication():
                self._auth_operation = None
                self._auth_feedback("当前无法取消登录，请等待当前操作结束或先停止操作。输入已保留。", error=True)
                return
            self._challenge_ended()
            self._auth_end_pending = False
            self._auth_feedback("已请求取消本次登录，当前输入已清除；可点击“发送验证码”重新申请。如显示等待，请在倒计时结束后操作。")
            self._feedback("已请求取消本次登录。旧账号会话文件保留。")
        else:
            if self._new_account_mode:
                self._auth_feedback("添加账号时请先输入新手机号并获取验证码；恢复旧账号请从账号列表选择后点击“切换账号”。", error=True)
            else:
                self._action("connect")

    def _stats(self, values):
        for key, label in self.stat_labels.items():
            if key in values:
                label.setText(str(values[key]))
                value = values[key]
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    self._task_counts[key] = value
        if self._task_operation is not None or self._task_terminal_seen:
            self._update_task_detail()

    def _begin_task(self, action):
        self._task_operation = action
        self._task_active = True
        self._task_terminal_seen = False
        self._task_phase_key = "starting"
        self._task_reason = ""
        self._task_last_message = ""
        self._last_worker_task_status = ""
        self._last_task_log_key = None
        if action != "cleanup":
            self._task_counts = dict.fromkeys(self._task_counts, 0)
            for label in self.stat_labels.values():
                label.setText("0")
        names = {"run": "搬运任务", "check": "连接与评论区检查", "preview": "源消息预览", "cleanup": "已转发文件清理"}
        name = names[action]
        self.log_view.clear()
        self.log_view.appendPlainText(f"========== {datetime.now():%Y-%m-%d %H:%M:%S} · 本次{name} ==========")
        message = ("已开始本地清理 · 正在核对已确认成功转发的文件记录…" if action == "cleanup"
                   else f"已开始{name} · 正在检查登录状态与任务配置…")
        self.log_view.appendPlainText("[开始] " + message)
        self.task_status.setText(message)
        self.task_status.setStyleSheet("font-weight:600;color:#245fc8;")
        self.task_detail.setText("")
        self.task_detail.setVisible(False)
        self.task_progress.setVisible(True)
        self.task_progress.setRange(0, 0)
        self._feedback(message)

    def _update_task_detail(self):
        self.task_detail.setText(self._task_reason)
        self.task_detail.setVisible(bool(self._task_reason))

    def _task_phase_event(self, values):
        if not isinstance(values, dict):
            return
        phase = str(values.get("phase") or "working")
        message = str(values.get("message") or "正在处理任务…")
        if self._task_operation is not None and self._task_terminal_seen and self._task_phase_key == "uncertain" and phase in {"failed", "error"}:
            if message not in self._task_last_message:
                self.log_view.appendPlainText("[错误] " + message)
            self._feedback(self._task_last_message, error=True)
            return
        reason = str(values.get("reason") or "")
        reason = _TASK_REASON_LABELS.get(reason, reason)
        terminal = phase in {"completed", "failed", "error", "uncertain", "stopped", "cancelled", "checked", "chats", "topics", "cleaned"}
        self._task_phase_key = phase
        self._task_reason = reason
        self._task_last_message = message + ("\n" + reason if reason and reason != message else "")
        counts = values.get("counts")
        if isinstance(counts, dict) and phase not in {"loading_chats", "chats", "loading_topics", "topics", "cleanup", "cleaned"}:
            self._stats(counts)
        self.task_status.setText(message)
        self._update_task_detail()
        self._task_active = not terminal
        self._task_terminal_seen = terminal
        if terminal:
            self.task_progress.setVisible(False)
            self.task_progress.setRange(0, 100)
            self.task_progress.setValue(100 if phase in {"completed", "checked", "chats", "topics", "cleaned"} else 0)
            color = "#b63145" if phase in {"failed", "error", "uncertain"} else "#18764d" if phase in {"completed", "checked", "chats", "topics", "cleaned"} else "#7c641c"
            self.task_status.setStyleSheet(f"font-weight:600;color:{color};")
        else:
            self.task_progress.setVisible(True)
            self.task_progress.setRange(0, 0)
            self.task_status.setStyleSheet("font-weight:600;color:#245fc8;")
        # Percentages can update very frequently. Show every message in the
        # card, while keeping one log entry per phase/message unit and terminal.
        log_key = (phase, values.get("scope"), values.get("source_id"))
        if terminal or log_key != self._last_task_log_key:
            self.log_view.appendPlainText(f"[{_TASK_PHASE_LABELS.get(phase, '任务进度')}] {message}")
            if reason and reason != message:
                self.log_view.appendPlainText("[原因] " + reason)
            self._last_task_log_key = log_key
        self._feedback(self._task_last_message, error=phase in {"failed", "error", "uncertain"})

    def _busy(self, busy):
        self._apply_auth_step()
        for widget in (self.connect_button, self.code_button, self.login_button, self.resend_button, self.start_button,
                       self.check_button, self.task_preview_button, self.preview_button,
                       self.save_button, self.config_button, self.block_selected_button,
                       self.save_app_settings_button, self.app_settings_toggle,
                       self.account_selector, self.switch_account_button, self.add_account_button,
                       self.login_management_button, self.paths_toggle,
                       self.choose_source_button, self.choose_target_button, self.choose_download_button,
                       self.open_download_button, self.cleanup_button, self.pending_button):
            widget.setEnabled(not busy)
        # Changes apply to the next operation, never mutate a running task.
        for field in self.fields.values():
            field.setEnabled(not busy)
        self.phone.setEnabled(not busy)
        self.code.setEnabled(not busy)
        self.password.setEnabled(not busy and self._password_pending)
        self.password_button.setEnabled(not busy and self._password_pending)
        if self._password_pending:
            self.code.setEnabled(False)
            self.login_button.setEnabled(False)
        self.once.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.auth_stop_button.setEnabled(busy)
        side = (self._topic_selection_request or self._chat_selection_request or {}).get("side")
        self.choose_source_button.setText("读取中" if busy and side == "source" else "选择")
        self.choose_target_button.setText("读取中" if busy and side == "target" else "选择")
        self.start_button.setText("搬运中…" if busy and self._task_operation == "run" else "开始搬运")
        self.cleanup_button.setText("正在清理…" if busy and self._task_operation == "cleanup" else "一键清理已转发文件")
        self.check_button.setText("检查中…" if busy and self._task_operation == "check" else "检查连接与评论区")
        self.task_preview_button.setText("预览中…" if busy and self._task_operation == "preview" else "预览最近消息")
        self.preview_button.setText("预览中…" if busy and self._task_operation == "preview" else "预览最近 50 条")
        self.code_button.setText("正在获取…" if busy and self._auth_operation == "code" else "发送验证码")
        self.login_button.setText("正在验证…" if busy and self._auth_operation == "login" else "验证并登录")
        self.password_button.setText("正在验证…" if busy and self._auth_operation == "password" else "验证密码")
        cancel = "取消本次登录" if self._password_pending else "取消本次验证码"
        self.connect_button.setText("正在连接…" if busy and self._auth_operation == "connect" else (cancel if self._auth_challenge else "恢复已有会话"))
        if not busy:
            self._mode_changed()
            self._apply_challenge_lock()
            if self._auth_challenge:
                (self.password if self._password_pending else self.code).setFocus()
            if self._pending_chat_choices is not None:
                QTimer.singleShot(0, self._show_pending_chats)
            if self._pending_topic_choices is not None:
                QTimer.singleShot(0, self._show_pending_topics)
        self._refresh_resend()
        if self._pending_dialog is not None:
            self._pending_dialog.set_busy(busy)
        if not busy and self._pending_refresh_after_resolve:
            QTimer.singleShot(0, self._refresh_pending_after_resolve)

    def _stop(self):
        self.controller.stop()
        self._feedback("已请求安全停止；正在发送的媒体组会先完成并保存映射。")
        if self._auth_operation is not None:
            self._auth_feedback("已请求停止当前登录操作，正在结束网络等待；请稍候。")
        if self._task_operation is not None and not self._task_terminal_seen:
            self._task_phase_event({"phase": "stopping", "message": "正在安全停止 · 等待当前操作或媒体组完成并保存进度。"})

    def _finished(self, result):
        if result == "pending_resolved":
            self._pending_request = None
            self._pending_refresh_after_resolve = True
            if self._pending_dialog is not None:
                self._pending_dialog.acknowledge.setChecked(False)
                self._pending_dialog.message.setText("已解除所选批次的本地阻塞；没有发送内容。正在刷新记录…")
            if not getattr(self.controller, "busy", False):
                QTimer.singleShot(0, self._refresh_pending_after_resolve)
            return
        if result == "connected" and not self._account_authenticated:
            self._show_login_management(True)
        if self._auth_operation is not None or self._auth_end_pending:
            if result == "stopped":
                self._auth_feedback("登录操作已停止；可检查手机号和网络后手动重试。")
            elif result == "auth_cancelled":
                self._auth_feedback("已取消本次登录。可以选择其他账号或重新获取验证码。")
            self._auth_operation = None
            self._auth_end_pending = False
        if result == "closed":
            self._closed = True
            if self._close_requested:
                QTimer.singleShot(0, self.close)
            return
        if self._task_operation is not None:
            if not self._task_terminal_seen:
                if self._task_operation == "cleanup" and result in {"cleanup", "cleaned", "completed"}:
                    self._task_phase_event({"phase": "cleaned", "message": "本地清理已结束；具体文件结果请查看本次日志。"})
                elif result == "completed":
                    reason = "本次处理 0 条；后台未提供具体扫描原因，请查看本次日志。" if self._task_counts["processed"] == 0 else "已完成消息映射与进度保存。"
                    self._task_phase_event({"phase": "completed", "message": "本次搬运已结束。", "reason": reason})
                elif result in {"stopped", "failed"}:
                    self._task_phase_event({"phase": result, "message": self._last_worker_task_status or ("任务已安全停止，已完成进度保留。" if result == "stopped" else "任务未完成，请查看本次日志中的错误。")})
                elif result == "checked":
                    self._task_phase_event({"phase": "checked", "message": "连接与评论区检查通过，未发送内容。"})
                elif result == "preview":
                    self._task_phase_event({"phase": "completed", "message": f"预览完成 · 已读取 {self.preview_table.rowCount()} 条消息，未发送或下载。"})
            if self._task_terminal_seen:
                self._feedback(self._task_last_message, error=self._task_phase_key in {"failed", "error", "uncertain"})
            self._task_operation = None
            if result in {"completed", "stopped"}:
                self._history()
            return

    def _sample(self):
        try:
            settings = self.filter_settings()
            message = types.Message(id=1, peer_id=types.PeerChannel(1), message=self.sample_text.toPlainText())
            decision = AdFilter(settings).evaluate([message], scope="post", mode="reupload")
            if decision.blocked:
                self.sample_result.setText("将被屏蔽：" + "；".join(decision.reasons))
                self.sample_result.setStyleSheet("color:#b53445;")
            else:
                self.sample_result.setText("未命中屏蔽规则（不代表一定没有广告）。")
                self.sample_result.setStyleSheet("color:#18764d;")
        except Exception as exc:
            self.sample_result.setText(self._safe_error(exc))
            self.sample_result.setStyleSheet("color:#b53445;")

    def _preview_ready(self, rows):
        self.preview_rows = list(rows)
        self.preview_table.setRowCount(len(rows))
        for index, data in enumerate(rows):
            text = str(data.get("text", "") or "（无文字；请人工核对媒体内容）")
            reasons = data.get("reasons", [])
            values = [str(data.get("source_id", "")), text[:180].replace("\n", " "),
                      str(data.get("media", "文字")), "屏蔽" if data.get("blocked") else "通过",
                      "；".join(reasons) if not isinstance(reasons, str) else reasons]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(text if column == 1 else value)
                item.setBackground(QColor("#fff1f2" if data.get("blocked") else "#f1faf5"))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, data)
                self.preview_table.setItem(index, column, item)
        self._feedback(f"已只读预览 {len(rows)} 条消息，尚未发送。红色为整组拦截，绿色为未命中规则。")

    def _block_selected(self):
        selected = {index.row() for index in self.preview_table.selectionModel().selectedRows()}
        if not selected:
            self._feedback("请先在预览中选中要屏蔽的消息。", error=True)
            return
        groups = {self.preview_rows[index].get("grouped_id") for index in selected if self.preview_rows[index].get("grouped_id")}
        selected.update(index for index, row in enumerate(self.preview_rows) if row.get("grouped_id") in groups)
        keys = lines(self.fields["filters.blocked_message_keys"])
        for index in sorted(selected):
            row = self.preview_rows[index]
            key = f"{row['chat_id']}:{row['source_id']}"
            if key not in keys:
                keys.append(key)
            row["blocked"], row["reasons"] = True, ["手动屏蔽消息 / 相册"]
        self.fields["filters.blocked_message_keys"].setPlainText("\n".join(keys))
        try:
            self._save()
            self._preview_ready(self.preview_rows)
            self._feedback(f"已将 {len(selected)} 条消息加入当前源的手动屏蔽名单。")
        except Exception as exc:
            self._feedback(self._safe_error(exc), error=True)

    def _choose_config(self):
        if not self._account_change_allowed():
            return
        selected, _ = QFileDialog.getOpenFileName(self, "选择配置文件", str(self.config_path.parent), "TOML 配置 (*.toml)")
        if selected:
            self._new_account_mode = False
            self._account_authenticated = False
            self._show_login_management(True)
            self.phone.clear()
            self.code.clear()
            self.config_path = Path(selected).resolve()
            self._load()
            self._feedback("已载入配置；若切换应用凭证或会话，请重新连接账号。")

    def _database(self):
        value = Path(str(self.collect_draft().get("storage", {}).get("database", "data/state.sqlite3")))
        return value if value.is_absolute() else self.config_path.parent / value

    def _readonly_database(self):
        path = self._database().resolve()
        if not path.is_file():
            return None
        return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)

    def _history(self):
        try:
            connection = self._readonly_database()
            if connection is None:
                self.history_table.setRowCount(0)
                return
            try:
                rows = connection.execute("SELECT source_id,text,scope,metadata FROM messages WHERE status='filtered_ad' ORDER BY updated_at DESC,source_id DESC LIMIT 200").fetchall()
            finally:
                connection.close()
            self.history_table.setRowCount(len(rows))
            for index, (identifier, text, scope, metadata) in enumerate(rows):
                reasons = json.loads(metadata).get("ad_filter", {}).get("reasons", [])
                for column, value in enumerate([str(identifier), text[:200].replace("\n", " "), "评论" if scope == "comment" else "正文", "；".join(reasons)]):
                    item = QTableWidgetItem(value)
                    item.setToolTip(text if column == 1 else value)
                    self.history_table.setItem(index, column, item)
        except (OSError, ValueError, sqlite3.Error):
            self._feedback("暂时无法读取拦截记录；可在当前任务完成后刷新。", error=True)

    def _limit_status(self):
        try:
            connection = self._readonly_database()
            if connection is None:
                self.limit_status.setPlainText("尚无本机账号记录。连接并运行后会保存发送尝试与冷却。")
                return
            try:
                rows = connection.execute("SELECT account_id,global_cooldown_until,stopped_reason FROM limit_accounts").fetchall()
                output = []
                import time
                now = time.time()
                for identifier, until, reason in rows:
                    counts = [connection.execute("SELECT COALESCE(SUM(cost),0) FROM limit_events WHERE account_id=? AND created_at>?", (identifier, now - seconds)).fetchone()[0] for seconds in (60, 3600, 86400)]
                    output.append(f"账号 {identifier}\n最近60秒 / 1小时 / 24小时尝试：{counts[0]} / {counts[1]} / {counts[2]} 条\n账号冷却剩余：{max(0, until-now):.0f} 秒\n停止原因：{reason or '未设置停止'}")
                self.limit_status.setPlainText("\n\n".join(output) or "尚无本机账号记录。")
            finally:
                connection.close()
        except (OSError, ValueError, sqlite3.Error):
            self.limit_status.setPlainText("暂时无法读取；可在任务完成后刷新。")

    def _open_data(self):
        path = self._database().parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path if path.exists() else self.config_path.parent)))

    def _open_pending(self):
        if not self._account_change_allowed():
            return
        if not callable(getattr(self.controller, "list_pending", None)):
            self._feedback("当前版本暂不支持查看待核对发送，请更新软件后重试。", error=True)
            return
        if self._pending_dialog is None:
            self._pending_dialog = PendingOperationsDialog(self)
            self._pending_dialog.refresh_button.clicked.connect(self._refresh_pending)
            self._pending_dialog.resolve_button.clicked.connect(self._resolve_pending)
        self._pending_dialog.open()
        self._refresh_pending()

    def _pending_config(self):
        self._save()
        return load_config(self.config_path, account_only=True)

    def _refresh_pending(self, *_):
        dialog = self._pending_dialog
        if dialog is None:
            return
        if getattr(self.controller, "busy", False) or self._pending_request is not None:
            dialog.message.setText("已有操作正在进行，请完成后再刷新本地记录。")
            return
        try:
            config = self._pending_config()
            context = {"path": str(self.config_path), "database": str(config.database)}
            self._pending_request = {"kind": "list", **context}
            dialog.acknowledge.setChecked(False)
            dialog.set_busy(True)
            dialog.message.setText("正在读取本地待核对记录，不连接 Telegram…")
            if not self.controller.list_pending(config):
                raise ValueError("待核对记录读取未开始，请等待当前操作结束后重试。")
        except Exception as exc:
            self._pending_request = None
            dialog.set_busy(bool(getattr(self.controller, "busy", False)))
            dialog.message.setText(self._safe_error(exc))

    def _pending_ready(self, rows):
        request = self._pending_request
        if request is None or request.get("kind") != "list" or self._pending_dialog is None:
            return
        self._pending_request = None
        dialog = self._pending_dialog
        if request["path"] != str(self.config_path):
            dialog.message.setText("配置已变化，请重新刷新待核对记录。")
        elif not isinstance(rows, list):
            dialog.message.setText("未收到有效的本地记录，请重新刷新。")
        else:
            records = [dict(row) for row in rows if isinstance(row, dict)]
            self._pending_context = {"path": request["path"], "database": request["database"]}
            dialog.set_rows(records)
            dialog.message.setText(f"共 {len(records)} 批待核对记录；请先核对目标，再选择一整批并勾选说明。" if records else "没有待核对发送记录。解除本地阻塞不会立即发送消息。")
        dialog.set_busy(bool(getattr(self.controller, "busy", False)))

    def _resolve_pending(self, *_):
        dialog = self._pending_dialog
        if dialog is None:
            return
        operation = dialog.selected_operation()
        if (getattr(self.controller, "busy", False) or self._pending_request is not None
                or not dialog.acknowledge.isChecked() or operation is None):
            dialog.message.setText("请等待当前操作结束，选择一整批，并勾选已核对目标的说明。")
            return
        if not callable(getattr(self.controller, "resolve_pending", None)):
            dialog.message.setText("当前版本暂不支持解除本地阻塞，请更新软件。")
            return
        try:
            config = self._pending_config()
            context = {"path": str(self.config_path), "database": str(config.database)}
            if context != self._pending_context:
                raise ValueError("配置或数据库位置已变化；请重新刷新后核对，原记录未修改。")
            self._pending_request = {"kind": "resolve", **context}
            dialog.acknowledge.setChecked(False)
            dialog.set_busy(True)
            dialog.message.setText("正在解除所选整批的本地阻塞，不删除目标消息，也不发送内容…")
            if not self.controller.resolve_pending(config, operation["route"], operation["key"], operation["scope"]):
                raise ValueError("解除操作未开始，本地记录保留，请稍后重试。")
        except Exception as exc:
            self._pending_request = None
            dialog.acknowledge.setChecked(False)
            dialog.set_busy(bool(getattr(self.controller, "busy", False)))
            dialog.message.setText(self._safe_error(exc))

    def _refresh_pending_after_resolve(self):
        if not self._pending_refresh_after_resolve or getattr(self.controller, "busy", False):
            return
        self._pending_refresh_after_resolve = False
        if self._pending_dialog is not None and self._pending_dialog.isVisible():
            self._refresh_pending()

    def _download_directory(self):
        value = self.fields["storage.download_dir"].text().strip()
        if not value:
            raise ValueError("请选择下载文件夹，也可以手动填写另一硬盘上的完整路径。")
        path = Path(value).expanduser()
        return (path if path.is_absolute() else self.config_path.parent / path).resolve()

    def _choose_download_directory(self):
        if getattr(self.controller, "busy", False):
            self._feedback("请先等待当前操作结束，再更改下载文件夹。", error=True)
            return
        try:
            initial = self._download_directory()
        except (OSError, ValueError):
            initial = self.config_path.parent
        selected = QFileDialog.getExistingDirectory(self, "选择下载文件夹，可选择另一硬盘", str(initial))
        if selected:
            self.fields["storage.download_dir"].setText(str(Path(selected).expanduser().resolve()))
            self._feedback("下载文件夹已选择；点击保存或开始时会保存此路径。")

    def _open_download_directory(self):
        try:
            path = self._download_directory()
            if not path.is_dir():
                raise ValueError("下载文件夹尚未创建；首次下载时会自动创建，也可选择已有文件夹。")
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                raise ValueError("文件夹未能打开，请检查路径是否可用。")
        except (OSError, ValueError) as exc:
            self._feedback(self._safe_error(exc), error=True)

    def _cleanup_downloads(self):
        if not self._account_change_allowed():
            return
        if not callable(getattr(self.controller, "cleanup_downloads", None)):
            self._feedback("当前版本暂不支持清理已转发文件，请更新软件后重试。", error=True)
            return
        self._begin_task("cleanup")
        try:
            self._save()
            config = load_config(self.config_path, account_only=True)
            if not self.controller.cleanup_downloads(config):
                raise ValueError("清理未开始：请等待当前操作结束后重试。")
        except Exception as exc:
            self._task_phase_event({"phase": "failed", "message": self._safe_error(exc)})
            self._task_operation = None

    def _cleanup_ready(self, result):
        if self._task_operation != "cleanup":
            return
        if not isinstance(result, dict):
            self._task_phase_event({"phase": "failed", "message": "未收到有效清理结果，请查看本次日志后重试。"})
            return
        message = str(result.get("message") or "本地清理已结束；具体文件结果请查看本次日志。")
        phase = "failed" if result.get("success") is False or result.get("ok") is False else "cleaned"
        if not self._task_terminal_seen or phase != self._task_phase_key or message != self._task_last_message:
            self._task_phase_event({"phase": phase, "message": message})

    def closeEvent(self, event):
        if self._closed:
            event.accept()
            return
        self._close_requested = True
        self._feedback("正在保存状态并关闭连接；如在传输，会等待当前媒体组完成。")
        self.controller.shutdown()
        event.ignore()


def run_gui(config_path=None):
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("TelegramMover")
    app.setOrganizationName("LocalTools")
    window = MainWindow(config_path=config_path)
    window.show()
    return app.exec()
