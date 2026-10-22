"""Ordered, resumable MTProto copying. No Bot API or media-reference reupload."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from telethon import errors, functions, types, utils

from .config import Config
from .acceleration import backend_info
from .cleanup import cleanup_forwarded
from .downloads import download_media as download_media_file
from .filters import AdFilter, FilterSettings
from .forum import ForumError, forward_to_topic, get_forum_topic
from .limits import AccountStopped, Limiter
from .peers import PeerReferenceError, parse_chat_reference
from .state import State

log = logging.getLogger(__name__)
READ_TIMEOUT_SECONDS = 45.0
DOWNLOAD_IDLE_SECONDS = 120.0
READ_POLL_SECONDS = 0.1
HEARTBEAT_SECONDS = 10.0
SEND_RPC_TIMEOUT_SECONDS = 90.0
UPLOAD_IDLE_SECONDS = 120.0
SEND_POLL_SECONDS = 0.1
SEND_CLOSE_TIMEOUT_SECONDS = 5.0


class SyncError(RuntimeError):
    pass


class SendOutcomeUnknown(SyncError):
    """A write may have reached Telegram; keep its journal and never replay."""


class UserStopped(SyncError):
    """Cooperative stop at a boundary where completed mappings are durable."""


async def batches(iterator) -> AsyncIterator[list]:
    """Keep adjacent grouped_id members together, in ascending message order."""
    batch = []
    async for message in iterator:
        if batch and (not message.grouped_id or message.grouped_id != batch[0].grouped_id):
            yield batch
            batch = []
        batch.append(message)
        if not message.grouped_id:
            yield batch
            batch = []
    if batch:
        yield batch


def thread_root(messages, linked_id: int):
    """Discussion responses include channel messages: never use those as roots."""
    candidates = [m for m in messages if isinstance(m, types.Message)
                  and utils.get_peer_id(m.peer_id) == linked_id]
    return min(candidates, key=lambda m: m.id) if candidates else None


class Mover:
    def __init__(self, client, config: Config, state: State, *, limiter=None, should_stop=None,
                 on_progress=None, on_status=None):
        self.client, self.config, self.state = client, config, state
        self.source = self.target = self.source_link = self.target_link = None
        self.source_topic_id = getattr(config, "source_topic_id", 0)
        self.target_topic_id = getattr(config, "target_topic_id", 0)
        self.route_id = ""
        self.limiter = limiter
        self.peer_slowmode = {}
        self.should_stop = should_stop or (lambda: False)
        self.on_progress = on_progress
        self.on_status = on_status
        self._stage = {"phase": "preparing", "message": "正在准备搬运任务…"}
        self._post_scan = {}
        self._deferred_scopes = set()
        self.summary = None
        self.cleanup_counts = {"deleted_files": 0, "freed_bytes": 0}
        self._send_client_unusable = False
        self._upload_activity_at = 0.0
        self.ad_filter = AdFilter(getattr(config, "filters", FilterSettings()))
        self.counts = {"processed": 0, "sent": 0, "filtered": 0, "files": 0}

    def _emit_status(self, event):
        if self.on_status:
            self.on_status({"counts": dict(self.counts), **event})

    def phase(self, phase, message, **details):
        self._stage = {"phase": phase, "message": message, **details}
        self._emit_status(self._stage)

    def waiting(self, event):
        # Limit waits temporarily cover the active step, then restore it.
        self._emit_status(self._stage if event["phase"] == "wait_complete" else event)

    async def _await_read(self, factory, *, label=None, timeout=None, activity=None):
        """Bound only read-only work, including iterator pages and downloads.

        Telethon's connection timeout does not bound an awaited RPC. Cancelling
        reads is safe; cancelling a send could lose an accepted result, so sends
        use a separate, non-cancelling heartbeat below.
        """
        self.checkpoint()
        timeout = READ_TIMEOUT_SECONDS if timeout is None else timeout
        label = label or self._stage["message"].rstrip("…")
        clock = asyncio.get_running_loop().time
        started = last_heartbeat = clock()
        task = asyncio.ensure_future(factory())
        try:
            while not task.done():
                self.checkpoint()
                if self.limiter:
                    self.limiter.check_runtime()
                now = clock()
                last_activity = activity() if activity else started
                remaining = timeout - (now - last_activity)
                if remaining <= 0:
                    raise SyncError(f"{label}：连续 {timeout:g} 秒未收到响应或下载进度，已停止。"
                                    "请检查 Telegram 网络连接后重试；本次读取未发送内容，进度未越过未完成消息。")
                if now - last_heartbeat >= HEARTBEAT_SECONDS:
                    self._emit_status({**self._stage,
                                       "message": f"{label}，已等待 {now - started:.0f} 秒（可安全停止）"})
                    last_heartbeat = now
                await asyncio.wait({task}, timeout=min(READ_POLL_SECONDS, remaining))
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _await_send(self, factory, *, timeout=None, activity=None):
        """Bound uncertain writes without treating a lost reply as rejection.

        A normal stop waits for this request. On timeout the client is closed
        and must be discarded: Telethon's cancelled futures can still leave
        requests in its reconnect queue. The operation journal stays pending.
        """
        clock = asyncio.get_running_loop().time
        started = last_heartbeat = clock()
        timeout = SEND_RPC_TIMEOUT_SECONDS if timeout is None else timeout
        self._upload_activity_at = started
        stop_reported = False
        task = asyncio.ensure_future(factory())
        try:
            while not task.done():
                now = clock()
                last_activity = activity() if activity is not None else started
                remaining = timeout - (now - last_activity)
                if remaining <= 0:
                    if task.done():
                        break
                    task.cancel()
                    await self._close_uncertain_client()
                    label = self._stage["message"].rstrip("…")
                    message = (f"{label}：连续 {timeout:g} 秒未收到"
                               + ("上传进度或发送确认" if activity is not None else "Telegram 发送确认")
                               + "，本次结果无法确认，已停止并保留待核对记录。"
                                 "请到“运行记录 → 待核对发送”检查目标；本次不会自动重发。")
                    self.phase("uncertain", message)
                    raise SendOutcomeUnknown(message)
                await asyncio.wait({task}, timeout=min(SEND_POLL_SECONDS, HEARTBEAT_SECONDS, remaining))
                stopping = self.should_stop()
                if not task.done() and ((stopping and not stop_reported)
                                        or clock() - last_heartbeat >= HEARTBEAT_SECONDS):
                    last_heartbeat = clock()
                    stop_reported = stop_reported or stopping
                    hint = "已请求停止，等待当前发送结果后保存进度。" if stopping else "请勿重复启动。"
                    text = f"等待 Telegram 返回当前发送结果（已等待 {clock() - started:.0f} 秒）。{hint}"
                    self._emit_status({**self._stage, "message": text, "elapsed_seconds": clock() - started})
                    log.info("%s", text)
            return await task
        except (OSError, asyncio.CancelledError):
            await self._close_uncertain_client()
            raise
        except errors.RPCError as exc:
            if abs(getattr(exc, "code", 0)) >= 500:
                await self._close_uncertain_client()
            raise
        finally:
            # Only external cancellation reaches here. The pending marker is
            # retained by process(); never replay an uncertain write.
            if not task.done():
                task.cancel()
                await asyncio.wait({task}, timeout=SEND_CLOSE_TIMEOUT_SECONDS)
            if task.done():
                # Retrieve cancelled/failed children without replacing the
                # actual error that is already propagating to the caller.
                try:
                    task.exception()
                except asyncio.CancelledError:
                    pass
            else:
                task.add_done_callback(lambda done: None if done.cancelled() else done.exception())

    async def _close_uncertain_client(self):
        self._send_client_unusable = True
        disconnect = getattr(self.client, "disconnect", None)
        if callable(disconnect):
            try:
                await asyncio.wait_for(disconnect(), timeout=SEND_CLOSE_TIMEOUT_SECONDS)
            except (Exception, asyncio.CancelledError):
                log.warning("发送结果未确认，原连接已停用；不自动重发。")

    def checkpoint(self):
        if self._send_client_unusable:
            raise SendOutcomeUnknown("上次发送结果未确认，原连接已停用；请先到“运行记录 → 待核对发送”核对，不能复用旧连接重试。")
        if self.should_stop():
            raise UserStopped("已安全停止；已完成的消息映射和进度已保存。")

    def progress(self, scope, rows, *, files=0):
        self.counts["processed"] += len(rows)
        self.counts["sent"] += sum(row.get("target_id") is not None for row in rows)
        self.counts["filtered"] += sum(row.get("status") == "filtered_ad" for row in rows)
        self.counts["files"] += files
        if self.on_progress:
            self.on_progress({**self.counts, "scope": scope,
                              "last_source_id": max(row["source_id"] for row in rows)})

    def account_failure(self, exc):
        name = type(exc).__name__
        severe = {"PeerFloodError", "UserRestrictedError", "UserDeactivatedError",
                  "UserDeactivatedBanError", "PhoneNumberBannedError", "SessionRevokedError",
                  "SessionExpiredError", "AuthKeyUnregisteredError", "AuthKeyInvalidError",
                  "AuthKeyDuplicatedError"}
        raw = str(getattr(exc, "message", ""))
        if name in severe or name.startswith("Frozen") or raw.startswith(("FROZEN_", "PEER_FLOOD")):
            return f"{name}：账号/会话受限，请先通过 Telegram 官方客户端检查，勿继续自动发送。"
        return None

    def protect_account(self, exc):
        reason = self.account_failure(exc)
        if reason:
            if self.limiter:
                self.limiter.stop(reason)
            raise AccountStopped(reason) from exc

    def wait_error(self, exc, *, peer_id=None):
        """Recognize exact wait errors, never treat every 420 as retryable."""
        name = type(exc).__name__
        if name not in {"FloodWaitError", "FloodPremiumWaitError", "SlowModeWaitError"}:
            return False
        seconds = getattr(exc, "seconds", None)
        if seconds is None:
            return False
        if self.limiter:
            self.limiter.cooldown(seconds,
                                  peer_id=peer_id if name == "SlowModeWaitError" else None,
                                  reason=name)
        return True

    async def read(self, factory, *, timeout=None, activity=None):
        """Only automatically repeat read-only RPCs after explicit FloodWait."""
        retries = self.config.limits.retry_wait_errors if self.limiter else 3
        for attempt in range(retries + 1):
            try:
                self.checkpoint()
                if self.limiter:
                    await self.limiter.read_wait()
                self._emit_status(self._stage)
                return await self._await_read(factory, timeout=timeout, activity=activity)
            except errors.RPCError as exc:
                self.protect_account(exc)
                if not self.wait_error(exc):
                    raise
                if exc.seconds > self.config.max_flood_wait or attempt == retries:
                    raise SyncError(f"读取限流 {exc.seconds} 秒，已达到自动等待/重试上限；"
                                    "请稍后重试，已识别账号的冷却时间保留。") from exc
                log.warning("读取限流 %s 秒，%s", exc.seconds,
                            "等待时间已持久化" if self.limiter else "等待期间可安全停止")
                if not self.limiter:
                    await self._await_read(lambda: asyncio.sleep(exc.seconds + 1),
                                           timeout=exc.seconds + 1 + READ_TIMEOUT_SECONDS,
                                           label="Telegram 读取限流等待")

    async def resolve_entity(self, value, *, role="聊天"):
        try:
            reference = parse_chat_reference(value)
        except PeerReferenceError as exc:
            raise SyncError(f"{role}地址无法识别：{exc}") from None
        if reference.from_message:
            self.phase("resolving_source" if role == "源" else "resolving_target",
                       f"已识别{role}帖子所属的频道／群组，搬运起点仍按起始 ID 和增量进度，正在读取聊天信息…")
        if reference.kind == "title":
            return await self._resolve_title(reference.peer, role)
        try:
            return await self.read(lambda: self.client.get_entity(reference.peer))
        except (errors.InviteHashExpiredError, errors.InviteHashInvalidError):
            raise SyncError(f"{role}邀请链接已失效或不正确。请重新复制有效链接，"
                            "或点击“选择”使用当前账号已加入的频道／群组。") from None
        except errors.ChannelPrivateError:
            raise SyncError(f"当前账号没有{role}频道／群组的访问权限。"
                            "请先在 Telegram 官方客户端确认已加入且可以打开，再重新选择。") from None
        except errors.ChannelInvalidError:
            raise SyncError(f"{role}频道／群组 ID 当前无效或不可访问。"
                            "请点击输入框旁的“选择”重新选取，或重新复制频道链接。") from None
        except (ValueError, errors.UsernameInvalidError, errors.UsernameNotOccupiedError):
            if reference.kind == "invite":
                raise SyncError(f"{role}邀请链接当前无法访问。请先在 Telegram 官方客户端加入该频道／群组，"
                                "然后点击输入框旁的“选择”，或重新粘贴有效链接。软件不会自动入群。") from None
            if reference.allow_title:
                return await self._resolve_title(reference.peer, role)
            if reference.kind not in {"id", "entity"}:
                raise SyncError(f"无法找到{role}频道／群组。请检查 @用户名或链接是否有效，"
                                "也可点击输入框旁的“选择”从当前账号的聊天列表选取。") from None
            # A numeric private-chat ID needs the current account's access_hash.
            self.phase("loading_dialogs", f"正在读取当前账号的聊天列表，以识别{role}频道／群组 ID…")
            await self.read(lambda: self.client.get_dialogs())
            try:
                return await self.read(lambda: self.client.get_entity(reference.peer))
            except ValueError:
                raise SyncError(f"当前账号找不到{role}频道／群组 ID。请先在 Telegram 官方客户端确认已加入，"
                                "再点击输入框旁的“选择”。私有帖子链接只提供 ID，不能代替访问权限。") from None

    async def _resolve_title(self, title, role):
        self.phase("loading_dialogs", f"正在当前账号的聊天列表中查找{role}频道／群组名称…")
        dialogs = await self.read(lambda: self.client.get_dialogs())
        matches = {}
        for dialog in dialogs:
            entity = getattr(dialog, "entity", dialog)
            if isinstance(entity, (types.Channel, types.Chat)) and getattr(entity, "title", None) == title:
                matches[utils.get_peer_id(entity)] = entity
        if len(matches) == 1:
            return next(iter(matches.values()))
        if len(matches) > 1:
            raise SyncError(f"当前账号有多个同名的{role}频道／群组。请点击输入框旁的“选择”明确指定，"
                            "或填写 @用户名／完整频道链接。")
        raise SyncError(f"当前账号的聊天列表中找不到该{role}名称。请填写频道／群组链接或 @用户名，"
                        "也可点击输入框旁的“选择”；显示名称和 @用户名不一定相同。")

    async def linked(self, channel):
        if not isinstance(channel, types.Channel) or not channel.broadcast:
            return None
        full = await self.read(lambda: self.client(functions.channels.GetFullChannelRequest(channel)))
        linked_id = getattr(full.full_chat, "linked_chat_id", None)
        if not linked_id:
            return None
        for chat in full.chats:
            if chat.id == linked_id:
                return chat
        return await self.resolve_entity(types.PeerChannel(linked_id))

    async def prepare(self):
        self.phase("preparing", "正在检查已登录账号…")
        log.info("媒体传输处理：%s", backend_info()["message"])
        me = await self.read(lambda: self.client.get_me())
        if self.limiter is None:
            self.limiter = Limiter(self.state, me.id, self.config.limits)
        self.phase("resolving_source", "正在识别源频道／群组及访问权限…")
        self.source = await self.resolve_entity(self.config.source, role="源")
        if not isinstance(self.source, (types.Channel, types.Chat)):
            raise SyncError("源地址没有指向可访问的频道或群组。请检查是否误填了个人账号，或点击“选择”重新选取。")
        if (self.config.mode in {"forward", "download_then_forward"}
                and getattr(self.source, "noforwards", False)):
            raise SyncError("源聊天启用了“保护内容”，Telegram 不允许直接转发。"
                            "请使用允许转发的源，或由源管理员调整保护设置；本次未下载、未发送，进度未推进。")
        if self.source_topic_id:
            await self.check_topic(self.source, self.source_topic_id, "源")
        if self.config.mode != "download_only":
            self.phase("resolving_target", "正在识别目标频道／群组…")
            self.target = await self.resolve_entity(self.config.target, role="目标")
            if not isinstance(self.target, (types.Channel, types.Chat)):
                raise SyncError("目标地址没有指向可访问的频道或群组。请检查是否误填了个人账号，或点击“选择”重新选取。")
            if self.target_topic_id:
                await self.check_topic(self.target, self.target_topic_id, "目标")
            if utils.get_peer_id(self.source) == utils.get_peer_id(self.target):
                destination_topic = self.target_topic_id or (1 if getattr(self.target, "forum", False) else 0)
                if not (self.source_topic_id and destination_topic and self.source_topic_id != destination_topic):
                    raise SyncError("源和目标不能是同一范围；同群搬运必须选择两个不同话题，不能从整个群搬回该群。")
        if self.config.sync_comments:
            self.phase("checking_discussion", "正在检查源和目标频道的关联评论区…")
            self.source_link = await self.linked(self.source)
            if self.source_link and self.config.comment_mode != "download_only":
                self.target_link = await self.linked(self.target)
                if not self.target_link:
                    raise SyncError("目标频道未开启评论区（缺少 linked chat）；请先关联讨论组。尚未发送消息。")
                if self.config.comment_mode in {"forward", "download_then_forward"}:
                    # forward_messages has NO reply_to. Raw top_msg_id is for forum
                    # topics; raw reply_to only supports monoforums. Neither is a
                    # linked-channel comment thread. Do not send orphan forwards.
                    raise SyncError("Telegram/Telethon 不支持将直接转发附到普通频道评论线程。"
                                    "请设置 comment_mode='reupload' 或 'download_only'；"
                                    "频道正文仍支持 forward/download_then_forward。尚未发送消息。")
        for peer in (self.target, self.target_link):
            if isinstance(peer, types.Channel) and peer.megagroup:
                self.phase("checking_slowmode", "正在检查目标群组的慢速模式…")
                await self.load_slowmode(peer)
        signature = {
            "account": me.id, "source": utils.get_peer_id(self.source),
            "target": utils.get_peer_id(self.target) if self.target else None,
            "mode": self.config.mode, "comment_mode": self.config.comment_mode,
            "source_link": utils.get_peer_id(self.source_link) if self.source_link else None,
            "target_link": utils.get_peer_id(self.target_link) if self.target_link else None,
        }
        # Keep existing, unrestricted routes unchanged. Each chosen topic gets
        # an independent durable cursor and pending journal.
        if self.source_topic_id:
            signature["source_topic_id"] = self.source_topic_id
        if self.target_topic_id > 1:
            signature["target_topic_id"] = self.target_topic_id
        self.route_id = self.state.route(signature)
        for op in self.state.pending(self.route_id):
            ids = op["source_ids"]
            if isinstance(ids, str):
                ids = json.loads(ids)
            if all(self.state.get_message(self.route_id, op["scope"], n) for n in ids):
                self.state.finish_operation(self.route_id, op["scope"], op["key"])
            else:
                raise SendOutcomeUnknown(f"发送结果待人工核对：{op['key']}。上次可能在发送后断线，"
                                         "为避免重复发送已停止。请到“运行记录 → 待核对发送”查看；"
                                         "核对并清理目标中这批可能已发送的内容后，再解除阻塞。")
        log.info("模式=%s，评论=%s，任务=%s", self.config.mode,
                 self.config.comment_mode if self.source_link else "无独立评论区", self.route_id)

    async def check_topic(self, peer, topic_id, role):
        if not getattr(peer, "forum", False):
            raise SyncError(f"{role}选择了话题，但该聊天没有开启话题。请重新选择群组和话题。")
        self.phase("checking_topic", f"正在检查{role}群组话题 {topic_id}…")
        try:
            topic = await get_forum_topic(self.client, peer, topic_id, read=self.read)
        except ForumError as exc:
            raise SyncError(f"{role}话题不可用：{exc}") from None
        self.phase("topic_ready", f"已确认{role}话题：{topic.title}（ID {topic_id}）")
        return topic

    async def load_slowmode(self, peer):
        full = await self.read(lambda: self.client(functions.channels.GetFullChannelRequest(peer)))
        peer_id = utils.get_peer_id(peer)
        if not peer.creator and not peer.admin_rights:
            self.peer_slowmode[peer_id] = getattr(full.full_chat, "slowmode_seconds", 0) or 0
            until = getattr(full.full_chat, "slowmode_next_send_date", None)
            if until:
                timestamp = until.timestamp() if hasattr(until, "timestamp") else float(until)
                if timestamp > time.time():
                    self.limiter.cooldown(timestamp - time.time(), peer_id=peer_id, reason="群慢速模式")

    def settled(self, batch):
        now = datetime.now(timezone.utc)
        return all((now - m.date).total_seconds() >= self.config.settle_seconds for m in batch)

    async def scan(self, entity, *, minimum: int, root: int | None = None, topic_id: int = 0):
        # Freeze the upper bound to avoid chasing a live chat forever.
        scope = "comment" if root is not None else "post"
        label = (f"正在读取评论线程 {root} 的新增评论" if root is not None else
                 f"正在读取源话题 {topic_id} 的新增消息" if topic_id else "正在读取源频道／群组的新增消息")
        self.phase("scanning_comments" if root is not None else "scanning_posts",
                   label + f"（起点 ID {minimum}）…", scope=scope)
        reply_root = root if root is not None else (topic_id or None)
        latest = await self.read(lambda: self.client.get_messages(entity, limit=1, reply_to=reply_root))
        if root is None:
            self._post_scan = {"minimum": minimum, "latest": latest[0].id if latest else None,
                               "deferred": False}
        if not latest:
            return
        if latest[0].id <= minimum:
            return
        upper = latest[0].id + 1
        cursor = minimum
        attempts = 0
        while True:
            try:
                if self.limiter:
                    await self.limiter.read_wait()
                iterator = self.client.iter_messages(entity, min_id=cursor, max_id=upper,
                                                     reverse=True, reply_to=reply_root, limit=None,
                                                     wait_time=max(1.0, self.config.limits.min_request_interval))
                async def guarded():
                    while True:
                        try:
                            item = await self._await_read(
                                lambda: anext(iterator), label=label,
                                timeout=READ_TIMEOUT_SECONDS + max(1.0, self.config.limits.min_request_interval))
                        except StopAsyncIteration:
                            return
                        yield item

                async for batch in batches(guarded()):
                    self.checkpoint()
                    if not self.settled(batch):
                        self._deferred_scopes.add(scope)
                        if root is None:
                            self._post_scan["deferred"] = True
                        self.phase("settling", f"消息 {batch[0].id} 尚在相册稳定等待期（{self.config.settle_seconds:g} 秒），"
                                   "暂留到后续扫描处理。", scope=scope, source_id=batch[0].id)
                        return
                    if batch[0].grouped_id and cursor == minimum and minimum:
                        previous = await self.read(lambda: self.client.get_messages(
                            entity, offset_id=batch[0].id, limit=10))
                        if any(m.grouped_id == batch[0].grouped_id and m.id <= minimum for m in previous):
                            raise SyncError("start_id/游标位于相册中间；请将起点设到整组相册之前。未拆分相册。")
                    yield batch
                    cursor = max(m.id for m in batch)
                    self.phase("scanning_comments" if root is not None else "scanning_posts",
                               label + f"（已扫描至 ID {cursor}）…", scope=scope)
                return
            except errors.RPCError as exc:
                self.protect_account(exc)
                if not self.wait_error(exc):
                    raise
                if (exc.seconds > self.config.max_flood_wait
                        or attempts >= self.config.limits.retry_wait_errors):
                    raise SyncError(f"历史读取限流 {exc.seconds} 秒，已达到自动等待/重试上限；"
                                    "冷却时间保留，请稍后重试。") from exc
                attempts += 1
                if not self.limiter:
                    await asyncio.sleep(exc.seconds + 1)
                # Resume only after the last complete, yielded album.

    async def download(self, message, scope):
        connections = getattr(self.config, "download_connections", 4)
        self.phase("downloading", f"正在下载{'评论' if scope == 'comment' else '正文'}媒体，消息 ID {message.id}（最多 {connections} 路）…",
                   scope=scope, source_id=message.id)
        old = self.state.download(self.route_id, scope, message.id)
        if old:
            path = Path(old["path"])
            expected_parent = self.config.download_dir / self.route_id / scope / str(message.id)
            # Selecting a new folder applies to unfinished/new downloads too.
            # Never silently reuse a cache that lives on the previously chosen
            # disk. Old registrations stay available for explicit cleanup there.
            if (path.parent.resolve() == expected_parent.resolve()
                    and path.is_file() and path.stat().st_size == old["size"]):
                return path
        ext = getattr(message.file, "ext", "") or ".bin"
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,12}", ext):
            ext = ".bin"
        directory = self.config.download_dir / self.route_id / scope / str(message.id)
        directory.mkdir(parents=True, exist_ok=True)
        original_name = getattr(message.file, "name", None)
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", original_name or ("media" + ext))
        name = name.strip(" .") or ("media" + ext)
        # Prefix also neutralizes Windows reserved names such as CON and NUL.
        name = f"{message.id}_{Path(name).stem[:120]}{ext}"
        path = directory / name
        temporary = directory / (name + ".part")
        activity = [asyncio.get_running_loop().time()]
        def start_download():
            # Every attempt gets its own inactivity clock, after any persisted
            # cooldown or request pacing has finished.
            activity[0] = asyncio.get_running_loop().time()
            progress = self.transfer_progress("downloading", message.id, scope, activity=activity)
            return download_media_file(self.client, message, temporary,
                                       connections=connections, progress_callback=progress)

        result = await self.read(start_download,
            timeout=DOWNLOAD_IDLE_SECONDS, activity=lambda: activity[0])
        if not result or not Path(result).is_file():
            raise SyncError(f"消息 {message.id} 的媒体未成功下载。")
        actual = Path(result)
        size = actual.stat().st_size
        expected = getattr(message.document, "size", None)
        if expected is not None and size != expected:
            raise SyncError(f"消息 {message.id} 文件不完整：{size}/{expected} 字节。")
        actual.replace(path)
        self.state.save_download(self.route_id, scope, message.id, str(path), size,
                                 root=self.config.download_dir)
        return path

    def cleanup_batch(self, batch, scope):
        """Only confirmed original forwards may opt in to local deletion."""
        self.phase("cleanup", "转发已确认并保存进度，正在清理本组已转发的下载文件…", scope=scope)
        try:
            result = cleanup_forwarded(
                self.state, roots=[self.config.download_dir], route_id=self.route_id,
                scope=scope, source_ids=[item.id for item in batch], should_stop=self.should_stop)
        except Exception as exc:
            # Cleanup must never turn a committed send into a retryable send.
            log.warning("转发已成功，自动清理未完成（%s）；保留发送进度，可稍后手动清理。", type(exc).__name__)
            self.phase("cleanup_result", "本组转发已成功；自动清理未完成，可稍后点击清理重试。", scope=scope)
            return
        for key in self.cleanup_counts:
            self.cleanup_counts[key] += result.get(key, 0)
        text = (f"本组转发已成功，清理 {result.get('deleted_files', 0)} 个下载文件，"
                f"释放 {result.get('freed_bytes', 0) / 1048576:.2f} MB。")
        if result.get("failed_files") or result.get("skipped_files"):
            text += "未满足清理条件或被占用的文件已保留，可稍后查看并重试。"
        self.phase("cleanup_result", text, scope=scope)
        log.info("%s", text)

    def transfer_progress(self, phase, source_id, scope, *, activity=None):
        last = [0.0]
        last_bytes = [0]
        label = "下载" if phase == "downloading" else "上传"
        started = asyncio.get_running_loop().time()

        def report(current, total):
            now = asyncio.get_running_loop().time()
            if current > last_bytes[0]:
                if activity is not None:
                    activity[0] = now
                if phase == "uploading":
                    self._upload_activity_at = now
                last_bytes[0] = current
            if now - last[0] < 0.5 and current != total:
                return
            last[0] = now
            amount = f"{100 * current / total:.1f}%" if total else f"{current / 1048576:.1f} MB"
            speed_text = ""
            # Album-upload callbacks count files/fractions, not bytes. Only
            # download callbacks have a guaranteed byte unit here.
            if phase == "downloading":
                speed = current / max(now - started, 0.001)
                speed_text = f" · {current / 1048576:.1f}" + (f"/{total / 1048576:.1f}" if total else "") + " MB"
                speed_text += f" · {speed / 1048576:.2f} MB/s"
            self._emit_status({"phase": phase, "scope": scope, "source_id": source_id,
                               "message": f"正在{label}媒体，消息 ID {source_id}：{amount}{speed_text}",
                               "current_bytes": current, "total_bytes": total})
        return report

    def record(self, message, scope, target_id=None, context=None, status="done"):
        context = context or {}
        return {
            "source_id": message.id, "target_id": target_id,
            "source_chat_id": utils.get_peer_id(self.source if scope == "post" else self.source_link),
            "target_chat_id": utils.get_peer_id(self.target if scope == "post" else self.target_link)
            if (self.target if scope == "post" else self.target_link) else None,
            "grouped_id": message.grouped_id,
            "source_post_id": context.get("source_post_id"),
            "source_root_id": context.get("source_root_id"),
            "target_root_id": context.get("target_root_id"),
            "text": getattr(message, "message", "") or "", "status": status,
            "metadata": json.loads(message.to_json()),
        }

    async def send_reupload(self, batch, paths, destination, reply_to, *, scope=None):
        progress = self.transfer_progress("uploading", batch[0].id, scope or ("comment" if reply_to else "post"))
        if paths:
            # Local paths force an upload. Never pass message.media/document/photo,
            # a cached InputDocument, a Bot file_id, or an existing InputFile here.
            if len(batch) > 1:
                return await self.client.send_file(
                    destination, [str(paths[m.id]) for m in batch],
                    caption=[m.message or "" for m in batch],
                    formatting_entities=[m.entities or [] for m in batch],
                    parse_mode=None, reply_to=reply_to, allow_cache=False,
                    progress_callback=progress,
                    force_document=all(bool(m.document) and not m.video for m in batch),
                )
            message = batch[0]
            kwargs = {}
            if message.document:
                kwargs.update(attributes=message.document.attributes,
                              mime_type=message.document.mime_type,
                              force_document=True,
                              voice_note=bool(message.voice), video_note=bool(message.video_note),
                              supports_streaming=bool(message.video))
            return await self.client.send_file(
                destination, str(paths[message.id]), caption=message.message or "",
                formatting_entities=message.entities or [], parse_mode=None,
                reply_to=reply_to, allow_cache=False, **kwargs,
                progress_callback=progress,
            )
        message = batch[0]
        return await self.client.send_message(destination, message.message or "",
                                              formatting_entities=message.entities or [],
                                              parse_mode=None, link_preview=False, reply_to=reply_to)

    async def process(self, batch, scope, mode, *, context=None):
        self.checkpoint()
        if self.limiter:
            self.limiter.check_runtime()
        context = context or {}
        thread_id = context.get("source_root_id", 0)
        existing = [self.state.get_message(self.route_id, scope, m.id) for m in batch]
        if all(existing):
            self.state.set_cursor(self.route_id, scope, max(m.id for m in batch), thread_id)
            return
        if any(existing):
            raise SyncError("发现部分已处理的相册，停止以避免拆分或重复发送；请检查 SQLite。")
        if len(batch) > 10:
            raise SyncError("媒体组超过 Telegram 单次相册限制 10，未拆分发送。")
        regular = [m for m in batch if isinstance(m, types.Message)]
        if not regular:
            rows = [self.record(m, scope, context=context, status="skipped_service") for m in batch]
            self.state.save_messages(self.route_id, scope, rows, max(m.id for m in batch), thread_id)
            self.progress(scope, rows)
            return
        if len(regular) != len(batch):
            raise SyncError("媒体组中出现服务消息，停止处理。")
        decision = self.ad_filter.evaluate(batch, scope=scope, mode=mode)
        if decision.blocked:
            rows = [self.record(m, scope, context=context, status="filtered_ad") for m in batch]
            for row in rows:
                row["metadata"]["ad_filter"] = {
                    "reasons": list(decision.reasons), "matched_ids": list(decision.matched_ids),
                    "album_ids": [m.id for m in batch],
                }
            self.state.save_messages(self.route_id, scope, rows, max(m.id for m in batch), thread_id)
            self.progress(scope, rows)
            log.info("已拦截广告 %s %s：%s", scope, [m.id for m in batch], "；".join(decision.reasons))
            return
        if (mode in {"forward", "download_then_forward"}
                and any(getattr(item, "noforwards", False) for item in batch)):
            raise SyncError("这批源消息启用了“保护内容”，Telegram 不允许直接转发。"
                            "请使用允许转发的源；本次未下载、未发送，进度未推进。")
        paths = {}
        if mode in {"download_only", "reupload", "download_then_forward"}:
            for m in batch:
                if m.photo or m.document:
                    paths[m.id] = await self.download(m, scope)
                elif mode == "reupload" and m.media and not isinstance(m.media, types.MessageMediaWebPage):
                    raise SyncError(f"消息 {m.id} 含不支持重传的媒体 {type(m.media).__name__}；"
                                    "请改用正文 forward 或单独备份，进度未越过此消息。")
        if len(batch) > 1 and mode == "reupload" and len(paths) != len(batch):
            raise SyncError("相册中存在无法下载的成员，未拆开发送。")
        if mode == "download_only":
            rows = [self.record(m, scope, context=context) for m in batch]
            self.state.save_messages(self.route_id, scope, rows, max(m.id for m in batch), thread_id)
            self.progress(scope, rows, files=len(paths))
            log.info("已备份 %s %s", scope, [m.id for m in batch])
            return
        if mode == "reupload" and not paths and not batch[0].message:
            raise SyncError(f"消息 {batch[0].id} 没有可重发的文字或文件。")
        destination = self.target if scope == "post" else self.target_link
        peer_id = utils.get_peer_id(destination)
        if self.peer_slowmode.get(peer_id) and len(batch) > 1 and mode in {"forward", "download_then_forward"}:
            raise SyncError("目标群启用了慢速模式，不支持多条直接转发；相册未被拆分。")
        reply_to = (context.get("target_root_id") if scope == "comment" else
                    self.target_topic_id if self.target_topic_id > 1 else None)
        key = f"{scope}:{thread_id}:{','.join(str(m.id) for m in batch)}"
        retries = self.config.limits.retry_wait_errors if self.limiter else 0
        for attempt in range(retries + 1):
            self.checkpoint()
            if self.limiter:
                await self.limiter.reserve(peer_id, len(batch))
            self.checkpoint()
            self.state.begin_operation(self.route_id, scope, key, [m.id for m in batch])
            try:
                if mode in {"forward", "download_then_forward"}:
                    self.phase("forwarding", f"正在转发消息组 {','.join(str(m.id) for m in batch)}…",
                               scope=scope, source_ids=[m.id for m in batch])
                    # Archives plus ORIGINAL forwarding, not a media reupload.
                    if scope == "post" and self.target_topic_id > 1:
                        sent = await self._await_send(lambda: forward_to_topic(
                            self.client, destination, batch, self.source, self.target_topic_id))
                    else:
                        sent = await self._await_send(lambda: self.client.forward_messages(
                            destination, batch, from_peer=self.source if scope == "post" else self.source_link))
                else:
                    self.phase("uploading" if paths else "sending_text",
                               f"正在{'重新上传媒体' if paths else '发送文字'}，消息组 {','.join(str(m.id) for m in batch)}…",
                               scope=scope)
                    sent = await self._await_send(
                        lambda: self.send_reupload(batch, paths, destination, reply_to, scope=scope),
                        timeout=UPLOAD_IDLE_SECONDS if paths else SEND_RPC_TIMEOUT_SECONDS,
                        activity=(lambda: self._upload_activity_at) if paths else None)
            except ForumError as exc:
                # A raw forward may already be accepted even if its response
                # cannot be mapped. Retain pending and do not replay it.
                raise SyncError(f"目标话题发送结果未确认：{exc}。已保留待核对记录，请先检查目标话题。") from None
            except errors.RPCError as exc:
                reason = self.account_failure(exc)
                if reason and self.limiter:
                    self.limiter.stop(reason)
                waited = self.wait_error(exc, peer_id=peer_id)
                # Persist a cooldown/stop BEFORE clearing a rejected operation.
                # 5xx/timeouts remain uncertain and retain the pending marker.
                if 400 <= abs(getattr(exc, "code", 500)) < 500:
                    self.state.finish_operation(self.route_id, scope, key)
                if reason:
                    raise AccountStopped(reason) from exc
                if waited and attempt < retries and exc.seconds <= self.config.max_flood_wait:
                    log.warning("发送被限流 %s 秒，将按已保存的冷却时间恢复（重试 %s/%s）",
                                exc.seconds, attempt + 1, retries)
                    continue
                if abs(getattr(exc, "code", 500)) >= 500:
                    raise SendOutcomeUnknown(
                        f"Telegram 服务器异常 {key}（{type(exc).__name__}），发送结果无法确认。"
                        "进度未推进，已保留待核对记录；请到“运行记录 → 待核对发送”核对，不会自动重发。") from exc
                reasons = {
                    "ChatForwardsRestrictedError": "源聊天启用了“保护内容”，Telegram 不允许直接转发。请使用允许转发的源，或由源管理员调整保护设置。",
                    "ChatWriteForbiddenError": "当前账号没有向目标频道或群组发送内容的权限。请检查目标发帖权限。",
                    "ChatAdminRequiredError": "目标操作需要管理员权限，请检查当前账号的目标权限。",
                    "UserBannedInChannelError": "当前账号在目标频道或群组被禁言或封禁，请先检查目标权限。",
                    "MessageIdInvalidError": "源消息不存在或不可转发，请检查源消息是否已删除及当前账号的访问权限。",
                    "ChannelPrivateError": "当前账号无法访问源或目标聊天，请检查是否仍已加入且有对应权限。",
                }
                if waited:
                    reason = f"Telegram 要求等待 {exc.seconds} 秒，已保存冷却期限并停止本次发送。"
                else:
                    reason = reasons.get(type(exc).__name__, f"Telegram 拒绝了本次发送（{type(exc).__name__}）。")
                raise SyncError(f"{reason} 进度未推进，本次没有待核对发送。") from exc
            break
        # Network failures/cancellation keep the pending marker: an accepted RPC
        # might have lost its response. A fresh random_id would risk duplicates.
        sent = list(sent) if isinstance(sent, (list, tuple)) else [sent]
        if len(sent) != len(batch) or any(m is None for m in sent):
            raise SyncError(f"{key} 未获得完整目标 ID；请核对 --show-pending。")
        if any(getattr(m, "video_processing_pending", False) for m in sent):
            raise SyncError(f"{key} 正由 Telegram 异步处理视频，暂存 ID 不是最终帖子 ID。"
                            "已保留 pending，请等待完成并按恢复说明核对目标。")
        if scope == "comment":
            for m in sent:
                header = m.reply_to
                if not header or reply_to not in {header.reply_to_msg_id, header.reply_to_top_id}:
                    raise SyncError(f"{key} 的评论线程响应不匹配，已停止；请核对目标和 pending。")
        if scope == "post" and self.target_topic_id > 1:
            for item in sent:
                header = getattr(item, "reply_to", None)
                if not header or self.target_topic_id not in {header.reply_to_msg_id, header.reply_to_top_id}:
                    raise SyncError(f"{key} 的目标话题响应不匹配，已保留待核对记录；请检查目标和 pending。")
        rows = [self.record(m, scope, t.id, context) for m, t in zip(batch, sent)]
        self.state.save_messages(self.route_id, scope, rows, max(m.id for m in batch), thread_id)
        self.state.finish_operation(self.route_id, scope, key)
        self.progress(scope, rows, files=len(paths))
        if paths and mode == "download_then_forward" and getattr(self.config, "auto_cleanup_forwarded", False):
            self.cleanup_batch(batch, scope)
        if self.limiter and self.peer_slowmode.get(peer_id):
            self.limiter.cooldown(self.peer_slowmode[peer_id], peer_id=peer_id, reason="目标群慢速模式")
        log.info("已同步 %s %s → %s", scope, [m.id for m in batch], [m.id for m in sent])
        self.checkpoint()
        if self.config.send_delay:
            if self.limiter:
                await self.limiter.pause(self.config.send_delay)
            else:
                await asyncio.sleep(self.config.send_delay)

    async def discussion_root(self, channel, post_id, linked, *, retry=False):
        for attempt in range(4 if retry else 1):
            try:
                result = await self.read(lambda: self.client(
                    functions.messages.GetDiscussionMessageRequest(peer=channel, msg_id=post_id)))
                root = thread_root(result.messages, utils.get_peer_id(linked))
                if root:
                    return root.id
            except errors.MsgIdInvalidError:
                pass
            if retry and attempt < 3:
                await asyncio.sleep(1 + attempt)
        return None

    async def sync_comments(self):
        if not self.source_link:
            return
        seen = set()
        posts = self.state.messages(self.route_id, "post")
        for index, post in enumerate(posts, 1):
            self.checkpoint()
            if post["status"] != "done":
                continue
            self.phase("checking_comments", f"正在检查旧帖评论（{index}/{len(posts)}），帖子 ID {post['source_id']}…",
                       scope="comment", source_id=post["source_id"])
            thread = self.state.get_thread(self.route_id, post["source_id"])
            if not thread:
                root = await self.discussion_root(self.source, post["source_id"], self.source_link)
                if not root:
                    # No comments root (e.g. an older or closed post). Check again
                    # on later runs; never misclassify a permanent absence as sent.
                    continue
                target_root = None
                if self.config.comment_mode != "download_only":
                    target_root = await self.discussion_root(self.target, post["target_id"],
                                                              self.target_link, retry=True)
                    if not target_root:
                        raise SyncError(f"目标帖子 {post['target_id']} 的讨论根尚不存在或已被删除。"
                                        "正文已保存，不会重发；恢复评论区后重试。")
                self.state.save_thread(self.route_id, post["source_id"], root, post["target_id"],
                                       target_root, utils.get_peer_id(self.source_link),
                                       utils.get_peer_id(self.target_link) if self.target_link else None)
                thread = self.state.get_thread(self.route_id, post["source_id"])
            root = thread["source_root_id"]
            if root in seen:
                continue  # Album members can share one comment thread.
            seen.add(root)
            if self.target_link:
                target_root = await self.read(lambda: self.client.get_messages(
                    self.target_link, ids=thread["target_root_id"]))
                if not isinstance(target_root, types.Message):
                    raise SyncError(f"目标讨论根 {thread['target_root_id']} 已删除或不可读，评论未发送。")
            minimum = self.state.cursor(self.route_id, "comment", root)
            async for batch in self.scan(self.source_link, minimum=minimum, root=root):
                # Only real replies belong in this thread; never copy its root.
                replies = [m for m in batch if m.id != root]
                if replies:
                    await self.process(replies, "comment", self.config.comment_mode, context=thread)

    async def run_once(self):
        self.checkpoint()
        before = dict(self.counts)
        self._deferred_scopes.clear()
        minimum = max(self.config.start_id, self.state.cursor(self.route_id, "post"))
        async for batch in self.scan(self.source, minimum=minimum, topic_id=self.source_topic_id):
            await self.process(batch, "post", self.config.mode)
        # Scan every known thread with its own min_id, including old posts with
        # newly added comments. A single global comment cursor would miss these.
        await self.sync_comments()
        delta = {name: self.counts[name] - before[name] for name in before}
        reason, detail = "processed", ""
        if self._deferred_scopes:
            label = "正文及评论" if len(self._deferred_scopes) > 1 else ("评论" if "comment" in self._deferred_scopes else "正文")
            reason, detail = "settling", f"最新{label}尚在相册稳定等待期，将在后续扫描处理。"
        elif not delta["processed"]:
            latest, minimum = self._post_scan.get("latest"), self._post_scan.get("minimum", minimum)
            if latest is None:
                reason, detail = "source_empty", "源频道／群组当前没有可读取的历史消息。"
            elif minimum >= latest:
                reason = "start_after_latest" if self.config.start_id and self.config.start_id >= latest else "no_new"
                detail = f"源最新 ID {latest}，本次起点 ID {minimum}，没有新增内容。"
            else:
                reason, detail = "no_visible_messages", "未读到起点之后的可见消息，请检查源历史可见性及起点设置。"
        elif delta["filtered"] == delta["processed"]:
            reason, detail = "filtered_all", "本轮内容全部命中广告屏蔽规则，未发送。"
        if self.config.mode == "download_only":
            detail += "当前为纯下载备份模式，不向目标发送。"
            if reason == "processed":
                reason = "download_only"
        message = (f"本轮完成：处理 {delta['processed']} 条，发送 {delta['sent']} 条，"
                   f"屏蔽 {delta['filtered']} 条，媒体文件 {delta['files']} 个。{detail}")
        if self.cleanup_counts["deleted_files"]:
            message += (f"本次累计自动清理 {self.cleanup_counts['deleted_files']} 个已转发文件，"
                        f"释放 {self.cleanup_counts['freed_bytes'] / 1048576:.2f} MB。")
        self.summary = {"phase": "round_complete", "message": message, "reason": reason,
                        "counts": dict(self.counts), "round_counts": delta}
        self._emit_status(self.summary)
        return self.summary

    async def run(self):
        await self.prepare()
        while True:
            await self.run_once()
            if not self.config.poll_interval:
                return
            log.info("本轮完成，%s 秒后检查新增内容", self.config.poll_interval)
            self.phase("polling", f"本轮检查结束，{self.config.poll_interval:g} 秒后检查新增消息及旧帖的新评论。",
                       wait_seconds=self.config.poll_interval)
            if self.limiter:
                await self.limiter.pause(self.config.poll_interval)
            else:
                await asyncio.sleep(self.config.poll_interval)
