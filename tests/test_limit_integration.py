"""Exercise policy, retry, history and durable state together without waiting."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from telethon import errors, functions, utils

from test_engine import FakeClient, channel, message
from test_limits import FakeClock
from tgmover.config import Config
from tgmover.engine import Mover, SyncError
from tgmover.limits import AccountStopped, LimitSettings, LimitStop, Limiter, clear_stop
from tgmover.state import State


class IteratorFailureClient(FakeClient):
    def __init__(self, failures, *, yield_first=False):
        super().__init__()
        self.iterator_failures = list(failures)
        self.iterator_attempts = 0
        self.iterator_options = []
        self.yield_first = yield_first

    def iter_messages(self, entity, **kwargs):
        original = super().iter_messages(entity, **kwargs)
        self.iterator_attempts += 1
        self.iterator_options.append(kwargs)
        failure = self.iterator_failures.pop(0) if self.iterator_failures else None

        async def iterator():
            if failure and not self.yield_first:
                raise failure
            async for item in original:
                yield item
                if failure:
                    raise failure

        return iterator()


class SlowGroupClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.target = channel(2, broadcast=False)
        self.entities["target"] = self.target

    async def __call__(self, request):
        if isinstance(request, functions.channels.GetFullChannelRequest) and request.channel.id == 2:
            self.calls.append(("request", type(request).__name__))
            return SimpleNamespace(
                full_chat=SimpleNamespace(slowmode_seconds=30, slowmode_next_send_date=None), chats=[],
            )
        return await super().__call__(request)


class BoundaryFailureClient(FakeClient):
    """Fail the album-boundary read inside scan's own retry scope."""

    def __init__(self):
        super().__init__()
        self.boundary_attempts = 0
        self.iterator_attempts = 0

    async def get_messages(self, entity, **kwargs):
        if kwargs.get("offset_id"):
            self.boundary_attempts += 1
            raise errors.FloodWaitError(None, capture=4)
        return await super().get_messages(entity, **kwargs)

    def iter_messages(self, entity, **kwargs):
        self.iterator_attempts += 1
        return super().iter_messages(entity, **kwargs)


class LimitIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.state = State(self.base / "state.sqlite3")
        self.addCleanup(self.state.close)
        self.clock = FakeClock()
        self.client = FakeClient()
        self.settings = LimitSettings(
            min_send_interval=0, min_request_interval=0,
            max_messages_per_minute=0, max_messages_per_hour=0,
            max_messages_per_day=0, max_messages_per_run=0,
            max_runtime_minutes=0, wait_padding=3,
        )
        self.config = Config(
            api_id=12345, api_hash="a" * 32, source="source", target="target",
            mode="reupload", comment_mode="reupload", sync_comments=True,
            session=self.base / "session", database=self.base / "state.sqlite3",
            download_dir=self.base / "downloads", start_id=0, poll_interval=0,
            settle_seconds=2, max_flood_wait=300, send_delay=0, limits=self.settings,
        )

    def limiter(self, settings=None):
        return Limiter(
            self.state, 42, settings or self.settings,
            clock=self.clock.time, sleep=self.clock.sleep, monotonic=self.clock.monotonic,
        )

    async def mover(self, *, client=None, limit_overrides=None, **overrides):
        settings = replace(self.settings, **(limit_overrides or {}))
        mover = Mover(client or self.client, replace(self.config, limits=settings, **overrides),
                      self.state, limiter=self.limiter(settings))
        await mover.prepare()
        return mover

    def account(self):
        return dict(self.state.conn.execute("SELECT * FROM limit_accounts WHERE account_id=42").fetchone())

    def events(self):
        return [dict(row) for row in self.state.conn.execute(
            "SELECT * FROM limit_events WHERE account_id=42 ORDER BY rowid")]

    def assert_no_delivery(self, mover, scope="post", thread_id=0):
        self.assertEqual(self.state.cursor(mover.route_id, scope, thread_id), 0)
        self.assertEqual(self.state.messages(mover.route_id, scope), [])
        self.assertEqual(self.state.pending(mover.route_id), [])

    async def test_posts_album_and_comments_spend_the_same_account_budget(self):
        self.client.add(1, [message(1), message(2, media=True, grouped_id=77),
                            message(3, media=True, grouped_id=77)])
        self.client.link_post(1, 10)
        self.client.add(3, [message(11, 3, root=10)], root=10)
        mover = await self.mover()

        await mover.run_once()

        events = self.events()
        self.assertEqual([row["cost"] for row in events], [1, 2, 1])
        self.assertEqual({row["account_id"] for row in events}, {42})
        self.assertEqual([row["peer_id"] for row in events],
                         [utils.get_peer_id(self.client.target)] * 2 +
                         [utils.get_peer_id(self.client.target_link)])
        self.assertEqual(len(self.client.sends()), 3)
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 3)
        self.assertEqual(self.state.cursor(mover.route_id, "comment", 10), 11)
        self.assertEqual(self.clock.sleeps, [])

    async def test_send_flood_wait_retries_after_padding_and_charges_both_attempts(self):
        mover = await self.mover(sync_comments=False)
        self.client.send_failure = errors.FloodWaitError(None, capture=4)

        def permit_retry():
            self.assertEqual(self.state.cursor(mover.route_id, "post"), 0)
            self.assertEqual(self.state.messages(mover.route_id, "post"), [])
            self.assertEqual(self.state.pending(mover.route_id), [])
            self.client.send_failure = None

        self.clock.on_sleep = permit_retry
        await mover.process([message(1)], "post", "reupload")

        self.assertEqual(self.clock.sleeps, [7])
        self.assertEqual([row["cost"] for row in self.events()], [1, 1])
        self.assertEqual(len(self.client.sends()), 2)
        self.assertEqual(len(self.state.messages(mover.route_id, "post")), 1)
        self.assertEqual(self.state.cursor(mover.route_id, "post"), 1)
        self.assertEqual(self.state.pending(mover.route_id), [])

    async def test_slowmode_only_cools_its_peer_and_other_comment_peer_can_send(self):
        mover = await self.mover(limit_overrides={"retry_wait_errors": 0})
        target_id = utils.get_peer_id(self.client.target)
        self.client.send_failure = errors.SlowModeWaitError(None, capture=5)
        with self.assertRaises(SyncError):
            await mover.process([message(1)], "post", "reupload")

        self.assertEqual(self.account()["global_cooldown_until"], 0)
        cooldown = self.state.conn.execute(
            "SELECT * FROM limit_peers WHERE account_id=42 AND peer_id=?", (target_id,)
        ).fetchone()
        self.assertEqual(cooldown["cooldown_until"], self.clock.now + 8)
        self.client.send_failure = None
        await mover.process([message(11, 3, root=10)], "comment", "reupload", context={
            "source_post_id": 1, "source_root_id": 10, "target_root_id": 10101,
        })
        self.assertEqual(self.clock.sleeps, [])
        await mover.process([message(2)], "post", "reupload")
        self.assertEqual(self.clock.sleeps, [8])
        self.assertEqual([row["cost"] for row in self.events()], [1, 1, 1])

    async def test_long_flood_is_persisted_and_blocks_a_restarted_mover(self):
        mover = await self.mover(sync_comments=False, max_flood_wait=10,
                                 limit_overrides={"max_auto_wait": 10})
        self.client.send_failure = errors.FloodWaitError(None, capture=20)
        with self.assertRaises(SyncError):
            await mover.process([message(1)], "post", "reupload")

        self.assertEqual(self.account()["global_cooldown_until"], self.clock.now + 23)
        self.assert_no_delivery(mover)
        self.assertEqual(len(self.events()), 1)
        self.client.send_failure = None
        with self.assertRaises(LimitStop):
            await self.mover(sync_comments=False, max_flood_wait=10,
                             limit_overrides={"max_auto_wait": 10})
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(len(self.client.sends()), 1)
        self.assertEqual(len(self.events()), 1)

    async def assert_account_stop(self, failure):
        mover = await self.mover(sync_comments=False)
        self.client.send_failure = failure
        with self.assertRaises(AccountStopped):
            await mover.process([message(1)], "post", "reupload")
        self.assertEqual(len(self.client.sends()), 1)
        self.assert_no_delivery(mover)
        self.assertEqual(len(self.events()), 1)
        self.assertTrue(self.account()["stopped_reason"])
        with self.assertRaises(AccountStopped):
            await self.mover(sync_comments=False)
        mover.limiter.cooldown(40, reason="existing global wait")
        mover.limiter.cooldown(50, peer_id=999, reason="existing peer wait")
        before = self.account()["global_cooldown_until"]
        clear_stop(self.state, 42)
        self.assertIsNone(self.account()["stopped_reason"])
        self.assertEqual(self.account()["global_cooldown_until"], before)
        self.assertEqual(len(self.events()), 1)
        peer = self.state.conn.execute("SELECT cooldown_until FROM limit_peers WHERE account_id=42 AND peer_id=999").fetchone()
        self.assertEqual(peer[0], self.clock.now + 53)
        self.assertEqual(self.clock.sleeps, [])

    async def test_peer_flood_stops_persistently_without_retry(self):
        await self.assert_account_stop(errors.PeerFloodError(None))

    async def test_frozen_420_stops_persistently_without_wait_retry(self):
        await self.assert_account_stop(errors.FrozenMethodInvalidError(None))

    async def test_unknown_420_is_not_a_retryable_wait_error(self):
        mover = await self.mover(sync_comments=False)
        self.client.send_failure = errors.FloodError(None, "UNKNOWN_420", code=420)

        with self.assertRaises(SyncError):
            await mover.process([message(1)], "post", "reupload")

        self.assertEqual(len(self.client.sends()), 1)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.account()["global_cooldown_until"], 0)
        self.assertIsNone(self.account()["stopped_reason"])
        self.assertEqual(self.clock.sleeps, [])
        self.assert_no_delivery(mover)

    async def test_local_quota_rejects_before_pending_and_does_not_advance(self):
        mover = await self.mover(sync_comments=False, limit_overrides={"max_messages_per_run": 1})
        await mover.limiter.reserve(utils.get_peer_id(self.client.target), 1)

        with self.assertRaises(LimitStop):
            await mover.process([message(1)], "post", "reupload")

        self.assertEqual(self.client.sends(), [])
        self.assertEqual(len(self.events()), 1)
        self.assert_no_delivery(mover)

    async def test_read_flood_persists_wait_and_retries_without_message_cost(self):
        mover = await self.mover(sync_comments=False)
        initial = self.clock.now
        attempts = 0

        async def read_once():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise errors.FloodWaitError(None, capture=4)
            return "read result"

        self.assertEqual(await mover.read(read_once), "read result")
        self.assertEqual(attempts, 2)
        self.assertEqual(self.clock.sleeps, [7])
        self.assertEqual(self.account()["global_cooldown_until"], initial + 7)
        self.assertEqual(self.events(), [])

    async def test_read_unknown_420_is_not_retried_or_interpreted_as_seconds(self):
        mover = await self.mover(sync_comments=False)
        attempts = 0

        async def read_once():
            nonlocal attempts
            attempts += 1
            raise errors.FloodError(None, "UNKNOWN_420", code=420)

        with self.assertRaises(errors.FloodError):
            await mover.read(read_once)
        self.assertEqual(attempts, 1)
        self.assertEqual(self.account()["global_cooldown_until"], 0)
        self.assertEqual(self.clock.sleeps, [])

    async def test_read_wait_exhaustion_stops_after_exact_retry_budget(self):
        mover = await self.mover(sync_comments=False)
        attempts = 0
        initial = self.clock.now

        async def read_once():
            nonlocal attempts
            attempts += 1
            raise errors.FloodWaitError(None, capture=4)

        with self.assertRaises(SyncError):
            await mover.read(read_once)

        self.assertEqual(attempts, 4)
        self.assertEqual(self.clock.sleeps, [7, 7, 7])
        self.assertEqual(self.account()["global_cooldown_until"], initial + 28)
        self.assertEqual(self.events(), [])

    async def test_iterator_flood_resumes_after_last_yielded_message(self):
        client = IteratorFailureClient([errors.FloodWaitError(None, capture=4)], yield_first=True)
        client.add(1, [message(1), message(2)])
        mover = await self.mover(client=client, sync_comments=False)

        result = [[item.id for item in group] async for group in mover.scan(mover.source, minimum=0)]

        self.assertEqual(result, [[1], [2]])
        self.assertEqual(client.iterator_attempts, 2)
        self.assertEqual([options["min_id"] for options in client.iterator_options], [0, 1])
        self.assertTrue(all(options["wait_time"] >= 1 for options in client.iterator_options))
        self.assertEqual(self.clock.sleeps, [7])
        self.assertEqual(self.events(), [])

    async def test_repeated_iterator_flood_stops_at_retry_budget_and_keeps_last_deadline(self):
        client = IteratorFailureClient([errors.FloodWaitError(None, capture=4) for _ in range(4)])
        client.add(1, [message(1)])
        mover = await self.mover(client=client, sync_comments=False)
        initial = self.clock.now

        with self.assertRaises(SyncError):
            _ = [group async for group in mover.scan(mover.source, minimum=0)]

        self.assertEqual(client.iterator_attempts, 4)
        self.assertEqual(self.clock.sleeps, [7, 7, 7])
        self.assertEqual(self.account()["global_cooldown_until"], initial + 28)
        self.assert_no_delivery(mover)
        self.assertEqual(self.events(), [])

    async def test_nested_album_boundary_waits_do_not_multiply_scan_retry_budget(self):
        client = BoundaryFailureClient()
        client.add(1, [message(1, grouped_id=77), message(2, grouped_id=77),
                       message(3, grouped_id=77)])
        mover = await self.mover(client=client, sync_comments=False)
        initial = self.clock.now

        with self.assertRaises(SyncError):
            _ = [group async for group in mover.scan(mover.source, minimum=1)]

        self.assertEqual(client.boundary_attempts, self.settings.retry_wait_errors + 1)
        self.assertEqual(client.iterator_attempts, 1)
        self.assertEqual(self.clock.sleeps, [7, 7, 7])
        self.assertEqual(self.account()["global_cooldown_until"], initial + 28)
        self.assertEqual(client.sends(), [])
        self.assertEqual(self.events(), [])
        self.assert_no_delivery(mover)

    async def test_iterator_unknown_420_is_not_retried(self):
        client = IteratorFailureClient([errors.FloodError(None, "UNKNOWN_420", code=420)])
        client.add(1, [message(1)])
        mover = await self.mover(client=client, sync_comments=False)

        with self.assertRaises(errors.FloodError):
            _ = [group async for group in mover.scan(mover.source, minimum=0)]

        self.assertEqual(client.iterator_attempts, 1)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(self.account()["global_cooldown_until"], 0)

    async def test_slowmode_group_rejects_forward_album_before_any_reservation(self):
        client = SlowGroupClient()
        mover = await self.mover(client=client, mode="forward", sync_comments=False)

        with self.assertRaisesRegex(SyncError, "相册未被拆分"):
            await mover.process([message(1, media=True, grouped_id=77),
                                 message(2, media=True, grouped_id=77)], "post", "forward")

        self.assertEqual(client.sends(), [])
        self.assertEqual(self.events(), [])
        self.assert_no_delivery(mover)


if __name__ == "__main__":
    unittest.main()
