"""Persistent account-wide send budgets, cooldowns, and conservative stops."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from .state import State

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LimitSettings:
    enabled: bool = True
    min_send_interval: float = 15.0
    min_request_interval: float = 1.0
    max_messages_per_minute: int = 20
    max_messages_per_hour: int = 200
    max_messages_per_day: int = 1000
    max_messages_per_run: int = 200
    max_runtime_minutes: float = 60.0
    max_auto_wait: float = 300.0
    retry_wait_errors: int = 3
    wait_padding: float = 3.0
    active_start: str = "00:00"
    active_end: str = "00:00"
    timezone: str = "+08:00"


class LimitError(RuntimeError):
    """A limit cannot be satisfied without changing the requested operation."""


class LimitStop(LimitError):
    """A normal run budget or automatic-wait boundary has been reached."""


class AccountStopped(LimitError):
    """This account was persistently stopped and needs manual review."""


def _schema(state: State) -> None:
    state.conn.executescript("""
        CREATE TABLE IF NOT EXISTS limit_accounts (
            account_id INTEGER PRIMARY KEY,
            last_send_at REAL,
            global_cooldown_until REAL NOT NULL DEFAULT 0,
            stopped_reason TEXT,
            stopped_at REAL,
            last_request_at REAL
        );
        CREATE TABLE IF NOT EXISTS limit_events (
            account_id INTEGER NOT NULL,
            peer_id INTEGER NOT NULL,
            created_at REAL NOT NULL,
            cost INTEGER NOT NULL CHECK(cost > 0)
        );
        CREATE INDEX IF NOT EXISTS limit_events_account_time
            ON limit_events(account_id, created_at);
        CREATE TABLE IF NOT EXISTS limit_peers (
            account_id INTEGER NOT NULL,
            peer_id INTEGER NOT NULL,
            cooldown_until REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(account_id, peer_id)
        );
    """)


def clear_stop(state: State, account_id: int) -> None:
    """Explicitly release an account stop; recorded budgets/cooldowns remain."""
    _schema(state)
    with state.conn:
        state.conn.execute(
            "UPDATE limit_accounts SET stopped_reason=NULL, stopped_at=NULL WHERE account_id=?",
            (account_id,),
        )


def status(state: State) -> list[dict]:
    """Return account summaries without a Telegram connection or active limiter."""
    _schema(state)
    now = time.time()
    result = []
    for row in state.conn.execute("SELECT * FROM limit_accounts ORDER BY account_id"):
        item = dict(row)
        for label, seconds in (("minute", 60), ("hour", 3600), ("day", 86400)):
            item[f"attempted_{label}"] = state.conn.execute(
                "SELECT COALESCE(SUM(cost), 0) FROM limit_events WHERE account_id=? AND created_at>?",
                (row["account_id"], now - seconds),
            ).fetchone()[0]
        item["peer_cooldowns"] = [dict(peer) for peer in state.conn.execute(
            "SELECT peer_id, cooldown_until FROM limit_peers WHERE account_id=? AND cooldown_until>? ORDER BY peer_id",
            (row["account_id"], now),
        )]
        result.append(item)
    return result


class Limiter:
    """Reserve attempted message cost immediately before a sending RPC.

    Reservations are never refunded, including rejected or uncertain sends.
    Instances share persisted account budgets through the same SQLite database;
    only the current run's cost and runtime are local to this instance. Reuse the
    same instance across polling rounds. No await occurs inside a transaction.
    """

    clear_stop = staticmethod(clear_stop)
    status = staticmethod(status)

    def __init__(
        self, state: State, account_id: int, settings: LimitSettings, *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        on_wait: Callable[[dict], None] | None = None,
    ):
        self.state, self.account_id, self.settings = state, account_id, settings
        self.clock, self.sleep, self.monotonic = clock, sleep, monotonic
        self.on_wait = on_wait
        self._started_at = monotonic()
        self._run_cost = 0
        self._timezone = self._parse_timezone(settings.timezone)
        self._active_start = self._parse_time(settings.active_start)
        self._active_end = self._parse_time(settings.active_end)
        _schema(state)
        with state.conn:
            state.conn.execute("INSERT OR IGNORE INTO limit_accounts(account_id) VALUES (?)", (account_id,))

    @staticmethod
    def _parse_timezone(value: str):
        match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", value)
        if not match or int(match[2]) > 23 or int(match[3]) > 59:
            raise LimitError("timezone 必须是固定 UTC 偏移，例如 '+08:00'。")
        minutes = int(match[2]) * 60 + int(match[3])
        return timezone(timedelta(minutes=minutes if match[1] == "+" else -minutes))

    @staticmethod
    def _parse_time(value: str) -> int:
        if not re.fullmatch(r"\d{2}:\d{2}", value):
            raise LimitError("active_start/active_end 必须为 HH:MM。")
        hour, minute = map(int, value.split(":"))
        if hour > 23 or minute > 59:
            raise LimitError("active_start/active_end 必须为有效的 HH:MM。")
        return hour * 60 + minute

    def _check_runtime(self) -> None:
        maximum = self.settings.max_runtime_minutes
        if self.settings.enabled and maximum and self.monotonic() - self._started_at >= maximum * 60:
            raise LimitStop("已达到本次最长运行时间；进度已保存，请下次运行继续。")

    def check_runtime(self) -> None:
        """Check a cooperative runtime boundary without making a reservation."""
        self._check_runtime()

    async def pause(self, seconds: float) -> None:
        """Polling/legacy pauses obey runtime without using max_auto_wait."""
        self._check_runtime()
        deadline = self.monotonic() + seconds
        while self.monotonic() < deadline:
            self._check_runtime()
            self._account()
            chunk = min(30.0, deadline - self.monotonic(), self._runtime_remaining())
            if chunk <= 0:
                self._check_runtime()
                raise LimitStop("已达到本次最长运行时间。")
            await self.sleep(chunk)
        self._check_runtime()

    def _runtime_remaining(self) -> float:
        if not self.settings.enabled or not self.settings.max_runtime_minutes:
            return math.inf
        return self.settings.max_runtime_minutes * 60 - (self.monotonic() - self._started_at)

    def _active_delay(self, now: float) -> float:
        start, end = self._active_start, self._active_end
        if start == end:
            return 0.0
        local = datetime.fromtimestamp(now, self._timezone)
        minute = local.hour * 60 + local.minute
        active = start <= minute < end if start < end else minute >= start or minute < end
        if active:
            return 0.0
        candidate = local.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
        if candidate.timestamp() <= now:
            candidate += timedelta(days=1)
        return candidate.timestamp() - now

    def _account(self):
        account = self.state.conn.execute(
            "SELECT * FROM limit_accounts WHERE account_id=?", (self.account_id,)
        ).fetchone()
        if account["stopped_reason"]:
            raise AccountStopped(f"账号 {self.account_id} 已停止：{account['stopped_reason']}。请人工核对后解除。")
        return account

    def _window_delay(self, now: float, seconds: int, maximum: int, cost: int) -> float:
        if not maximum:
            return 0.0
        rows = self.state.conn.execute(
            "SELECT created_at, cost FROM limit_events WHERE account_id=? AND created_at>? ORDER BY created_at",
            (self.account_id, now - seconds),
        ).fetchall()
        used = sum(row["cost"] for row in rows)
        if used + cost <= maximum:
            return 0.0
        for row in rows:
            used -= row["cost"]
            if used + cost <= maximum:
                return max(0.0, row["created_at"] + seconds - now)
        raise LimitError("单次消息组无法满足配置的发送预算，未拆分或发送。")

    async def _wait(self, delay: float, reasons: list[str], started: float, already_logged: bool) -> bool:
        elapsed = max(0.0, self.monotonic() - started)
        if elapsed + delay > self.settings.max_auto_wait:
            raise LimitStop(
                f"需要等待 {delay:.1f} 秒（{'、'.join(reasons)}），超过自动等待上限 "
                f"{self.settings.max_auto_wait:g} 秒；请稍后重新运行。"
            )
        if not already_logged:
            log.info("等待 %.1f 秒：%s", delay, "、".join(reasons))
        deadline = self.monotonic() + delay
        self._report_wait(max(0.0, deadline - self.monotonic()), reasons)
        while self.monotonic() < deadline:
            self._check_runtime()
            # A manual stop may be written by an operator during a long wait.
            self._account()
            chunk = min(30.0, deadline - self.monotonic(), self._runtime_remaining())
            if chunk <= 0:
                self._check_runtime()
                raise LimitStop("已达到本次最长运行时间。")
            await self.sleep(chunk)
            remaining = max(0.0, deadline - self.monotonic())
            if remaining > 0:
                self._report_wait(remaining, reasons)
        self._check_runtime()
        self._report_wait(0.0, reasons, complete=True)
        return True

    def _report_wait(self, remaining: float, reasons: list[str], *, complete: bool = False) -> None:
        """Synchronous UI notification; callback errors expose programming bugs."""
        if self.on_wait is not None:
            self.on_wait({
                "phase": "wait_complete" if complete else "waiting",
                "message": "等待结束" if complete else f"等待 {remaining:.1f} 秒：{'、'.join(reasons)}",
                "wait_seconds": float(remaining),
                "reasons": list(reasons),
            })

    async def reserve(self, peer_id: int, cost: int) -> None:
        """Atomically reserve N attempted messages, keeping albums indivisible.

        A send is also a request: this updates both last_send_at and
        last_request_at. Call read_wait() for separate read-only RPCs, not again
        for the sending RPC immediately after a successful reserve().
        """
        if isinstance(cost, bool) or not isinstance(cost, int) or cost <= 0:
            raise LimitError("发送成本必须是正整数；相册按成员消息数计算。")
        started, logged = self.monotonic(), False
        while True:
            self._check_runtime()
            now = self.clock()
            with self.state.conn:
                self.state.conn.execute("BEGIN IMMEDIATE")
                account = self._account()
                if self.settings.enabled:
                    quotas = (
                        self.settings.max_messages_per_minute, self.settings.max_messages_per_hour,
                        self.settings.max_messages_per_day, self.settings.max_messages_per_run,
                    )
                    if any(maximum and cost > maximum for maximum in quotas):
                        raise LimitError("单次相册/消息组超过一项发送预算，未拆分或发送；请调整预算。")
                    if self.settings.max_messages_per_run and self._run_cost + cost > self.settings.max_messages_per_run:
                        raise LimitStop("已达到本次消息发送上限；本组未发送，下次运行继续。")
                waits = [(account["global_cooldown_until"] - now, "账号全局冷却")]
                peer = self.state.conn.execute(
                    "SELECT cooldown_until FROM limit_peers WHERE account_id=? AND peer_id=?",
                    (self.account_id, peer_id),
                ).fetchone()
                if peer:
                    waits.append((peer["cooldown_until"] - now, "目标聊天慢速冷却"))
                if self.settings.enabled:
                    if account["last_send_at"] is not None:
                        waits.append((account["last_send_at"] + self.settings.min_send_interval - now, "最小发送间隔"))
                    if account["last_request_at"] is not None:
                        waits.append((account["last_request_at"] + self.settings.min_request_interval - now, "最小请求间隔"))
                    waits.extend((
                        (self._window_delay(now, 60, self.settings.max_messages_per_minute, cost), "滚动一分钟预算"),
                        (self._window_delay(now, 3600, self.settings.max_messages_per_hour, cost), "滚动一小时预算"),
                        (self._window_delay(now, 86400, self.settings.max_messages_per_day, cost), "滚动24小时预算"),
                        (self._active_delay(now), "允许发送时段"),
                    ))
                positive = [(delay, reason) for delay, reason in waits if delay > 0]
                if not positive:
                    self.state.conn.execute(
                        "INSERT INTO limit_events(account_id, peer_id, created_at, cost) VALUES (?, ?, ?, ?)",
                        (self.account_id, peer_id, now, cost),
                    )
                    self.state.conn.execute(
                        "UPDATE limit_accounts SET last_send_at=?, last_request_at=? WHERE account_id=?",
                        (now, now, self.account_id),
                    )
            if not positive:
                self._run_cost += cost
                return
            logged = await self._wait(max(delay for delay, _ in positive),
                                      [reason for _, reason in positive], started, logged)

    async def read_wait(self) -> None:
        """Reserve one read request interval without spending message budgets."""
        started, logged = self.monotonic(), False
        while True:
            self._check_runtime()
            now = self.clock()
            with self.state.conn:
                self.state.conn.execute("BEGIN IMMEDIATE")
                account = self._account()
                waits = [(account["global_cooldown_until"] - now, "账号全局冷却")]
                if self.settings.enabled and account["last_request_at"] is not None:
                    waits.append((account["last_request_at"] + self.settings.min_request_interval - now, "最小请求间隔"))
                positive = [(delay, reason) for delay, reason in waits if delay > 0]
                if not positive:
                    self.state.conn.execute(
                        "UPDATE limit_accounts SET last_request_at=? WHERE account_id=?", (now, self.account_id)
                    )
            if not positive:
                return
            logged = await self._wait(max(delay for delay, _ in positive),
                                      [reason for _, reason in positive], started, logged)

    def cooldown(self, seconds: float, peer_id: int | None = None, reason: str = "") -> None:
        """Persist a maximum deadline, adding the configured safety padding."""
        if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
            raise LimitError("冷却秒数必须是非负有限数字。")
        until = self.clock() + seconds + self.settings.wait_padding
        with self.state.conn:
            if peer_id is None:
                self.state.conn.execute(
                    "UPDATE limit_accounts SET global_cooldown_until=MAX(global_cooldown_until, ?) WHERE account_id=?",
                    (until, self.account_id),
                )
            else:
                self.state.conn.execute(
                    """INSERT INTO limit_peers(account_id, peer_id, cooldown_until) VALUES (?, ?, ?)
                       ON CONFLICT(account_id, peer_id) DO UPDATE SET cooldown_until=MAX(cooldown_until, excluded.cooldown_until)""",
                    (self.account_id, peer_id, until),
                )
        log.warning("已记录%s冷却 %.1f 秒%s", "全局" if peer_id is None else f"聊天 {peer_id}",
                    seconds + self.settings.wait_padding, f"：{reason}" if reason else "")

    def stop(self, reason: str) -> None:
        """Persist an account-wide stop, including when local limits are off."""
        reason = str(reason).strip() or "需要人工检查账号状态"
        with self.state.conn:
            self.state.conn.execute(
                "UPDATE limit_accounts SET stopped_reason=?, stopped_at=? WHERE account_id=?",
                (reason, self.clock(), self.account_id),
            )
        log.error("账号 %s 已停止：%s", self.account_id, reason)
