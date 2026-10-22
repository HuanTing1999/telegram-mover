"""Qt signals around one dedicated asyncio/Telethon thread, with no console I/O."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import os
import queue
import re
import sqlite3
import threading
import time
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Signal
from telethon import TelegramClient, errors, functions, types, utils

from main import ProcessLock
from .config import Config
from .cleanup import cleanup_forwarded
from .engine import Mover, SendOutcomeUnknown, SyncError, UserStopped, batches
from .downloads import DownloadError
from .filters import AdFilter, FilterConfigError
from .forum import ForumError, list_forum_topics
from .limits import LimitError, LimitStop, Limiter
from .login_input import normalize_code, normalize_phone
from .state import State

AUTH_TIMEOUT_SECONDS = 45.0
AUTH_POLL_SECONDS = 0.1
AUTH_MAX_DC_RETRIES = 2
AUTH_LOCAL_RETRY_SECONDS = 30.0
AUTH_OPERATIONS = frozenset({"connected", "code_sent", "authenticated", "auth_cancelled"})


class WorkerError(RuntimeError):
    """An actionable, credential-free message for the GUI."""


class _GuiLogHandler(logging.Handler):
    def __init__(self, controller):
        super().__init__(logging.INFO)
        self.controller = controller

    def emit(self, record):
        if record.thread != self.controller._thread.ident:
            return
        if record.name.startswith("telethon") and record.levelno < logging.WARNING:
            return
        try:
            # Never render exception tracebacks into the GUI log.
            message = record.getMessage()
            if record.name.startswith("telethon") and "AuthRestartError" in message:
                message = ("Telegram 验证码服务要求重新开始授权（AUTH_RESTART），"
                           "正在按有限次数尝试；请以本次登录的最终提示为准。")
            self.controller.log.emit(self.controller._redact(message))
        except Exception:
            pass


class GuiController(QObject):
    status = Signal(str)
    log = Signal(str)
    authenticated = Signal(dict)
    code_sent = Signal(str)
    code_delivery = Signal(dict)
    password_required = Signal(str)
    challenge_ended = Signal()
    preview_ready = Signal(list)
    chats_ready = Signal(list)
    topics_ready = Signal(dict)
    cleanup_ready = Signal(dict)
    pending_ready = Signal(list)
    stats = Signal(dict)
    task_phase = Signal(dict)
    finished = Signal(str)
    failed = Signal(str)
    busy_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._gate = threading.Lock()
        self._busy = False
        self._closing = False
        self._closed = False
        self._stop_event = threading.Event()
        self._jobs = queue.SimpleQueue()
        self._loop = None
        self._client = None
        self._identity = None
        self._session_lock = None
        self._phone = None
        self._phone_code_hash = None
        self._code_delivery_type = None
        self._code_next_type = None
        self._code_resend_at = 0.0
        self._auth_wait_context = None
        self._auth_waits = {"server_until": 0.0, "local_until": 0.0, "delivery_until": 0.0}
        self._code_attempts = 0
        self._password_pending = False
        self._password_attempts = 0
        self._secrets = set()
        self._active_task_status = None
        self._pending_snapshots = {}
        self._handler = _GuiLogHandler(self)
        self._logging_settings = []
        self._thread = threading.Thread(target=self._thread_main, name="TGMover worker", daemon=True)
        self._thread.start()

    @property
    def busy(self) -> bool:
        with self._gate:
            return self._busy

    @property
    def closing(self) -> bool:
        with self._gate:
            return self._closing

    def _remember(self, *values):
        with self._gate:
            self._secrets.update(str(value) for value in values if value)

    def _redact(self, text) -> str:
        text = str(text)
        with self._gate:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for secret in secrets:
            text = text.replace(secret, "[已隐藏]")
        text = re.sub(r"(?<![\w])\+[0-9]{7,15}\b", "[手机号已隐藏]", text)
        text = re.sub(
            r"(?i)((?:api[_-]?hash|phone[_-]?code[_-]?hash|phone|code|otp|password|密码|验证码|手机号)\s*[=:：]\s*)[^\s,;]+",
            r"\1[已隐藏]", text,
        )
        return text[:4000]

    def _install_logging(self):
        if self._logging_settings:
            return
        for name, level in (("tgmover", logging.INFO), ("telethon", logging.WARNING)):
            logger = logging.getLogger(name)
            self._logging_settings.append((logger, logger.level, logger.propagate))
            logger.addHandler(self._handler)
            logger.setLevel(level)
            logger.propagate = False

    def _restore_logging(self):
        for logger, level, propagate in self._logging_settings:
            logger.removeHandler(self._handler)
            logger.setLevel(level)
            logger.propagate = propagate
        self._logging_settings.clear()

    def _thread_main(self):
        loop = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            with self._gate:
                self._loop = loop
            loop.call_soon(self._drain_jobs)
            loop.run_forever()
        except Exception:
            with self._gate:
                self._busy = False
                self._closed = True
            self.failed.emit("无法初始化后台执行环境，请关闭后重新打开程序。")
            self.busy_changed.emit(False)
            self.finished.emit("closed")
        finally:
            self._restore_logging()
            if loop is not None:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()

    def _drain_jobs(self):
        while True:
            try:
                kind, factory = self._jobs.get_nowait()
            except queue.Empty:
                return
            self._loop.create_task(self._operate(kind, factory))

    def _submit(self, kind: str, factory: Callable, *, allow_closing=False) -> bool:
        with self._gate:
            if self._closed or (self._closing and not allow_closing):
                rejected = "程序正在关闭，不能开始新的操作。"
            elif self._busy:
                rejected = "已有操作正在运行，请等待结束或点击停止。"
            else:
                rejected = None
                self._busy = True
                if not allow_closing:
                    self._stop_event.clear()
                self._jobs.put((kind, factory))
                loop = self._loop
        if rejected:
            self.failed.emit(rejected)
            return False
        self.busy_changed.emit(True)
        if loop is not None:
            loop.call_soon_threadsafe(self._drain_jobs)
        return True

    async def _operate(self, kind, factory):
        self._install_logging()
        self._active_task_status = None
        outcome = kind
        try:
            returned = await self._run_auth_operation(factory) if kind in AUTH_OPERATIONS else await factory()
            if isinstance(returned, str):
                outcome = returned
        except SendOutcomeUnknown as exc:
            outcome = "uncertain"
            message = self._redact(exc)
            self._task_status({"phase": "uncertain", "message": message})
            self.failed.emit(message)
        except UserStopped:
            outcome = "stopped"
            self.status.emit("登录操作已取消，未自动重发验证码。" if kind in AUTH_OPERATIONS
                             else "已安全停止，已完成的映射和进度已保存。")
            if kind not in AUTH_OPERATIONS:
                self._task_status({"phase": "stopped", "message": "已安全停止，已完成的映射和进度已保存。"})
        except LimitStop as exc:
            outcome = "stopped"
            message = self._redact(exc)
            self.status.emit(message)
            if kind not in AUTH_OPERATIONS:
                self._task_status({"phase": "stopped", "message": message})
            else:
                self.log.emit(message)
        except (KeyboardInterrupt, EOFError):
            outcome = "stopped"
            self.status.emit("操作已停止。")
        except asyncio.CancelledError:
            # Telethon cancels pending RPC futures when a connection closes.
            # CancelledError is a BaseException and must never fall through to
            # a default successful outcome with no status/error notification.
            outcome = "failed"
            message = "连接已中断，操作未完成。请检查网络后重试。"
            self.status.emit(message)
            if kind not in AUTH_OPERATIONS:
                self._task_status({"phase": "failed", "message": message})
            self.failed.emit(message)
        except Exception as exc:
            outcome = "failed"
            message = self._friendly_error(exc)
            if kind not in AUTH_OPERATIONS and self._active_task_status:
                message = f"{self._active_task_status['message'].rstrip('…')}\n{message}"
            self.status.emit(message)
            if kind not in AUTH_OPERATIONS:
                self._task_status({"phase": "failed", "message": message})
            self.failed.emit(message)
        finally:
            with self._gate:
                closing = self._closing
                if not closing:
                    # Publish idle atomically with the close decision. Otherwise
                    # shutdown() could see "busy" after this decision and leave
                    # no operation responsible for disconnecting the client.
                    self._busy = False
            if closing:
                try:
                    await self._disconnect()
                except Exception:
                    self.log.emit("连接已关闭；断开时出现网络异常。")
                outcome = "closed"
                with self._gate:
                    self._closed = True
                    self._busy = False
            self.busy_changed.emit(False)
            self.finished.emit(outcome)
            if closing:
                self._loop.call_soon(self._loop.stop)

    async def _reset_auth_connection(self):
        self._clear_login_challenge()
        self.challenge_ended.emit()
        try:
            await asyncio.wait_for(self._disconnect(), timeout=5)
        except (Exception, asyncio.CancelledError):
            self.log.emit("登录连接已关闭；请检查网络后手动重试。")

    async def _run_auth_operation(self, factory):
        """Bound login waits only; never cancel a mover's in-flight upload."""
        task = asyncio.create_task(factory())
        deadline = asyncio.get_running_loop().time() + AUTH_TIMEOUT_SECONDS
        reset = False
        try:
            while not task.done():
                if self._stop_event.is_set():
                    reset = True
                    raise UserStopped("登录操作已取消。")
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    reset = True
                    raise WorkerError(
                        f"连接 Telegram 超时（{AUTH_TIMEOUT_SECONDS:g} 秒），已停止等待。"
                        "请检查网络后重试，或先恢复已有会话；不会自动重发验证码。" + self._local_retry_hint()
                    )
                await asyncio.wait((task,), timeout=min(AUTH_POLL_SECONDS, remaining))
            return task.result()
        except asyncio.CancelledError:
            reset = True
            raise WorkerError("Telegram 登录连接已中断，未完成本次验证。请检查网络后重试或恢复已有会话。"
                              + self._local_retry_hint()) from None
        except TimeoutError:
            reset = True
            raise WorkerError(
                f"连接 Telegram 超时（{AUTH_TIMEOUT_SECONDS:g} 秒），已停止等待。"
                "请检查网络后重试，或先恢复已有会话；不会自动重发验证码。" + self._local_retry_hint()
            ) from None
        except (UserStopped, OSError):
            reset = True
            raise
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if reset:
                await self._reset_auth_connection()

    async def _auth_rpc(self, stage, factory):
        """Replay only explicit DC migrations, never generic network errors.

        Telethon with request_retries=0 changes DC, then raises the migration
        without replaying the request. Login needs that replay for accounts
        hosted in a different DC; mover sends retain their zero-retry policy.
        """
        async def attempts():
            for attempt in range(AUTH_MAX_DC_RETRIES + 1):
                if self._stop_event.is_set():
                    raise UserStopped("登录操作已取消。")
                self.status.emit(f"{stage}（最多等待 {AUTH_TIMEOUT_SECONDS:g} 秒，可点击停止）")
                try:
                    return await factory()
                except (errors.PhoneMigrateError, errors.NetworkMigrateError, errors.UserMigrateError) as exc:
                    if attempt >= AUTH_MAX_DC_RETRIES:
                        raise WorkerError("Telegram 登录数据中心切换未完成，请检查网络后手动重试。") from None
                    self.status.emit("正在切换至该账号所属的 Telegram 数据中心…")
                    # Usually Telethon already switched before raising. Do not
                    # reconnect again and throw away the newly negotiated key.
                    if getattr(getattr(self._client, "session", None), "dc_id", exc.new_dc) != exc.new_dc:
                        await self._client._switch_dc(exc.new_dc)
                except errors.FloodWaitError as exc:
                    self._save_auth_wait("server_until", exc.seconds)
                    self._emit_code_delivery(f"Telegram 服务端要求等待 {exc.seconds} 秒后再尝试登录操作。")
                    raise
        return await asyncio.wait_for(attempts(), timeout=AUTH_TIMEOUT_SECONDS)

    def _friendly_error(self, exc) -> str:
        if isinstance(exc, errors.AuthRestartError):
            return ("Telegram 验证码服务要求重新开始授权（AUTH_RESTART），本次登录未完成。"
                    "请稍后重新获取验证码；不需要更改国家码，也不会自动无限重试。")
        if isinstance(exc, errors.SessionPasswordNeededError):
            return "此账号启用了两步验证，请在软件中的两步验证密码区域继续登录。"
        if isinstance(exc, errors.FloodWaitError):
            return f"Telegram 服务端要求等待 {exc.seconds} 秒，请稍后重试；等待期限已保存，重启不会解除。"
        if isinstance(exc, errors.PhoneNumberInvalidError):
            return "Telegram 未接受该手机号，请检查国家码和号码格式。"
        if isinstance(exc, errors.PhoneNumberBannedError):
            return "该手机号受到 Telegram 限制，请先在官方客户端检查账号。"
        if isinstance(exc, errors.ApiIdInvalidError):
            return "Telegram 未接受应用凭证，请检查你自己申请的 API_ID 和 API_HASH 是否配对正确。"
        specific = {
            "ChannelPrivateError": "当前账号无法读取这个频道／群组，请在 Telegram 官方客户端确认已加入且有访问权限。",
            "UsernameInvalidError": "频道／群组用户名格式无效，请检查源和目标的链接或 @用户名。",
            "UsernameNotOccupiedError": "没有找到该频道／群组用户名，请检查源和目标是否填写正确。",
            "ChatWriteForbiddenError": "当前账号没有目标聊天的发送权限，请在 Telegram 中检查目标权限。",
            "ChatAdminRequiredError": "此操作需要目标频道／群组管理员权限，请先为当前账号授权。",
            "ChatForwardsRestrictedError": "源聊天启用了内容保护，Telegram 拒绝转发或保存受保护内容；任务已停止。",
        }
        if type(exc).__name__ in specific:
            return specific[type(exc).__name__]
        if isinstance(exc, TimeoutError):
            return "连接 Telegram 超时，请检查网络后重试。" + self._local_retry_hint()
        if isinstance(exc, (WorkerError, SyncError, LimitError, FilterConfigError, ForumError, DownloadError)):
            return self._redact(exc)
        if isinstance(exc, errors.RPCError):
            if isinstance(exc, errors.SendCodeUnavailableError):
                return ("Telegram 暂无可用的验证码再次发送渠道（SEND_CODE_UNAVAILABLE）。"
                        "请查看官方客户端的登录提示，或取消本次验证后稍后重新获取；不会强制短信。")
            if isinstance(exc, (errors.PhoneNumberFloodError, errors.PhonePasswordFloodError)):
                return ("Telegram 限制了此手机号的登录尝试，但未提供具体等待时间。"
                        "请暂停尝试并在官方客户端检查账号；不要连续获取验证码。")
            return f"Telegram 拒绝了操作（{type(exc).__name__}）。请检查账号状态、源/目标权限或稍后重试。"
        if isinstance(exc, OSError):
            return (f"文件或连接操作失败（{type(exc).__name__}），请检查网络、目录权限和会话是否被其他程序占用。"
                    + self._local_retry_hint())
        return f"操作未完成（{type(exc).__name__}），请检查配置后重试。"

    def connect_account(self, config: Config) -> bool:
        self._remember(config.api_hash)
        return self._submit("connected", lambda: self._connect_account(config))

    def request_code(self, config: Config, phone: str) -> bool:
        phone = str(phone).strip()
        self._remember(config.api_hash, phone, phone.lstrip("+"))
        return self._submit("code_sent", lambda: self._request_code(config, phone))

    def resend_code(self, config: Config, phone: str) -> bool:
        phone = str(phone).strip()
        self._remember(config.api_hash, phone, phone.lstrip("+"))
        return self._submit("code_sent", lambda: self._resend_code(config, phone))

    def submit_code(self, code: str) -> bool:
        code = str(code).strip()
        self._remember(code)
        return self._submit("authenticated", lambda: self._submit_code(code))

    def submit_password(self, password: str) -> bool:
        async def operation():
            if not isinstance(password, str) or password == "":
                raise WorkerError("请输入 Telegram 两步验证密码。")
            # Keep passwords only while their accepted login operation runs.
            # Never put them in a configuration, account index, or log message.
            with self._gate:
                already_known = password in self._secrets
                self._secrets.add(password)
            try:
                return await self._submit_password(password)
            finally:
                if not already_known:
                    with self._gate:
                        self._secrets.discard(password)
        return self._submit("authenticated", operation)

    def cancel_authentication(self) -> bool:
        return self._submit("auth_cancelled", self._cancel_authentication)

    async def _cancel_authentication(self):
        await self._reset_auth_connection()
        self.status.emit("已取消本次登录；已有会话文件保留。")
        return "auth_cancelled"

    def run_task(self, config: Config, once: bool = True, check_only: bool = False) -> bool:
        self._remember(config.api_hash)
        return self._submit("checked" if check_only else "completed",
                            lambda: self._run_task(config, once=once, check_only=check_only))

    def preview(self, config: Config, limit: int = 50) -> bool:
        self._remember(config.api_hash)
        return self._submit("preview", lambda: self._preview(config, limit))

    def list_chats(self, config: Config) -> bool:
        """Fetch the authorized account's group/channel choices on its thread."""
        self._remember(config.api_hash)
        return self._submit("chats", lambda: self._list_chats(config))

    def list_topics(self, config: Config, peer) -> bool:
        """Read existing topics for the selected forum on the account thread."""
        self._remember(config.api_hash)
        return self._submit("topics", lambda: self._list_topics(config, peer))

    def cleanup_downloads(self, config: Config) -> bool:
        """Clean registered, confirmed forwards locally, without Telegram I/O."""
        return self._submit("cleaned", lambda: self._cleanup_downloads(config))

    def list_pending(self, config: Config) -> bool:
        return self._submit("pending", lambda: self._list_pending(config))

    def resolve_pending(self, config: Config, route: str, key: str, scope: str) -> bool:
        # The GUI requires selecting one whole operation and acknowledging
        # that its possible destination messages were checked and cleaned.
        return self._submit("pending_resolved", lambda: self._resolve_pending(config, route, key, scope))

    def stop(self):
        self._stop_event.set()
        self.status.emit("正在停止：等待当前操作或媒体组完成并保存进度。")

    def shutdown(self):
        with self._gate:
            already_closed = self._closed
            if self._closing and not already_closed:
                return
            if not already_closed:
                self._closing = True
            busy = self._busy
        if already_closed:
            # Initialization may have failed before the window wired signals.
            # Repeat the terminal notification so closeEvent can still finish.
            self.busy_changed.emit(False)
            self.finished.emit("closed")
            return
        if busy:
            self.stop()
        else:
            self._submit("closed", self._disconnect, allow_closing=True)

    async def _stopaware_sleep(self, seconds):
        deadline = asyncio.get_running_loop().time() + seconds
        while True:
            if self._stop_event.is_set():
                raise UserStopped("已停止等待，已完成的进度保留。")
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.25, remaining))

    @staticmethod
    def _client_identity(config):
        session = str(Path(config.session).expanduser().resolve())
        if not session.endswith(".session"):
            session += ".session"
        return config.api_id, config.api_hash, session

    def _auth_wait_store(self, config, phone):
        session = self._client_identity(config)[2]
        key = hashlib.sha256((os.path.normcase(session) + "\0" + phone).encode("utf-8")).hexdigest()
        self._auth_wait_context = (Path(session).parent / "auth_cooldowns.sqlite3", key)
        self._load_auth_waits()

    def _auth_wait_database(self):
        path, key = self._auth_wait_context
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path, timeout=5)
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("""CREATE TABLE IF NOT EXISTS auth_cooldowns(
                key TEXT PRIMARY KEY, server_until REAL NOT NULL DEFAULT 0,
                local_until REAL NOT NULL DEFAULT 0, delivery_until REAL NOT NULL DEFAULT 0)""")
            connection.execute("INSERT OR IGNORE INTO auth_cooldowns(key) VALUES (?)", (key,))
            connection.commit()
            return connection, key
        except (OSError, sqlite3.Error):
            if "connection" in locals():
                connection.close()
            raise WorkerError("无法读取或保存登录等待记录。请检查会话目录权限，关闭占用后重试；未继续请求验证码。") from None

    def _load_auth_waits(self):
        if self._auth_wait_context is None:
            return
        connection, key = self._auth_wait_database()
        try:
            row = connection.execute(
                "SELECT server_until,local_until,delivery_until FROM auth_cooldowns WHERE key=?", (key,)).fetchone()
            values = dict(zip(("server_until", "local_until", "delivery_until"), row))
            if any(not math.isfinite(value) or value < 0 for value in values.values()):
                raise ValueError()
            self._auth_waits = values
        except (sqlite3.Error, ValueError, TypeError):
            raise WorkerError("登录等待记录损坏或暂不可用；为避免重复请求，本次登录操作已停止。") from None
        finally:
            connection.close()

    def _save_auth_wait(self, field, seconds):
        if self._auth_wait_context is None:
            return
        if field not in {"server_until", "local_until", "delivery_until"}:
            raise ValueError("Unknown authentication wait kind")
        deadline = time.time() + max(0.0, float(seconds))
        self._auth_waits[field] = max(self._auth_waits.get(field, 0.0), deadline)
        connection, key = self._auth_wait_database()
        try:
            with connection:
                connection.execute(f"UPDATE auth_cooldowns SET {field}=MAX({field},?) WHERE key=?", (deadline, key))
        except sqlite3.Error:
            raise WorkerError("无法保存登录等待期限。请检查会话目录权限并稍后重试；不会自动重复请求。") from None
        finally:
            connection.close()

    def _local_retry_hint(self):
        remaining = self._auth_waits.get("local_until", 0.0) - time.time()
        if remaining <= 0:
            return ""
        return f"本次结果未确认；本机为防重复请求，需再等待 {math.ceil(remaining)} 秒。这不是 Telegram 服务端限流。"

    def _check_auth_wait(self, *, delivery=True):
        self._load_auth_waits()
        now = time.time()
        choices = [
            (self._auth_waits.get("server_until", 0) - now,
             "Telegram 服务端要求等待 {seconds} 秒；等待期限已保存，重启不会解除。"),
            (self._auth_waits.get("local_until", 0) - now,
             "上次请求结果未确认；本机为防重复请求，需等待 {seconds} 秒。这不是 Telegram 服务端限流。"),
        ]
        if delivery:
            choices.append((max(self._code_resend_at, self._auth_waits.get("delivery_until", 0)) - now,
                            "请等待 Telegram 规定的验证码接收时间，还需 {seconds} 秒才能再次请求；当前验证码可继续提交。"))
        remaining, template = max(choices, key=lambda item: item[0])
        if remaining > 0:
            message = template.format(seconds=math.ceil(remaining))
            self._emit_code_delivery(message)
            raise WorkerError(message)

    @staticmethod
    def _delivery_label(value, *, next_type=False):
        if value is None:
            return ""
        labels = {
            "App": "Telegram 官方客户端", "Sms": "短信", "Call": "语音电话",
            "FlashCall": "来电号码验证", "MissedCall": "未接来电验证",
            "FragmentSms": "Fragment 短信", "EmailCode": "邮箱验证码",
            "SetUpEmailRequired": "Telegram 要求先设置登录邮箱",
            "FirebaseSms": "Telegram 指定的短信验证", "SmsWord": "短信口令",
            "SmsPhrase": "短信短语",
        }
        name = type(value).__name__
        prefix = "CodeType" if next_type else "SentCodeType"
        return labels.get(name.removeprefix(prefix), "Telegram 指定的验证渠道")

    def _emit_code_delivery(self, message):
        now = time.time()
        wait = max(0.0, self._code_resend_at - now,
                   *(value - now for value in self._auth_waits.values()))
        available = bool(self._phone and self._phone_code_hash and self._code_next_type is not None
                         and not self._password_pending)
        self.code_delivery.emit({
            "can_resend": available and wait <= 0, "resend_available": available,
            "resend_after": float(wait),
            "delivery": self._delivery_label(self._code_delivery_type),
            "next_delivery": self._delivery_label(self._code_next_type, next_type=True),
            "message": message,
        })

    def _clear_client_challenge(self):
        client = self._client
        if client is None:
            return
        cache = getattr(client, "_phone_code_hash", None)
        if isinstance(cache, dict):
            cache.clear()
        # Clearing consumed challenge hashes never logs out or edits the
        # authorized session. Preserve the signed-in client's cached phone.
        if (hasattr(client, "_phone") and not getattr(client, "_authorized", False)
                and not getattr(client, "authorized", False)):
            client._phone = None

    def _clear_login_challenge(self):
        had_challenge = bool(self._phone or self._phone_code_hash or self._password_pending)
        self._clear_client_challenge()
        self._phone = self._phone_code_hash = None
        self._code_delivery_type = self._code_next_type = None
        self._code_resend_at = 0.0
        self._code_attempts = 0
        self._password_pending = False
        self._password_attempts = 0
        if had_challenge:
            self._emit_code_delivery("本次验证码验证已结束；已有会话保留，可按等待提示重新开始。")

    async def _disconnect(self):
        client, lock = self._client, self._session_lock
        self._clear_login_challenge()
        self._client = self._session_lock = self._identity = None
        self._auth_wait_context = None
        try:
            if client is not None:
                await client.disconnect()
        finally:
            if lock is not None:
                lock.__exit__(None, None, None)

    async def _ensure_client(self, config):
        identity = self._client_identity(config)
        session = identity[2]
        if self._client is not None and self._identity == identity:
            if not getattr(self._client, "is_connected", lambda: True)():
                await self._auth_rpc("正在重新连接 Telegram", self._client.connect)
            return
        await self._disconnect()
        self._auth_waits = {"server_until": 0.0, "local_until": 0.0, "delivery_until": 0.0}
        config.session.parent.mkdir(parents=True, exist_ok=True)
        lock = ProcessLock(Path(session + ".mover.lock"))
        lock.__enter__()
        self._session_lock = lock
        try:
            self._client = TelegramClient(
                str(config.session), config.api_id, config.api_hash,
                flood_sleep_threshold=0, request_retries=0,
                raise_last_call_error=True, receive_updates=False,
            )
            self._identity = identity
            await self._auth_rpc("正在连接 Telegram", self._client.connect)
        except Exception:
            await self._disconnect()
            raise

    async def _user(self, *, required=False):
        if not await self._auth_rpc("正在检查用户会话", self._client.is_user_authorized):
            if required:
                raise WorkerError("请先在账号区域使用手机号和验证码完成登录。")
            return None
        user = await self._auth_rpc("正在读取账号信息", self._client.get_me)
        if user is None:
            raise WorkerError("未能读取已登录的用户账号，请重新连接会话。")
        if getattr(user, "bot", False):
            await self._disconnect()
            raise WorkerError("当前 session 属于机器人账号。工具只接受 MTProto 用户账号，请更换会话文件。")
        return user

    def _emit_user(self, user):
        name = " ".join(filter(None, [getattr(user, "first_name", ""), getattr(user, "last_name", "")]))
        self.authenticated.emit({
            "id": user.id, "name": self._redact(name or "用户账号"),
            "username": self._redact(getattr(user, "username", "") or ""),
        })

    async def _connect_account(self, config):
        self.status.emit("正在连接用户会话…")
        await self._ensure_client(config)
        user = await self._user()
        if user is not None:
            self._emit_user(user)
            self.status.emit("已复用用户会话，无需重复登录。")
            return "authenticated"
        self.status.emit("尚未登录，请输入手机号并获取验证码。")
        return "connected"

    async def _request_code(self, config, phone):
        if self._password_pending:
            raise WorkerError("请先完成两步验证，或取消本次登录后再获取验证码。")
        try:
            phone = normalize_phone(phone)
        except ValueError as exc:
            raise WorkerError(str(exc)) from None
        self._remember(phone, phone.lstrip("+"))
        await self._ensure_client(config)
        user = await self._user()
        if user is not None:
            saved_phone = str(getattr(user, "phone", "") or "")
            try:
                saved_phone = normalize_phone(saved_phone if saved_phone.startswith("+") else "+" + saved_phone)
            except ValueError:
                saved_phone = None
            if saved_phone != phone:
                raise WorkerError(
                    "当前会话已登录另一个账号。请点击“添加账号”，为新手机号保存独立的登录状态；"
                    "应用凭证无需更换，现有账号不会退出或删除。"
                )
            self._emit_user(user)
            self.status.emit("会话已登录，无需发送验证码。")
            return "authenticated"
        if self._phone == phone and self._phone_code_hash:
            self._emit_code_delivery("已有待验证的验证码；未收到时请点击重新发送，或取消后重新开始。")
            raise WorkerError("已有待验证的验证码；请提交验证码、点击重新发送，或取消后重新开始。")
        # Clear the old challenge before a new explicit request, so a failed
        # resend cannot leave the UI accepting an outdated verification code.
        self._clear_login_challenge()
        self._auth_wait_store(config, phone)
        self._check_auth_wait()
        try:
            sent = await self._code_request_rpc("正在向 Telegram 请求验证码",
                                                lambda: self._client.send_code_request(phone))
        except errors.AuthRestartError:
            await self._reset_auth_connection()
            raise
        return self._accept_code(phone, sent)

    async def _code_request_rpc(self, stage, factory):
        try:
            return await self._auth_rpc(stage, factory)
        except (OSError, TimeoutError, asyncio.CancelledError):
            self._save_auth_wait("local_until", AUTH_LOCAL_RETRY_SECONDS)
            self._emit_code_delivery(self._local_retry_hint())
            raise
        except (errors.PhoneNumberFloodError, errors.PhonePasswordFloodError):
            # These errors contain no server deadline. Keep a clearly labelled
            # local guard, rather than inventing a Telegram waiting period.
            self._save_auth_wait("local_until", AUTH_LOCAL_RETRY_SECONDS)
            self._emit_code_delivery("Telegram 限制了登录请求但未提供等待期限；请暂停尝试并检查官方客户端。")
            raise
        except errors.AuthRestartError:
            raise  # The explicit caller ends this challenge without replaying.
        except errors.RPCError as exc:
            if abs(getattr(exc, "code", 0)) < 500:
                raise
            self._save_auth_wait("local_until", AUTH_LOCAL_RETRY_SECONDS)
            self._emit_code_delivery("Telegram 返回服务器异常，本次验证码请求结果未确认。" + self._local_retry_hint())
            await self._reset_auth_connection()
            raise WorkerError(f"Telegram 服务器异常（{type(exc).__name__}），本次验证码请求结果未确认。"
                              + self._local_retry_hint()) from None

    def _accept_code(self, phone, sent):
        if not getattr(sent, "phone_code_hash", None):
            self._clear_login_challenge()
            self.challenge_ended.emit()
            raise WorkerError("Telegram 未返回可用的验证码请求。请检查官方客户端中的登录提示后手动重试。")
        if isinstance(sent, types.auth.SentCodePaymentRequired):
            self._clear_login_challenge()
            self.challenge_ended.emit()
            raise WorkerError("Telegram 要求先处理该号码的登录付费限制。请在官方客户端检查；软件不会代付或绕过。")
        self._phone, self._phone_code_hash = phone, sent.phone_code_hash
        self._remember(sent.phone_code_hash)
        self._code_attempts = 0
        self._code_delivery_type = getattr(sent, "type", None)
        self._code_next_type = getattr(sent, "next_type", None)
        timeout = getattr(sent, "timeout", None)
        timeout = max(0.0, float(timeout)) if isinstance(timeout, (int, float)) and math.isfinite(timeout) else 0.0
        self._code_resend_at = time.time() + timeout
        self._save_auth_wait("delivery_until", timeout)
        cache = getattr(self._client, "_phone_code_hash", None)
        if isinstance(cache, dict):
            cache[utils.parse_phone(phone)] = sent.phone_code_hash
        if hasattr(self._client, "_phone"):
            self._client._phone = utils.parse_phone(phone)
        delivery = self._delivery_label(self._code_delivery_type) or "Telegram 指定渠道"
        message = f"Telegram 已接受验证码请求，接收渠道：{delivery}。输入后点击“验证并登录”或按回车。"
        if timeout:
            message += "再次请求请查看重发倒计时；等待期间仍可提交当前验证码。"
        if self._code_next_type is None:
            message += "Telegram 未提供再次发送渠道；未收到时可取消本次验证，按等待提示重新获取。"
        self._emit_code_delivery(message)
        self.code_sent.emit(message)
        self.status.emit(message)
        return "code_sent"

    async def _resend_code(self, config, phone):
        if self._password_pending:
            raise WorkerError("验证码已通过，请继续两步验证；无需重新发送验证码。")
        try:
            phone = normalize_phone(phone)
        except ValueError as exc:
            raise WorkerError(str(exc)) from None
        if (self._client is None or self._identity != self._client_identity(config)
                or self._phone != phone or not self._phone_code_hash):
            raise WorkerError("当前手机号或会话与待验证登录不一致。请取消本次验证后，在正确账号中重新获取验证码。")
        self._remember(phone, phone.lstrip("+"))
        self._auth_wait_store(config, phone)
        self._check_auth_wait()
        if self._code_next_type is None:
            message = "Telegram 未提供再次发送渠道；请查看官方客户端，或取消后按等待提示重新获取验证码。"
            self._emit_code_delivery(message)
            raise WorkerError(message)
        current_hash = self._phone_code_hash
        try:
            sent = await self._code_request_rpc("正在请求 Telegram 再次发送验证码",
                lambda: self._client(functions.auth.ResendCodeRequest(
                    phone_number=utils.parse_phone(phone), phone_code_hash=current_hash)))
        except (errors.PhoneCodeExpiredError, errors.PhoneCodeHashEmptyError):
            self._clear_login_challenge()
            self.challenge_ended.emit()
            raise WorkerError("本次验证码请求已失效；请重新获取验证码，不会自动重复请求。") from None
        except errors.SendCodeUnavailableError:
            self._code_next_type = None
            self._emit_code_delivery("Telegram 暂无可用的再次发送渠道；可检查官方客户端或取消后稍后重新获取。")
            raise
        except errors.AuthRestartError:
            await self._reset_auth_connection()
            raise
        return self._accept_code(phone, sent)

    def _invalid_code(self):
        remaining = 3 - self._code_attempts
        if remaining <= 0:
            self._clear_login_challenge()
            self.challenge_ended.emit()
            raise WorkerError("验证码连续 3 次无效，本次登录已停止；请重新获取验证码。")
        raise WorkerError(f"验证码无效，还可尝试 {remaining} 次。")

    async def _submit_code(self, code):
        if self._password_pending:
            raise WorkerError("验证码已通过，请继续输入两步验证密码，无需重新提交验证码。")
        if self._client is None or not self._phone or not self._phone_code_hash:
            raise WorkerError("请先获取验证码，再提交登录。")
        self._check_auth_wait(delivery=False)
        self._code_attempts += 1
        # An empty Telethon sign_in code would send a new code implicitly.
        try:
            code = normalize_code(code)
        except ValueError:
            self._invalid_code()
        self._remember(code)
        try:
            await self._auth_rpc("正在验证登录并保存会话", lambda: self._client.sign_in(
                phone=self._phone, code=code, phone_code_hash=self._phone_code_hash))
        except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError):
            self._invalid_code()
        except errors.PhoneCodeExpiredError:
            self._clear_login_challenge()
            self.challenge_ended.emit()
            raise WorkerError("验证码已过期；请点击获取验证码重新申请，不会自动重发。") from None
        except errors.AuthRestartError:
            await self._reset_auth_connection()
            raise
        except errors.SessionPasswordNeededError:
            self._phone_code_hash = None
            self._password_pending = True
            self._password_attempts = 0
            message = "验证码已通过。此账号启用了两步验证，请在软件中输入两步验证密码继续登录；密码不会保存。"
            self._emit_code_delivery(message)
            self.password_required.emit(message)
            self.status.emit(message)
            return "password_required"
        user = await self._user(required=True)
        self._clear_login_challenge()
        self._emit_user(user)
        self.status.emit("用户账号登录成功，会话已保存。")
        return "authenticated"

    async def _submit_password(self, password):
        if self._client is None or not self._password_pending or not self._phone:
            raise WorkerError("请先提交验证码，等待 Telegram 要求两步验证后再输入密码。")
        self._check_auth_wait(delivery=False)
        try:
            await self._auth_rpc("正在验证两步验证密码并保存会话",
                                 lambda: self._client.sign_in(password=password))
        except errors.PasswordHashInvalidError:
            self._password_attempts += 1
            remaining = 3 - self._password_attempts
            if remaining <= 0:
                await self._reset_auth_connection()
                raise WorkerError("两步验证密码连续 3 次不正确，本次登录已停止。请确认密码后重新登录。") from None
            raise WorkerError(f"两步验证密码不正确，还可尝试 {remaining} 次。") from None
        except (errors.SrpIdInvalidError, errors.SrpPasswordChangedError):
            raise WorkerError("两步验证参数已更新，请重新输入当前密码并提交，无需重新获取验证码。") from None
        except errors.AuthRestartError:
            await self._reset_auth_connection()
            raise
        user = await self._user(required=True)
        self._clear_login_challenge()
        self._emit_user(user)
        self.status.emit("两步验证登录成功，会话已保存；密码未保存。")
        return "authenticated"

    async def _authorized(self, config):
        await self._ensure_client(config)
        user = await self._user(required=True)
        self._emit_user(user)
        return user

    def _mover(self, config, state, user):
        limiter = Limiter(state, user.id, config.limits, sleep=self._stopaware_sleep)
        mover = Mover(self._client, config, state, limiter=limiter,
                      should_stop=self._stop_event.is_set, on_progress=self.stats.emit,
                      on_status=self._task_status)
        limiter.on_wait = mover.waiting
        return mover

    def _task_status(self, event):
        event = {**event, "message": self._redact(event["message"])}
        self._active_task_status = event
        self.task_phase.emit(event)
        self.status.emit(event["message"])

    @staticmethod
    def _pending_ids(value):
        try:
            ids = json.loads(value) if isinstance(value, str) else value
        except (TypeError, ValueError):
            return []
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 10 or len(set(map(str, ids))) != len(ids)
                or any(type(identifier) is not int or not 0 < identifier <= 2147483647 for identifier in ids)):
            return []
        return ids

    async def _list_pending(self, config):
        rows = []
        database_key = os.path.normcase(str(config.database.resolve()))
        snapshots = {}
        if config.database.is_file():
            with ExitStack() as stack:
                stack.enter_context(ProcessLock(Path(str(config.database) + ".lock")))
                state = State(config.database)
                stack.callback(state.close)
                for row in state.conn.execute(
                        "SELECT operations.*, routes.signature FROM operations LEFT JOIN routes "
                        "ON routes.id=operations.route ORDER BY started_at, operations.route, scope, key"):
                    try:
                        signature = json.loads(row["signature"])
                        if not isinstance(signature, dict):
                            signature = {}
                    except (TypeError, ValueError):
                        signature = {}
                    comment = row["scope"] == "comment"
                    source = signature.get("source_link") if comment else signature.get("source")
                    target = signature.get("target_link") if comment else signature.get("target")
                    if comment:
                        root = re.match(r"comment:([0-9]+):", row["key"])
                        thread = state.conn.execute(
                            "SELECT source_root_id,target_root_id FROM threads WHERE route=? AND source_root_id=? LIMIT 1",
                            (row["route"], int(root.group(1)))).fetchone() if root else None
                        if thread:
                            source = f"{source or '未知讨论组'}（评论根 {thread['source_root_id']}）"
                            target = f"{target or '未知讨论组'}（评论根 {thread['target_root_id']}）"
                    rows.append({"route": row["route"], "key": row["key"], "scope": row["scope"],
                                 "source_ids": self._pending_ids(row["source_ids"]),
                                 "created_at": row["started_at"], "source": str(source or "未知"),
                                 "target": str(target or "未知"),
                                 "source_topic_id": 0 if comment else signature.get("source_topic_id", 0),
                                 "target_topic_id": 0 if comment else signature.get("target_topic_id", 0),
                                 "mode": signature.get("comment_mode" if comment else "mode", "未知")})
                    snapshots[(database_key, row["route"], row["scope"], row["key"])] = (
                        row["started_at"], row["source_ids"], row["signature"])
        self._pending_snapshots = {key: value for key, value in self._pending_snapshots.items() if key[0] != database_key}
        self._pending_snapshots.update(snapshots)
        self.pending_ready.emit(rows)
        self.status.emit(f"已读取当前数据库 {len(rows)} 批待核对发送；未连接 Telegram 或发送内容。")
        return "pending"

    async def _resolve_pending(self, config, route, key, scope):
        if (not isinstance(route, str) or not re.fullmatch(r"[0-9a-f]{64}", route)
                or not isinstance(key, str) or not key or scope not in {"post", "comment"}):
            raise WorkerError("请选择有效的整批待核对记录；本次未修改进度。")
        if not config.database.is_file():
            raise WorkerError("待核对数据库不存在，请刷新列表；本次未修改进度。")
        snapshot_key = (os.path.normcase(str(config.database.resolve())), route, scope, key)
        expected = self._pending_snapshots.get(snapshot_key)
        if expected is None:
            raise WorkerError("请先刷新并选择待核对列表中的整批记录；本次未解除阻塞。")
        with ExitStack() as stack:
            stack.enter_context(ProcessLock(Path(str(config.database) + ".lock")))
            state = State(config.database)
            stack.callback(state.close)
            with state.conn:
                state.conn.execute("BEGIN IMMEDIATE")
                row = state.conn.execute("SELECT operations.*,routes.signature FROM operations LEFT JOIN routes "
                                         "ON routes.id=operations.route WHERE route=? AND scope=? AND key=?",
                                         (route, scope, key)).fetchone()
                ids = self._pending_ids(row["source_ids"]) if row else []
                key_match = re.fullmatch(r"(post|comment):([0-9]+):([0-9]+(?:,[0-9]+){0,9})", key)
                canonical = (key_match is not None and key_match.group(1) == scope
                             and (key_match.group(2) == "0" if scope == "post" else 0 < int(key_match.group(2)) <= 2147483647)
                             and key == f"{scope}:{int(key_match.group(2))}:{','.join(map(str, ids))}")
                if (not ids or not canonical
                        or expected != (row["started_at"], row["source_ids"], row["signature"])):
                    raise WorkerError("记录已变动或源消息列表无效，请刷新列表；本次未解除阻塞。")
                mappings = [state.get_message(route, scope, identifier) for identifier in ids]
                completed = [mapping is not None and mapping.get("status") == "done"
                             and type(mapping.get("target_id")) is int and mapping["target_id"] > 0 for mapping in mappings]
                if any(mapping is not None for mapping in mappings) and not all(completed):
                    raise WorkerError("这批消息存在部分进度映射，不能直接解除；请保留记录并检查数据。")
                state.conn.execute("DELETE FROM operations WHERE route=? AND scope=? AND key=?", (route, scope, key))
        self._pending_snapshots.pop(snapshot_key, None)
        self.status.emit("已解除选中批次的本地阻塞；未删除目标内容，也未发送消息。已保存的进度和发送配额保留。")
        return "pending_resolved"

    async def _cleanup_downloads(self, config):
        self._task_status({"phase": "cleanup", "message": "正在检查当前下载目录内已确认转发的文件…"})
        if not config.database.is_file():
            result = {"deleted_files": 0, "freed_bytes": 0, "missing_files": 0,
                      "skipped_files": 0, "failed_files": 0, "stopped": False,
                      "message": "当前没有下载记录，无需清理。"}
        else:
            with ExitStack() as stack:
                stack.enter_context(ProcessLock(Path(str(config.database) + ".lock")))
                state = State(config.database)
                stack.callback(state.close)
                result = cleanup_forwarded(state, roots=[config.download_dir],
                                           should_stop=self._stop_event.is_set)
            result["message"] = (f"已清理 {result.get('deleted_files', 0)} 个已转发文件，"
                                 f"释放 {result.get('freed_bytes', 0) / 1048576:.2f} MB。")
            if result.get("stopped"):
                result["message"] += "清理已停止，未处理文件保留。"
            elif result.get("skipped_files"):
                result["message"] += "未发送、待核对、其他目录或已变更的文件已保留。旧目录请切回后再清理。"
            if any("缺少文件身份" in reason for reason in result.get("skip_reasons", {})):
                result["message"] += "部分旧版记录缺少文件校验信息，已保留，请人工核对处理。"
            if result.get("failed_files"):
                result["message"] += "部分文件暂时无法删除，可关闭占用程序后重试。"
        self.cleanup_ready.emit(result)
        self._task_status({"phase": "cleaned", "message": result["message"]})
        return "cleaned"

    async def _list_chats(self, config):
        self._task_status({"phase": "loading_chats", "message": "正在检查当前账号并读取频道／群组列表…"})
        # Connection/authorization reads are cancellable too, but never request
        # a code automatically when the selected session is not authorized.
        await self._run_auth_operation(lambda: self._authorized(config))
        identity = self._identity
        # Only the timeout/stop gate is needed here. Do not open the user's
        # State database or create routes, cursors, quotas, or download records.
        gate = Mover(self._client, config, None, should_stop=self._stop_event.is_set,
                     on_status=self._task_status)
        gate.phase("loading_chats", "正在读取当前账号可见的频道／群组（含归档）…")
        rows, seen = [], set()
        # No folder filter: Telethon includes normal and archived dialogs.
        iterator = self._client.iter_dialogs().__aiter__()
        try:
            while True:
                try:
                    dialog = await gate._await_read(
                        lambda: anext(iterator), label="读取频道／群组列表")
                except StopAsyncIteration:
                    break
                entity = getattr(dialog, "entity", None)
                if not isinstance(entity, (types.Channel, types.Chat)):
                    continue
                identifier = utils.get_peer_id(entity)
                if identifier in seen:
                    continue
                seen.add(identifier)
                rows.append({"id": identifier, "title": getattr(entity, "title", "") or "未命名聊天",
                             "username": getattr(entity, "username", "") or "",
                             "forum": bool(getattr(entity, "forum", False))})
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()
        gate.checkpoint()
        if identity != self._identity:
            raise WorkerError("当前账号已改变，请重新读取该账号的频道／群组列表。")
        self.chats_ready.emit(rows)
        self._task_status({"phase": "chats", "message": f"已读取 {len(rows)} 个频道／群组；未发送内容。"})
        return "chats"

    async def _list_topics(self, config, peer):
        self._task_status({"phase": "loading_topics", "message": "正在检查当前账号并读取所选群组的话题…"})
        await self._run_auth_operation(lambda: self._authorized(config))
        identity = self._identity
        gate = Mover(self._client, config, None, should_stop=self._stop_event.is_set,
                     on_status=self._task_status)
        entity = await gate.resolve_entity(peer, role="话题所属")
        gate.phase("loading_topics", "正在读取所选群组的全部话题，包含通用话题…")
        rows = await list_forum_topics(self._client, entity, read=gate.read)
        gate.checkpoint()
        if identity != self._identity:
            raise WorkerError("当前账号已改变，请重新读取该账号的群组话题。")
        self.topics_ready.emit({"chat_id": utils.get_peer_id(entity), "topics": rows})
        self._task_status({"phase": "topics", "message": f"已读取 {len(rows)} 个话题；未发送内容。"})
        return "topics"

    async def _run_task(self, config, *, once, check_only):
        self._task_status({"phase": "authorizing", "message": "任务已开始：正在连接并检查已保存的用户会话…"})
        user = await self._authorized(config)
        effective = replace(config, poll_interval=0) if once else config
        self.stats.emit({"processed": 0, "sent": 0, "filtered": 0, "files": 0})
        self.status.emit("正在检查任务配置…" if check_only else "搬运任务运行中…")
        with ExitStack() as stack:
            stack.enter_context(ProcessLock(Path(str(config.database) + ".lock")))
            state = State(config.database)
            stack.callback(state.close)
            mover = self._mover(effective, state, user)
            try:
                if check_only:
                    await mover.prepare()
                else:
                    await mover.run()
            except (Exception, asyncio.CancelledError) as exc:
                # Cancellation and network/5xx errors can lose a successful
                # response. Never reconnect a sender with an old write queue.
                route = getattr(mover, "route_id", None)
                pending = state.pending(route) if route else []
                if isinstance(exc, SendOutcomeUnknown) or pending:
                    await self._disconnect()
                    if isinstance(exc, SendOutcomeUnknown):
                        raise
                    reason = self._friendly_error(exc)
                    raise SendOutcomeUnknown(
                        f"{reason}\n本次发送结果未确认，已断开原连接并保留待核对记录。"
                        "请到“运行记录 → 待核对发送”检查目标；不会自动重发。") from exc
                raise
        if check_only:
            final = {"phase": "checked", "message": "检查通过，未发送内容。"}
        else:
            final = {**(mover.summary or {}), "phase": "completed"}
            final.setdefault("message", "本次任务已结束，请查看本轮处理统计和日志；进度已保存。")
        self._task_status(final)
        return "checked" if check_only else "completed"

    async def _preview(self, config, limit):
        self._task_status({"phase": "previewing", "message": "正在连接用户会话并读取源消息预览…"})
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise WorkerError("预览条数须为 1 至 500 之间的整数。")
        user = await self._authorized(config)
        self.status.emit("正在读取源消息预览，不会发送或下载文件…")
        rows = []
        with ExitStack() as stack:
            stack.enter_context(ProcessLock(Path(str(config.database) + ".lock")))
            state = State(config.database)
            stack.callback(state.close)
            mover = self._mover(config, state, user)
            source = await mover.resolve_entity(config.source, role="源")
            if not isinstance(source, (types.Channel, types.Chat)):
                raise WorkerError("源必须是频道或群组。")
            source_topic = getattr(config, "source_topic_id", 0)
            if source_topic:
                await mover.check_topic(source, source_topic, "源")
                messages = list(await mover.read(lambda: self._client.get_messages(
                    source, limit=limit + 10, reply_to=source_topic)))
            else:
                messages = list(await mover.read(lambda: self._client.get_messages(source, limit=limit + 10)))
            selected = {message.id for message in messages[:limit]}
            ad_filter = AdFilter(config.filters)

            async def ordered():
                for message in sorted(messages, key=lambda value: value.id):
                    yield message

            async for batch in batches(ordered()):
                if self._stop_event.is_set():
                    raise UserStopped("预览已停止。")
                if not any(message.id in selected for message in batch):
                    continue
                decision = ad_filter.evaluate(batch, scope="post", mode=config.mode)
                for message in batch:
                    if not isinstance(message, types.Message):
                        continue
                    media = "图片" if message.photo else ("文件" if message.document else (type(message.media).__name__ if message.media else "文字"))
                    rows.append({
                        "source_id": message.id, "chat_id": utils.get_peer_id(source),
                        "grouped_id": message.grouped_id, "text": message.message or "",
                        "media": media, "blocked": decision.blocked,
                        "reasons": list(decision.reasons),
                        "source_title": getattr(source, "title", "") or "",
                    })
        rows.sort(key=lambda row: row["source_id"], reverse=True)
        self.preview_ready.emit(rows)
        self.status.emit(f"已读取 {len(rows)} 条预览；相册按完整组显示，未发送或下载。")
        return "preview"
