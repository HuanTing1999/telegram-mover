"""Offline pending-operation review through the real background GUI worker."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from test_gui_worker import TrackedLock, TrackedState
from tgmover import gui_worker
from tgmover.config import Config
from tgmover.limits import LimitSettings, Limiter
from tgmover.state import State


class PendingWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = Config(api_id=12345, api_hash="a" * 32, source="source", target="target",
                             mode="forward", comment_mode="reupload", sync_comments=True,
                             session=self.root / "account", database=self.root / "state.sqlite3",
                             download_dir=self.root / "downloads", start_id=0, poll_interval=0,
                             settle_seconds=0, max_flood_wait=300, send_delay=0,
                             limits=LimitSettings(enabled=False))
        self.finished, self.failures, self.rows, self.statuses = [], [], [], []
        self.busy_values = []
        self.release_gates = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        TrackedState.instances = []
        TrackedLock.active = set()
        TrackedLock.events = []
        self.network_factory = Mock(side_effect=AssertionError("Pending review must never create a Telegram client"))
        self.stack.enter_context(patch.object(gui_worker, "TelegramClient", self.network_factory))
        self.stack.enter_context(patch.object(gui_worker, "State", TrackedState))
        self.stack.enter_context(patch.object(gui_worker, "ProcessLock", TrackedLock))
        self.controller = gui_worker.GuiController()
        self.controller.finished.connect(self.finished.append)
        self.controller.failed.connect(self.failures.append)
        self.controller.pending_ready.connect(self.rows.append)
        self.controller.status.connect(self.statuses.append)
        self.controller.busy_changed.connect(self.busy_values.append)
        self.addCleanup(self.dispose)

    def wait_for(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("The offline pending operation did not finish")

    def perform(self, action):
        before = len(self.finished)
        self.assertTrue(action())
        self.wait_for(lambda: len(self.finished) > before and not self.controller.busy)
        self.network_factory.assert_not_called()
        self.assert_released()
        return self.finished[-1]

    def assert_released(self):
        self.assertEqual(TrackedLock.active, set())
        for instance in TrackedState.instances:
            self.assertTrue(instance.closed)
            self.assertEqual(instance.created_thread, self.controller._thread.ident)
            self.assertEqual(instance.closed_thread, instance.created_thread)
            self.assertNotEqual(instance.created_thread, threading.get_ident())
        for _, path, owner in TrackedLock.events:
            self.assertEqual(path, Path(str(self.config.database) + ".lock"))
            self.assertEqual(owner, self.controller._thread.ident)

    def dispose(self):
        for gate in self.release_gates:
            gate.set()
        self.controller.shutdown()
        self.wait_for(lambda: "closed" in self.finished)
        self.controller._thread.join(timeout=2)
        self.assertFalse(self.controller._thread.is_alive())
        self.network_factory.assert_not_called()
        self.assert_released()

    def signature(self, *, target=2, **extra):
        return {"account": 42, "source": -1000000000001, "target": -(10**12 + target),
                "source_link": -1000000000003, "target_link": -1000000000004,
                "mode": "forward", "comment_mode": "reupload", **extra}

    def seed(self, *, ids=(1, 2), scope="post", key=None, extra=None):
        key = key or f"{scope}:{10 if scope == 'comment' else 0}:" + ",".join(map(str, ids))
        with_state = State(self.config.database)
        try:
            route = with_state.route(self.signature(**(extra or {})))
            with_state.begin_operation(route, scope, key, list(ids))
            with_state.save_messages(route, "post", [{"source_id": 90, "target_id": 900,
                                      "status": "done", "text": "preserve mapping", "metadata": {"audit": 1}}], cursor=90)
            with_state.save_thread(route, 90, 10, 900, 20, -1000000000003, -1000000000004)
            Limiter(with_state, 42, self.config.limits)
            with with_state.conn:
                with_state.conn.execute("UPDATE limit_accounts SET last_send_at=10, global_cooldown_until=9999999999, "
                                        "stopped_reason='audit stop', stopped_at=11, last_request_at=12 WHERE account_id=42")
                with_state.conn.execute("INSERT INTO limit_events(account_id,peer_id,created_at,cost) VALUES (42,-1000000000002,10,2)")
                with_state.conn.execute("INSERT OR REPLACE INTO limit_peers(account_id,peer_id,cooldown_until) VALUES (42,-1000000000002,9999999999)")
        finally:
            with_state.close()
        # Match the user flow: a whole operation must be listed before its
        # route/scope/key can be confirmed. The worker remembers that snapshot.
        self.list_rows()
        return route, key

    def snapshot(self, *, operations=False):
        tables = ["routes", "messages", "cursors", "threads", "downloads", "download_files",
                  "limit_accounts", "limit_events", "limit_peers"]
        if operations:
            tables.append("operations")
        with closing(sqlite3.connect(self.config.database)) as connection:
            return {table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables}

    def mutate(self, statement, parameters=()):
        with closing(sqlite3.connect(self.config.database)) as connection, connection:
            connection.execute(statement, parameters)

    def resolve(self, route, key, scope="post"):
        return self.perform(lambda: self.controller.resolve_pending(self.config, route, key, scope))

    def list_rows(self, config=None):
        self.assertEqual(self.perform(lambda: self.controller.list_pending(config or self.config)), "pending")
        return self.rows[-1]

    def test_missing_database_list_is_empty_without_creating_database_directory_or_lock(self):
        missing = replace(self.config, database=self.root / "missing" / "state.sqlite3")

        self.assertEqual(self.list_rows(missing), [])

        self.assertFalse(missing.database.parent.exists())
        self.assertEqual(TrackedState.instances, [])
        self.assertEqual(TrackedLock.events, [])

    def test_existing_database_list_uses_worker_thread_and_preserves_all_local_facts(self):
        route, key = self.seed()
        before = self.snapshot(operations=True)

        rows = self.list_rows()

        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["route"], rows[0]["scope"], rows[0]["key"], rows[0]["source_ids"]),
                         (route, "post", key, [1, 2]))
        self.assertEqual(self.snapshot(operations=True), before)
        self.assertIsNone(self.controller._client)
        self.assertTrue(TrackedState.instances)

    def test_resolve_only_selected_route_and_scope_even_when_other_operations_share_key(self):
        route, key = self.seed()
        state = State(self.config.database)
        try:
            other_route = state.route(self.signature(target=9))
            state.begin_operation(other_route, "post", key, [1, 2])
            # A malformed imported row in another scope is still a different
            # operation and must never be removed with the selected post.
            state.begin_operation(route, "comment", key, [1, 2])
        finally:
            state.close()
        before = self.snapshot()

        self.assertEqual(self.resolve(route, key), "pending_resolved")

        rows = self.list_rows()
        self.assertEqual({(row["route"], row["scope"]) for row in rows},
                         {(other_route, "post"), (route, "comment")})
        self.assertEqual(self.snapshot(), before)

    def test_whole_album_resolves_once_then_refresh_has_no_pending_without_sending(self):
        route, key = self.seed(ids=(15220, 15221, 15222))
        self.assertEqual(self.list_rows()[0]["source_ids"], [15220, 15221, 15222])
        before = self.snapshot()

        self.assertEqual(self.resolve(route, key), "pending_resolved")

        self.assertEqual(self.list_rows(), [])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.failures, [])

    def test_already_complete_mapping_allows_stale_pending_clear_without_progress_or_budget_change(self):
        route, key = self.seed()
        state = State(self.config.database)
        try:
            state.save_messages(route, "post", [{"source_id": identifier, "target_id": 1000 + identifier,
                                                "grouped_id": 77, "status": "done"} for identifier in (1, 2)], cursor=2)
        finally:
            state.close()
        before = self.snapshot()

        self.assertEqual(self.resolve(route, key), "pending_resolved")

        self.assertEqual(self.list_rows(), [])
        self.assertEqual(self.snapshot(), before)

    def test_partial_album_mapping_with_one_missing_row_is_rejected_and_retained(self):
        route, key = self.seed()
        state = State(self.config.database)
        try:
            state.save_messages(route, "post", [{"source_id": 1, "target_id": 1001, "grouped_id": 77}])
        finally:
            state.close()
        before = self.snapshot(operations=True)

        self.assertEqual(self.resolve(route, key), "failed")

        self.assertEqual(self.snapshot(operations=True), before)
        self.assertIn("部分", self.failures[-1])

    def test_partial_target_mapping_is_not_complete_merely_because_both_rows_exist(self):
        route, key = self.seed()
        state = State(self.config.database)
        try:
            state.save_messages(route, "post", [{"source_id": 1, "target_id": 1001, "grouped_id": 77},
                                                {"source_id": 2, "target_id": None, "grouped_id": 77}])
        finally:
            state.close()
        before = self.snapshot(operations=True)

        self.assertEqual(self.resolve(route, key), "failed")

        self.assertEqual(self.snapshot(operations=True), before)

    def test_record_removed_after_listing_is_rejected_without_touching_other_facts(self):
        route, key = self.seed()
        self.list_rows()
        self.mutate("DELETE FROM operations WHERE route=? AND scope='post' AND key=?", (route, key))
        before = self.snapshot(operations=True)

        self.assertEqual(self.resolve(route, key), "failed")

        self.assertEqual(self.snapshot(operations=True), before)

    def test_changed_valid_album_ids_under_same_key_are_rejected(self):
        route, key = self.seed()
        self.list_rows()
        self.mutate("UPDATE operations SET source_ids='[3,4]' WHERE route=? AND scope='post' AND key=?", (route, key))
        before = self.snapshot(operations=True)

        self.assertEqual(self.resolve(route, key), "failed")

        self.assertEqual(self.snapshot(operations=True), before)

    def test_same_ids_with_changed_creation_time_cannot_reuse_an_older_confirmation(self):
        route, key = self.seed()
        self.mutate("UPDATE operations SET started_at='2099-01-02 03:04:05' WHERE route=? AND key=?", (route, key))
        before = self.snapshot(operations=True)

        self.assertEqual(self.resolve(route, key), "failed")

        self.assertEqual(self.snapshot(operations=True), before)

    def test_changed_route_signature_cannot_reuse_an_older_confirmation(self):
        route, key = self.seed()
        self.mutate("UPDATE routes SET signature=? WHERE id=?", (json.dumps(self.signature(target=99)), route))
        before = self.snapshot(operations=True)

        self.assertEqual(self.resolve(route, key), "failed")

        self.assertEqual(self.snapshot(operations=True), before)

    def test_invalid_source_id_lists_never_release_pending(self):
        route, key = self.seed()
        for values in ([0], [-1], [True], ["1"], [1, 1], [], list(range(1, 12)), [2147483648], {"id": 1}):
            with self.subTest(ids=values):
                self.mutate("UPDATE operations SET source_ids=? WHERE route=? AND key=?", (json.dumps(values), route, key))
                self.list_rows()
                before = self.snapshot(operations=True)

                self.assertEqual(self.resolve(route, key), "failed")

                self.assertEqual(self.snapshot(operations=True), before)

    def test_operation_key_scope_must_match_selected_database_scope(self):
        route, key = self.seed(key="comment:10:1,2", scope="post")
        before = self.snapshot(operations=True)

        self.assertEqual(self.resolve(route, key, "post"), "failed")

        self.assertEqual(self.snapshot(operations=True), before)

    def test_invalid_route_or_scope_cannot_modify_any_pending_row(self):
        route, key = self.seed()
        before = self.snapshot(operations=True)
        for candidate_route, scope in (("not-a-route", "post"), (route, "posts"), (route, "unknown")):
            with self.subTest(route=candidate_route, scope=scope):
                self.assertEqual(self.resolve(candidate_route, key, scope), "failed")
                self.assertEqual(self.snapshot(operations=True), before)

    def test_missing_database_resolve_does_not_create_it(self):
        self.assertEqual(self.resolve("a" * 64, "post:0:1"), "failed")
        self.assertFalse(self.config.database.exists())
        self.assertEqual(TrackedState.instances, [])
        self.assertEqual(TrackedLock.events, [])

    def test_comments_show_linked_groups_and_corresponding_roots_with_independent_mode(self):
        route, key = self.seed(ids=(31, 32), scope="comment",
                               extra={"source_topic_id": 111, "target_topic_id": 222})

        row, = self.list_rows()

        self.assertEqual((row["route"], row["scope"], row["key"], row["mode"]),
                         (route, "comment", key, "reupload"))
        self.assertIn("-1000000000003", row["source"])
        self.assertIn("评论根 10", row["source"])
        self.assertIn("-1000000000004", row["target"])
        self.assertIn("评论根 20", row["target"])
        self.assertEqual((row["source_topic_id"], row["target_topic_id"]), (0, 0))

    def test_busy_rejects_second_local_operation_and_never_resolves_selected_pending(self):
        route, key = self.seed()
        started, release = threading.Event(), threading.Event()
        self.release_gates.append(release)

        class GatedState(TrackedState):
            def __init__(self, path):
                started.set()
                if not release.wait(timeout=4):
                    raise AssertionError("Test did not release its local state gate")
                super().__init__(path)

        with patch.object(gui_worker, "State", GatedState):
            before = len(self.finished)
            previous_instances = len(TrackedState.instances)
            self.assertTrue(self.controller.list_pending(self.config))
            self.assertTrue(started.wait(timeout=1))
            self.assertTrue(self.controller.busy)
            self.assertFalse(self.controller.resolve_pending(self.config, route, key, "post"))
            release.set()
            self.wait_for(lambda: len(self.finished) > before and not self.controller.busy)

        self.assertEqual(self.finished[-1], "pending")
        self.assertEqual(len(TrackedState.instances), previous_instances + 1)
        self.assertEqual(len(self.list_rows()), 1)
        self.assertTrue(self.failures)

    def test_sqlite_delete_failure_rolls_back_and_releases_thread_state_and_lock(self):
        route, key = self.seed()
        self.mutate("CREATE TRIGGER refuse_pending_delete BEFORE DELETE ON operations "
                    "BEGIN SELECT RAISE(FAIL, 'offline pending delete failure'); END")
        before = self.snapshot(operations=True)

        self.assertEqual(self.resolve(route, key), "failed")

        self.assertEqual(self.snapshot(operations=True), before)
        self.assertEqual(len(self.list_rows()), 1)


if __name__ == "__main__":
    unittest.main()
