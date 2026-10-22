"""Offline deterministic account-wide policy and persistent-budget tests."""

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tgmover.limits import AccountStopped, LimitError, LimitSettings, LimitStop, Limiter, clear_stop, status
from tgmover.state import State


class FakeClock:
    def __init__(self, now=None):
        self.now = now or datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp()
        self.elapsed = 0.0
        self.sleeps = []
        self.on_sleep = None

    def time(self):
        return self.now

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        self.now += seconds
        self.elapsed += seconds

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)
        if self.on_sleep:
            self.on_sleep()


class LimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.state = State(self.path)
        self.addCleanup(self.state.close)
        self.clock = FakeClock()
        self.settings = LimitSettings(
            min_send_interval=0, min_request_interval=0,
            max_messages_per_minute=0, max_messages_per_hour=0,
            max_messages_per_day=0, max_messages_per_run=0,
            max_runtime_minutes=0, max_auto_wait=300, wait_padding=0,
        )

    def limiter(self, account=1, state=None, on_wait=None, **settings):
        return Limiter(
            state or self.state, account, replace(self.settings, **settings),
            clock=self.clock.time, sleep=self.clock.sleep, monotonic=self.clock.monotonic,
            on_wait=on_wait,
        )

    def events(self, account=1):
        return self.state.conn.execute(
            "SELECT * FROM limit_events WHERE account_id=? ORDER BY created_at", (account,)
        ).fetchall()

    async def test_send_and_read_intervals_share_persisted_request_clock(self):
        limiter = self.limiter(min_send_interval=15, min_request_interval=2)
        await limiter.reserve(100, 1)
        await limiter.read_wait()
        self.assertEqual(self.clock.sleeps, [2])
        await limiter.reserve(100, 1)
        self.assertEqual(self.clock.sleeps, [2, 13])
        await limiter.read_wait()
        self.assertEqual(self.clock.sleeps, [2, 13, 2])
        self.assertEqual(sum(row["cost"] for row in self.events()), 2)

    async def test_rolling_minute_and_album_cost_are_not_split(self):
        limiter = self.limiter(max_messages_per_minute=3)
        await limiter.reserve(100, 2)
        await limiter.reserve(100, 2)
        self.assertEqual(self.clock.elapsed, 60)
        self.assertEqual([row["cost"] for row in self.events()], [2, 2])
        self.assertTrue(all(seconds <= 30 for seconds in self.clock.sleeps))

    async def test_rolling_hour_and_day_expire_at_actual_event_times(self):
        limiter = self.limiter(max_messages_per_hour=3, max_messages_per_day=4, max_auto_wait=60)
        await limiter.reserve(100, 2)
        self.clock.advance(3601)
        await limiter.reserve(100, 2)
        with self.assertRaises(LimitStop):
            await limiter.reserve(100, 1)
        self.assertEqual(self.clock.sleeps, [])
        self.clock.advance(86400 - 3601)
        await limiter.reserve(100, 1)
        self.assertEqual([row["cost"] for row in self.events()], [2, 2, 1])

    async def test_per_run_budget_persists_across_rounds_but_resets_on_new_instance(self):
        limiter = self.limiter(max_messages_per_run=3)
        await limiter.reserve(100, 2)
        with self.assertRaises(LimitStop):
            await limiter.reserve(100, 2)
        self.assertEqual([row["cost"] for row in self.events()], [2])
        replacement = self.limiter(max_messages_per_run=3)
        await replacement.reserve(100, 2)
        self.assertEqual([row["cost"] for row in self.events()], [2, 2])

    async def test_restart_and_different_routes_share_account_window(self):
        self.state.route({"source": 1, "target": 2})
        self.state.route({"source": 3, "target": 4})
        await self.limiter(max_messages_per_minute=2).reserve(100, 2)
        second = State(self.path)
        try:
            restarted = self.limiter(state=second, max_messages_per_minute=2)
            await restarted.reserve(200, 1)
        finally:
            second.close()
        self.assertEqual(self.clock.elapsed, 60)
        self.assertEqual([row["peer_id"] for row in self.events()], [100, 200])

    async def test_different_accounts_are_isolated(self):
        first = self.limiter(account=1, max_messages_per_minute=1)
        second = self.limiter(account=2, max_messages_per_minute=1)
        await first.reserve(100, 1)
        first.cooldown(100, reason="FloodWait")
        first.stop("manual")
        await second.reserve(100, 1)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(len(self.events(2)), 1)

    async def test_global_cooldown_survives_restart_and_uses_max_plus_padding(self):
        limiter = self.limiter(wait_padding=3)
        limiter.cooldown(40, reason="FloodWait")
        limiter.cooldown(10, reason="shorter subsequent wait")
        replacement = self.limiter(wait_padding=3)
        await replacement.read_wait()
        self.assertEqual(self.clock.sleeps, [30, 13])
        self.assertEqual(self.events(), [])

    async def test_peer_cooldown_does_not_block_other_peer_but_global_does(self):
        limiter = self.limiter()
        limiter.cooldown(50, peer_id=100, reason="SlowMode")
        await limiter.reserve(200, 1)
        self.assertEqual(self.clock.sleeps, [])
        await limiter.reserve(100, 1)
        self.assertEqual(self.clock.elapsed, 50)
        limiter.cooldown(10)
        await limiter.reserve(200, 1)
        self.assertEqual(self.clock.elapsed, 60)

    async def test_disabled_local_limits_still_obey_stop_and_cooldowns(self):
        limiter = self.limiter(enabled=False, max_messages_per_run=1, min_send_interval=100)
        await limiter.reserve(100, 20)
        await limiter.reserve(100, 20)
        self.assertEqual(self.clock.sleeps, [])
        limiter.cooldown(5, peer_id=100)
        await limiter.reserve(100, 20)
        self.assertEqual(self.clock.elapsed, 5)
        limiter.stop("manual review")
        for operation in (lambda: limiter.reserve(200, 1), limiter.read_wait):
            with self.assertRaises(AccountStopped):
                await operation()
        clear_stop(self.state, 1)
        await limiter.read_wait()
        self.assertEqual(sum(row["cost"] for row in self.events()), 60)

    async def test_manual_stop_is_checked_between_sleep_chunks(self):
        limiter = self.limiter()
        limiter.cooldown(100)
        self.clock.on_sleep = lambda: limiter.stop("operator stop while waiting")
        with self.assertRaises(AccountStopped):
            await limiter.reserve(100, 1)
        self.assertEqual(self.clock.sleeps, [30])
        self.assertEqual(self.events(), [])

    async def test_active_hours_cross_midnight_in_fixed_timezone(self):
        # 13:59 UTC = 21:59 at +08:00; active from 22:00 through 02:00.
        self.clock.now = datetime(2026, 10, 20, 13, 59, tzinfo=timezone.utc).timestamp()
        limiter = self.limiter(active_start="22:00", active_end="02:00", timezone="+08:00")
        await limiter.reserve(100, 1)
        self.assertEqual(self.clock.elapsed, 60)
        self.clock.advance(3 * 3600)
        await limiter.reserve(100, 1)  # 01:00, still in the same overnight window.
        self.assertEqual(sum(self.clock.sleeps), 60)
        self.clock.advance(3600)
        with self.assertRaises(LimitStop):
            await limiter.reserve(100, 1)  # 02:00 is the exclusive end.

    async def test_equal_active_times_allow_full_day_and_negative_offset_works(self):
        full_day = self.limiter(active_start="12:34", active_end="12:34")
        await full_day.reserve(100, 1)
        self.assertEqual(self.clock.sleeps, [])
        self.clock.now = datetime(2026, 10, 20, 13, 59, tzinfo=timezone.utc).timestamp()
        offset = self.limiter(account=2, active_start="09:00", active_end="10:00", timezone="-05:00")
        await offset.reserve(100, 1)
        self.assertEqual(self.clock.elapsed, 60)

    async def test_runtime_budget_is_checked_during_wait_without_oversleep(self):
        limiter = self.limiter(max_runtime_minutes=0.75)
        limiter.cooldown(100)
        with self.assertRaises(LimitStop):
            await limiter.reserve(100, 1)
        self.assertEqual(self.clock.sleeps, [30, 15])
        self.assertEqual(self.events(), [])

    async def test_poll_pause_ignores_auto_wait_cap_but_obeys_runtime_and_chunks(self):
        limiter = self.limiter(max_runtime_minutes=0.75, max_auto_wait=1)
        with self.assertRaises(LimitStop):
            await limiter.pause(100)
        self.assertEqual(self.clock.sleeps, [30, 15])
        self.assertEqual(self.events(), [])
        self.clock.sleeps.clear()
        unlimited_runtime = self.limiter(max_runtime_minutes=0, max_auto_wait=1)
        await unlimited_runtime.pause(65)
        self.assertEqual(self.clock.sleeps, [30, 30, 5])
        self.assertEqual(self.events(), [])

    async def test_runtime_budget_also_stops_immediate_reads_and_sends(self):
        limiter = self.limiter(max_runtime_minutes=1)
        self.clock.advance(60)
        with self.assertRaises(LimitStop):
            await limiter.read_wait()
        with self.assertRaises(LimitStop):
            await limiter.reserve(100, 1)

    async def test_unfulfillable_album_budget_rejects_without_wait_or_partial_event(self):
        for setting in ("max_messages_per_minute", "max_messages_per_hour", "max_messages_per_day", "max_messages_per_run"):
            with self.subTest(setting=setting):
                limiter = self.limiter(**{setting: 2})
                with self.assertRaises(LimitError) as caught:
                    await limiter.reserve(100, 3)
                self.assertNotIsInstance(caught.exception, LimitStop)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(self.events(), [])

    async def test_long_wait_exits_before_sleep_and_preserves_cooldown(self):
        limiter = self.limiter(max_auto_wait=30)
        limiter.cooldown(31)
        with self.assertRaises(LimitStop):
            await limiter.reserve(100, 1)
        self.assertEqual(self.clock.sleeps, [])
        account = self.state.conn.execute("SELECT * FROM limit_accounts WHERE account_id=1").fetchone()
        self.assertEqual(account["global_cooldown_until"], self.clock.now + 31)

    async def test_repeated_deadline_extensions_cannot_exceed_total_auto_wait(self):
        limiter = self.limiter(max_auto_wait=40)
        limiter.cooldown(30)
        self.clock.on_sleep = lambda: limiter.cooldown(30)
        with self.assertRaises(LimitStop):
            await limiter.reserve(100, 1)
        self.assertEqual(self.clock.sleeps, [30])
        self.assertEqual(self.events(), [])

    async def test_second_connection_rechecks_budget_after_waiting(self):
        first = self.limiter(max_messages_per_minute=1, max_auto_wait=60)
        await first.reserve(100, 1)
        second = State(self.path)
        try:
            other = self.limiter(state=second, max_messages_per_minute=1, max_auto_wait=60)
            # Simulate a separate process consuming the newly free window while
            # this instance is asleep; its next transaction must read it again.
            def competing_reservation():
                if self.clock.elapsed == 60:
                    with second.conn:
                        second.conn.execute(
                            "INSERT INTO limit_events(account_id,peer_id,created_at,cost) VALUES (1,200,?,1)",
                            (self.clock.now,),
                        )
            self.clock.on_sleep = competing_reservation
            with self.assertRaises(LimitStop):
                await other.reserve(300, 1)
        finally:
            second.close()
        self.assertEqual([row["peer_id"] for row in self.events()], [100, 200])

    async def test_first_request_at_unix_zero_is_not_treated_as_preexisting_send(self):
        self.clock.now = 0
        limiter = self.limiter(min_send_interval=15, min_request_interval=1)
        await limiter.reserve(100, 1)
        self.assertEqual(self.clock.sleeps, [])
        await limiter.reserve(100, 1)
        self.assertEqual(self.clock.sleeps, [15])

    async def test_attempts_are_not_refunded_and_offline_status_aggregates(self):
        limiter = self.limiter()
        await limiter.reserve(100, 2)
        self.clock.advance(61)
        await limiter.reserve(200, 3)
        limiter.cooldown(20, peer_id=100)
        limiter.stop("review")
        with patch("tgmover.limits.time.time", return_value=self.clock.now):
            result = status(self.state)
        self.assertEqual(len(result), 1)
        account = result[0]
        self.assertEqual((account["attempted_minute"], account["attempted_hour"], account["attempted_day"]), (3, 5, 5))
        self.assertEqual(account["stopped_reason"], "review")
        self.assertEqual(account["peer_cooldowns"][0]["peer_id"], 100)

    async def test_wait_callback_reports_chunk_remaining_and_completion_without_extra_logs(self):
        updates = []
        limiter = self.limiter(on_wait=updates.append)
        limiter.cooldown(65)
        with patch("tgmover.limits.log.info") as info:
            await limiter.read_wait()
        self.assertEqual(self.clock.sleeps, [30, 30, 5])
        self.assertEqual([event["phase"] for event in updates], ["waiting", "waiting", "waiting", "wait_complete"])
        self.assertEqual([event["wait_seconds"] for event in updates], [65.0, 35.0, 5.0, 0.0])
        self.assertTrue(all(isinstance(event["wait_seconds"], float) for event in updates))
        self.assertTrue(all(event["reasons"] == ["账号全局冷却"] for event in updates))
        self.assertEqual(updates[-1]["message"], "等待结束")
        self.assertIn("账号全局冷却", updates[0]["message"])
        info.assert_called_once()
        self.assertEqual(self.events(), [])

    async def test_one_second_request_spacing_callback_does_not_mean_a_send(self):
        updates = []
        limiter = self.limiter(min_request_interval=1, on_wait=updates.append)
        await limiter.read_wait()
        self.assertEqual(updates, [])
        await limiter.read_wait()
        self.assertEqual([event["wait_seconds"] for event in updates], [1.0, 0.0])
        self.assertEqual(updates[0]["reasons"], ["最小请求间隔"])
        self.assertEqual(self.events(), [])

    async def test_wait_callback_is_not_emitted_when_auto_wait_is_refused(self):
        updates = []
        limiter = self.limiter(max_auto_wait=1, on_wait=updates.append)
        limiter.cooldown(2)
        with self.assertRaises(LimitStop):
            await limiter.read_wait()
        self.assertEqual(updates, [])
        self.assertEqual(self.clock.sleeps, [])

    async def test_runtime_interrupted_wait_has_no_false_completion_callback(self):
        updates = []
        limiter = self.limiter(max_runtime_minutes=0.75, on_wait=updates.append)
        limiter.cooldown(100)
        with self.assertRaises(LimitStop):
            await limiter.read_wait()
        self.assertEqual(self.clock.sleeps, [30, 15])
        self.assertFalse(any(event["phase"] == "wait_complete" for event in updates))
        self.assertEqual(self.events(), [])

    async def test_callback_exception_is_not_swallowed_or_charged_as_send(self):
        failure = RuntimeError("synthetic callback programming error")

        def broken_callback(_):
            raise failure

        limiter = self.limiter(on_wait=broken_callback)
        limiter.cooldown(10)
        with self.assertRaises(RuntimeError) as caught:
            await limiter.reserve(100, 1)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(self.events(), [])

    async def test_callback_receives_a_copy_of_reasons_for_each_update(self):
        observed = []

        def mutating_callback(event):
            observed.append(list(event["reasons"]))
            event["reasons"].append("UI-only temporary value")

        limiter = self.limiter(on_wait=mutating_callback)
        limiter.cooldown(31)
        await limiter.read_wait()
        self.assertEqual(observed, [["账号全局冷却"]] * 3)


if __name__ == "__main__":
    unittest.main()
