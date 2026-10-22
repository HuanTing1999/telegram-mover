"""Offline configuration and actual SQLite durability/transaction tests."""

import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tgmover.config import ConfigError, load_config
from tgmover.limits import LimitSettings
from tgmover.state import State


HASH = "a" * 32


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.path = self.base / "config.toml"
        clean_environment = {key: value for key, value in os.environ.items() if key not in {"API_ID", "API_HASH"}}
        self.environment = patch.dict(os.environ, clean_environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write(self, extra="", *, mode="reupload", target=True, credentials=True):
        self.path.write_text(
            "[telegram]\n"
            + (f'api_id = 12345\napi_hash = "{HASH}"\n' if credentials else "")
            + 'session = "state/account"\n[sync]\nsource = "@source"\n'
            + ('target = "@target"\n' if target else "")
            + f'mode = "{mode}"\n'
            + extra,
            encoding="utf-8",
        )

    def test_relative_paths_and_default_modes(self):
        self.write()
        config = load_config(self.path)
        self.assertEqual(config.session, (self.base / "state/account").resolve())
        self.assertEqual(config.database, (self.base / "data/state.sqlite3").resolve())
        self.assertEqual(config.comment_mode, "reupload")
        self.assertEqual(config.source, "@source")

    def test_environment_overrides_and_no_hardcoded_credentials(self):
        self.write(credentials=False)
        with self.assertRaises(ConfigError):
            load_config(self.path)
        with patch.dict(os.environ, {"API_ID": "987", "API_HASH": "b" * 32}):
            config = load_config(self.path)
        self.assertEqual(config.api_id, 987)
        self.assertEqual(config.api_hash, "b" * 32)

    def test_topic_ids_default_to_whole_group_and_preserve_general_or_specific_topic(self):
        self.write()
        self.assertEqual((load_config(self.path).source_topic_id, load_config(self.path).target_topic_id), (0, 0))
        self.write("source_topic_id = 1\ntarget_topic_id = 2147483647\n")
        config = load_config(self.path)
        self.assertEqual((config.source_topic_id, config.target_topic_id), (1, 2147483647))

    def test_topic_ids_reject_wrong_types_negative_and_out_of_range_values(self):
        for name in ("source_topic_id", "target_topic_id"):
            for value in ("-1", "true", "false", "1.5", '"12"', "2147483648"):
                with self.subTest(name=name, value=value):
                    self.write(f"{name} = {value}\n")
                    with self.assertRaises(ConfigError):
                        load_config(self.path)

    def test_forwarded_cleanup_defaults_off_and_requires_a_real_boolean(self):
        self.write()
        self.assertFalse(load_config(self.path).auto_cleanup_forwarded)
        self.write("[storage]\nauto_cleanup_forwarded = true\n")
        self.assertTrue(load_config(self.path).auto_cleanup_forwarded)
        for value in ("1", "0", '"true"', '"false"'):
            with self.subTest(value=value):
                self.write(f"[storage]\nauto_cleanup_forwarded = {value}\n")
                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_download_connections_defaults_and_valid_integer_range(self):
        self.write()
        self.assertEqual(load_config(self.path).download_connections, 4)
        for value in (1, 4, 8):
            with self.subTest(value=value):
                self.write(f"[storage]\ndownload_connections = {value}\n")
                self.assertEqual(load_config(self.path).download_connections, value)

    def test_download_connections_reject_bool_string_float_and_out_of_range(self):
        for value in ("true", "false", '"4"', "4.0", "0", "-1", "9"):
            with self.subTest(value=value):
                self.write(f"[storage]\ndownload_connections = {value}\n")
                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_download_only_requires_no_target_and_no_comment_send(self):
        self.write(mode="download_only", target=False)
        config = load_config(self.path)
        self.assertIsNone(config.target)
        self.assertEqual(config.comment_mode, "download_only")
        self.write('comment_mode = "reupload"\n', mode="download_only", target=False)
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_target_required_for_send_and_credentials_validated(self):
        self.write(target=False)
        with self.assertRaises(ConfigError):
            load_config(self.path)
        self.write()
        for environment in ({"API_ID": "0"}, {"API_HASH": "invalid"}, {"API_ID": ""}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment):
                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_validation_of_modes_bounds_and_interactive_login_only(self):
        for extra in (
            'comment_mode = "invalid"\n', 'start_id = -1\n', 'poll_interval = -1\n',
            'settle_seconds = 1\n', 'send_delay = -1\n', 'max_flood_wait = -1\n',
            'sync_comments = "yes"\n', 'bot_token = "do-not-accept"\n',
            'phone = "+861234"\n', 'password = "do-not-store"\n',
        ):
            with self.subTest(extra=extra):
                self.write(extra)
                with self.assertRaises(ConfigError):
                    load_config(self.path)
        self.write(mode="invalid")
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_all_supported_modes(self):
        for mode in ("download_only", "forward", "reupload", "download_then_forward"):
            with self.subTest(mode=mode):
                self.write(mode=mode)
                self.assertEqual(load_config(self.path).mode, mode)

    def test_limits_defaults_and_valid_configuration(self):
        self.write()
        self.assertEqual(load_config(self.path).limits, LimitSettings())
        self.write('''[limits]
enabled = false
min_send_interval = 2.5
min_request_interval = 0
max_messages_per_minute = 0
max_messages_per_hour = 300
max_messages_per_day = 1500
max_messages_per_run = 40
max_runtime_minutes = 45.5
max_auto_wait = 0
retry_wait_errors = 10
wait_padding = 5
active_start = "22:00"
active_end = "02:00"
timezone = "-14:00"
''')
        self.assertEqual(load_config(self.path).limits, LimitSettings(
            enabled=False, min_send_interval=2.5, min_request_interval=0,
            max_messages_per_minute=0, max_messages_per_hour=300,
            max_messages_per_day=1500, max_messages_per_run=40,
            max_runtime_minutes=45.5, max_auto_wait=0, retry_wait_errors=10,
            wait_padding=5, active_start="22:00", active_end="02:00", timezone="-14:00",
        ))

    def test_limits_reject_negative_nonfinite_and_wrong_numeric_types(self):
        integers = ("max_messages_per_minute", "max_messages_per_hour", "max_messages_per_day",
                    "max_messages_per_run", "retry_wait_errors")
        numbers = ("min_send_interval", "min_request_interval", "max_runtime_minutes",
                   "max_auto_wait", "wait_padding")
        cases = [(name, value) for name in integers + numbers for value in ("-1", "true")]
        cases += [(name, value) for name in numbers for value in ("nan", "inf", "-inf")]
        cases += [(name, "1.5") for name in integers]
        for name, value in cases:
            with self.subTest(name=name, value=value):
                self.write(f"[limits]\n{name} = {value}\n")
                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_limits_time_formats_timezone_boundaries_retry_cap_and_unknown_keys(self):
        invalid = (
            'active_start = "9:00"', 'active_start = "24:00"',
            'active_end = "23:60"', 'active_end = 2200',
            'timezone = "+14:01"', 'timezone = "-14:01"',
            'timezone = "+15:00"', 'timezone = "-15:00"',
            'timezone = "+08"', 'timezone = "+00:60"', 'timezone = "UTC"',
            'retry_wait_errors = 11', 'enabled = 1', 'max_message_per_day = 1',
        )
        for setting in invalid:
            with self.subTest(setting=setting):
                self.write(f"[limits]\n{setting}\n")
                with self.assertRaises(ConfigError):
                    load_config(self.path)
        for zone in ("-14:00", "+14:00", "+13:59", "-00:30", "+00:00"):
            with self.subTest(zone=zone):
                self.write(f'[limits]\ntimezone = "{zone}"\nretry_wait_errors = 0\n')
                self.assertEqual(load_config(self.path).limits.timezone, zone)

    def test_changing_limits_reuses_actual_mover_route_and_progress(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from telethon import types
        from tgmover.engine import Mover

        self.write("sync_comments = false\n")
        first_config = load_config(self.path)
        self.write("sync_comments = false\n[limits]\nmin_send_interval = 60\nmax_messages_per_run = 10\n")
        second_config = load_config(self.path)
        self.assertNotEqual(first_config.limits, second_config.limits)
        state = State(self.base / "route.sqlite3")
        source = types.Channel(1, "source", types.ChatPhotoEmpty(), None, broadcast=True)
        target = types.Channel(2, "target", types.ChatPhotoEmpty(), None, broadcast=True)

        async def prepare(config):
            client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=42)))
            mover = Mover(client, config, state,
                          limiter=SimpleNamespace(read_wait=AsyncMock(), check_runtime=lambda: None))
            mover.resolve_entity = AsyncMock(side_effect=[source, target])
            await mover.prepare()
            return mover.route_id

        try:
            first = asyncio.run(prepare(first_config))
            state.save_messages(first, "post", [{"source_id": 10, "target_id": 100}], cursor=10)
            second = asyncio.run(prepare(second_config))
            third = asyncio.run(prepare(replace(second_config, download_connections=8)))
            self.assertEqual(first, second)
            self.assertEqual(first, third)
            self.assertEqual(state.cursor(second, "post"), 10)
            self.assertEqual(state.get_message(second, "post", 10)["target_id"], 100)
            self.assertEqual(state.conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0], 1)
        finally:
            state.close()


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.state = State(self.path)
        self.addCleanup(lambda: self.state.close())
        self.route = self.state.route({"source": -1001, "target": -1002, "mode": "reupload"})

    def test_pragmas_and_route_stability_isolation(self):
        self.assertEqual(self.state.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.state.conn.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertEqual(self.state.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.route, self.state.route({"mode": "reupload", "target": -1002, "source": -1001}))
        other = self.state.route({"source": -1001, "target": -1003, "mode": "reupload"})
        self.state.save_messages(self.route, "posts", [{"source_id": 1, "target_id": 10}], cursor=1)
        self.assertIsNone(self.state.get_message(other, "posts", 1))
        self.assertEqual(self.state.cursor(other, "posts"), 0)

    def test_cursor_is_monotonic_and_scoped_to_thread(self):
        self.state.set_cursor(self.route, "comments", 20, thread_id=1)
        self.state.set_cursor(self.route, "comments", 5, thread_id=1)
        self.assertEqual(self.state.cursor(self.route, "comments", thread_id=1), 20)
        self.assertEqual(self.state.cursor(self.route, "comments", thread_id=2), 0)
        with self.assertRaises(ValueError):
            self.state.set_cursor(self.route, "comments", -1)

    def test_batch_and_cursor_roll_back_together(self):
        self.state.save_messages(self.route, "posts", [{"source_id": 1}], cursor=1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.state.save_messages(self.route, "posts", [
                {"source_id": 2, "target_id": 200}, {"source_id": None, "target_id": 300},
            ], cursor=3)
        self.assertIsNone(self.state.get_message(self.route, "posts", 2))
        self.assertEqual(self.state.cursor(self.route, "posts"), 1)
        with self.assertRaises(ValueError):
            self.state.save_messages(self.route, "posts", [{"source_id": 4}], cursor=-1)
        self.assertIsNone(self.state.get_message(self.route, "posts", 4))

    def test_messages_threads_downloads_and_pending_persist(self):
        self.state.save_messages(self.route, "posts", [{
            "source_id": 1, "target_id": 2, "grouped_id": 777,
            "source_chat_id": -1001, "target_chat_id": -1002,
            "source_root_id": 3, "target_root_id": 4, "source_post_id": 1,
            "text": "你好", "metadata": {"album": [1, 2]},
        }], cursor=1)
        self.state.save_thread(self.route, 1, 3, 2, 4, -1003, -1004)
        self.state.save_download(self.route, "posts", 1, self.path.parent / "media.bin", 42)
        self.state.begin_operation(self.route, "posts", "album:777", [1, 2])
        other = State(self.path)
        try:
            self.assertEqual(other.get_message(self.route, "posts", 1)["metadata"], {"album": [1, 2]})
            self.assertEqual(other.messages(self.route, "posts")[0]["status"], "done")
            self.assertEqual(other.get_thread(self.route, 1)["target_root_id"], 4)
            self.assertEqual(len(other.threads(self.route)), 1)
            self.assertEqual(other.download(self.route, "posts", 1)["size"], 42)
            self.assertEqual(other.pending(self.route)[0]["source_ids"], [1, 2])
        finally:
            other.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.state.begin_operation(self.route, "posts", "album:777", [1, 2])
        self.assertEqual(len(self.state.pending(self.route)), 1)
        self.state.finish_operation(self.route, "posts", "album:777")
        self.assertEqual(self.state.pending(self.route), [])


if __name__ == "__main__":
    unittest.main()
