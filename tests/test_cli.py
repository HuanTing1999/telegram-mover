"""Offline recovery and operating-system lock integration checks."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import main
from tgmover.engine import SyncError
from tgmover.limits import Limiter, LimitSettings, LimitStop
from tgmover.state import State


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.config = self.base / "config.toml"
        self.config.write_text('[sync]\nsource="@source"\ntarget="@target"\n', encoding="utf-8")
        self.database = self.base / "data" / "state.sqlite3"

    def invoke(self, *arguments):
        output = io.StringIO()
        with patch.dict("os.environ", {"API_ID": "12345", "API_HASH": "a" * 32}), \
                patch("sys.argv", ["main.py", "--config", str(self.config), *arguments]), \
                patch("main.logging.basicConfig"), patch("main.logging.error"), \
                patch("main.TelegramClient", side_effect=AssertionError("Unexpected Telegram connection")), \
                contextlib.redirect_stdout(output):
            result = main.main()
        return result, output.getvalue()

    def test_validate_configuration_creates_no_session_or_database(self):
        result, output = self.invoke("--validate-config")
        self.assertEqual(result, 0)
        self.assertIn("配置有效", output)
        self.assertFalse(self.database.exists())
        self.assertFalse((self.base / "data").exists())

    def test_show_pending_uses_database_only_and_keeps_operations(self):
        state = State(self.database)
        route = state.route({"test": 1})
        state.begin_operation(route, "post", "post:0:1", [1])
        state.close()
        result, output = self.invoke("--show-pending")
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output)[0]["key"], "post:0:1")
        state = State(self.database)
        try:
            self.assertEqual(len(state.pending(route)), 1)
        finally:
            state.close()

    def test_recovery_requires_unique_route_and_never_sends(self):
        state = State(self.database)
        routes = [state.route({"test": value}) for value in (1, 2)]
        for route in routes:
            state.begin_operation(route, "post", "post:0:1", [1])
        state.close()
        self.assertEqual(self.invoke("--retry-pending", "post:0:1")[0], 2)
        self.assertEqual(self.invoke("--retry-pending", "post:0:1", "--route", routes[0])[0], 0)
        state = State(self.database)
        try:
            self.assertEqual(state.pending(routes[0]), [])
            self.assertEqual(len(state.pending(routes[1])), 1)
        finally:
            state.close()

    def test_os_lock_blocks_second_holder_and_releases(self):
        path = self.base / "process.lock"
        with main.ProcessLock(path):
            with self.assertRaises(SyncError):
                with main.ProcessLock(path):
                    self.fail("Second holder acquired an exclusive lock")
        with main.ProcessLock(path):
            pass

    def seed_limits(self, account=42):
        state = State(self.database)
        try:
            limiter = Limiter(state, account, LimitSettings(enabled=False))
            with state.conn:
                state.conn.execute(
                    "INSERT INTO limit_events(account_id, peer_id, created_at, cost) VALUES (?, 100, ?, 7)",
                    (account, time.time()),
                )
            limiter.cooldown(120, reason="FloodWait")
            limiter.cooldown(180, peer_id=100, reason="SlowMode")
            limiter.stop(f"review account {account}")
        finally:
            state.close()

    def test_limits_status_is_offline_and_retains_attempts_cooldowns_and_stop(self):
        self.seed_limits()
        with patch("main.asyncio.run", side_effect=AssertionError("Offline status started asyncio")):
            result, output = self.invoke("--limits-status")
        self.assertEqual(result, 0)
        account = json.loads(output)[0]
        self.assertEqual(account["account_id"], 42)
        self.assertEqual((account["attempted_minute"], account["attempted_hour"], account["attempted_day"]), (7, 7, 7))
        self.assertEqual(account["stopped_reason"], "review account 42")
        self.assertGreater(account["global_cooldown_until"], time.time())
        self.assertEqual(account["peer_cooldowns"][0]["peer_id"], 100)

    def test_resume_account_clears_only_named_stop_and_preserves_budget_cooldowns(self):
        self.seed_limits(42)
        self.seed_limits(43)
        state = State(self.database)
        try:
            before = [tuple(row) for row in state.conn.execute("SELECT * FROM limit_events ORDER BY account_id")]
            global_before = dict(state.conn.execute("SELECT * FROM limit_accounts WHERE account_id=42").fetchone())
            peers_before = [tuple(row) for row in state.conn.execute("SELECT * FROM limit_peers ORDER BY account_id")]
        finally:
            state.close()
        with patch("main.asyncio.run", side_effect=AssertionError("Offline resume started asyncio")):
            result, output = self.invoke("--resume-account", "42")
        self.assertEqual(result, 0)
        self.assertIn("发送计数及服务器冷却时间保留", output)
        state = State(self.database)
        try:
            account = dict(state.conn.execute("SELECT * FROM limit_accounts WHERE account_id=42").fetchone())
            self.assertIsNone(account["stopped_reason"])
            self.assertIsNone(account["stopped_at"])
            self.assertEqual(account["global_cooldown_until"], global_before["global_cooldown_until"])
            self.assertEqual([tuple(row) for row in state.conn.execute("SELECT * FROM limit_events ORDER BY account_id")], before)
            self.assertEqual([tuple(row) for row in state.conn.execute("SELECT * FROM limit_peers ORDER BY account_id")], peers_before)
            self.assertEqual(state.conn.execute("SELECT stopped_reason FROM limit_accounts WHERE account_id=43").fetchone()[0], "review account 43")
        finally:
            state.close()
        self.assertEqual(self.invoke("--resume-account", "999")[0], 2)
        self.assertEqual(self.invoke("--resume-account", "42")[0], 2)

    def test_once_treats_limit_stop_as_normal_and_retains_committed_progress(self):
        self.config.write_text('[sync]\nsource="@source"\ntarget="@target"\npoll_interval=300\n', encoding="utf-8")
        state = State(self.database)
        route = state.route({"test": "normal limit pause"})
        state.save_messages(route, "post", [{"source_id": 7, "target_id": 70}], cursor=7)
        state.close()

        def stop_after_committed_message(coroutine):
            try:
                frame = coroutine.cr_frame.f_locals
                self.assertEqual(frame["config"].poll_interval, 0)
                frame["state"].save_messages(route, "post", [{"source_id": 8, "target_id": 80}], cursor=8)
            finally:
                coroutine.close()
            raise LimitStop("per-run quota reached")

        with patch("main.asyncio.run", side_effect=stop_after_committed_message):
            result, _ = self.invoke("--once")
        self.assertEqual(result, 0)
        state = State(self.database)
        try:
            self.assertEqual(state.cursor(route, "post"), 8)
            self.assertEqual(state.get_message(route, "post", 7)["target_id"], 70)
            self.assertEqual(state.get_message(route, "post", 8)["target_id"], 80)
        finally:
            state.close()


if __name__ == "__main__":
    unittest.main()
