"""Bounded read-only MTProto download pipeline using the existing user client.

``connections`` is the maximum number of in-flight chunk requests, not a
promise of additional TCP connections or of bypassing Telegram speed limits.
No phone login, persisted session, message send, or upload is created here. Telethon 1.45's
sender pool and RPC handling retain the caller's proxy, AES backend and Flood
policy. Small/unknown media and CDN transfers use its ordinary downloader.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from copy import copy
from pathlib import Path

from telethon import TelegramClient, errors, functions, types, utils
from telethon.client.telegrambaseclient import _ExportState
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER


CHUNK_SIZE = 512 * 1024
PARALLEL_MIN_SIZE = 2 * 1024 * 1024
MAX_CONNECTIONS = 8
MAX_REFERENCE_REFRESHES = 2
MAX_DC_MIGRATIONS = 2
SENDER_CLEANUP_TIMEOUT = 5.0
_log = logging.getLogger(__name__)


class DownloadError(RuntimeError):
    """A safe-to-display local download/integrity failure."""


class _SequentialRequired(Exception):
    pass


async def _progress(callback, current, total):
    if callback is not None:
        returned = callback(current, total)
        if inspect.isawaitable(returned):
            await returned


async def _disconnect_unowned(sender):
    """An interrupted handshake must not orphan its new transport."""
    task = asyncio.create_task(sender.disconnect())
    deadline = asyncio.get_running_loop().time() + SENDER_CLEANUP_TIMEOUT
    failed = False
    try:
        while not task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                failed = True
                break
            try:
                # A second cancellation from the pipeline must not interrupt
                # cleanup already started for a failed handshake. The shield
                # has one fixed deadline and the owned task is always awaited.
                await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            except asyncio.CancelledError:
                if task.cancelled():
                    failed = True
                    break
            except Exception:
                failed = True
                break
        if task.done() and not task.cancelled() and task.exception() is not None:
            failed = True
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if failed:
        # Keep the original cancellation/RPC error. No cleanup is detached.
        _log.warning("未完成的媒体连接断开未能正常结束；原始下载异常仍保留。")


def _safe_pool_supported(client):
    return (isinstance(client, TelegramClient)
            and isinstance(getattr(client, "_borrowed_senders", None), dict)
            and getattr(client, "_borrow_sender_lock", None) is not None
            and callable(getattr(client, "_get_dc", None))
            and callable(getattr(client, "_connection", None))
            and hasattr(client, "_init_request"))


def _connection(client, dc):
    return client._connection(dc.ip_address, dc.port, dc.id, loggers=client._log,
                              proxy=client._proxy, local_addr=client._local_addr)


async def _create_sender(client, dc_id):
    """Telethon 1.45 media authorization, with local cancellation ownership.

    This is a transport for the existing account, with no phone login or new
    persisted user session. Unlike the upstream helper, every newly created
    sender is disconnected if any handshake stage fails or is cancelled.
    """
    dc = await client._get_dc(dc_id)
    sender = MTProtoSender(None, loggers=client._log)
    sender.dc_id = dc_id
    try:
        await sender.connect(_connection(client, dc))
        auth = await client._call(client._sender, functions.auth.ExportAuthorizationRequest(dc_id))
        initial = copy(client._init_request)
        initial.query = functions.auth.ImportAuthorizationRequest(id=auth.id, bytes=auth.bytes)
        await client._call(sender, functions.InvokeWithLayerRequest(LAYER, initial))
    except BaseException:
        await _disconnect_unowned(sender)
        raise
    return sender


async def _safe_borrow(client, dc_id):
    """Keep Telethon's cache/counting while owning interrupted construction."""
    async with client._borrow_sender_lock:
        state, sender = client._borrowed_senders.get(dc_id, (None, None))
        if state is None:
            sender = await _create_sender(client, dc_id)
            state = _ExportState()
            # There is no await between successful construction, publishing
            # the cache entry and incrementing its ownership count.
            client._borrowed_senders[dc_id] = (state, sender)
        elif state.need_connect():
            try:
                dc = await client._get_dc(dc_id)
                await sender.connect(_connection(client, dc))
            except BaseException:
                await _disconnect_unowned(sender)
                raise
        state.add_borrow()
        return sender


class _Senders:
    """Borrow each media DC once, and return every completed borrow once."""

    def __init__(self, client):
        self.client = client
        self.exported = {}
        self.lock = asyncio.Lock()

    async def get(self, dc_id):
        if dc_id == self.client.session.dc_id:
            return self.client._sender
        async with self.lock:
            if dc_id not in self.exported:
                sender = (await _safe_borrow(self.client, dc_id) if _safe_pool_supported(self.client)
                          else await self.client._borrow_exported_sender(dc_id))
                # No await between acquiring and recording ownership.
                self.exported[dc_id] = sender
            return self.exported[dc_id]

    async def close(self):
        senders, self.exported = list(self.exported.values()), {}
        results = await asyncio.gather(
            *(self.client._return_exported_sender(sender) for sender in senders),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result


class _File:
    def __init__(self, client, message, document, senders):
        self.client, self.message, self.document = client, message, document
        self.senders = senders
        self.size, self.document_id = document.size, document.id
        self.dc_id = document.dc_id
        self.reference_version = 0
        self.refreshes, self.migrations = 0, 0
        self.refresh_lock = asyncio.Lock()
        self.refresh_failure = None

    async def refresh(self, old_version):
        async with self.refresh_lock:
            if self.refresh_failure is not None:
                raise self.refresh_failure
            if old_version != self.reference_version:
                return  # Another lane already refreshed this same reference.
            if self.refreshes >= MAX_REFERENCE_REFRESHES:
                raise DownloadError("文件引用多次失效，已停止本次下载；请稍后重新读取源消息。")
            peer = getattr(self.message, "input_chat", None) or getattr(self.message, "peer_id", None)
            message_id = getattr(self.message, "id", None)
            if peer is None or type(message_id) is not int or message_id <= 0:
                raise DownloadError("文件引用失效，无法核对原消息；本次下载已停止。")
            self.refreshes += 1
            try:
                fresh = await self.client.get_messages(peer, ids=message_id)
            except Exception as exc:
                self.refresh_failure = exc
                raise
            document = getattr(fresh, "document", None)
            try:
                same_peer = utils.get_peer_id(fresh.peer_id) == utils.get_peer_id(peer)
            except (AttributeError, TypeError, ValueError):
                same_peer = False
            if (not isinstance(fresh, types.Message) or fresh.id != message_id or not same_peer
                    or not isinstance(document, types.Document) or document.id != self.document_id
                    or document.size != self.size):
                self.refresh_failure = DownloadError("刷新文件引用时源消息或媒体已变化；未混合不同文件，本次下载已停止。")
                raise self.refresh_failure
            self.document = document
            self.reference_version += 1

    async def chunk(self, offset):
        # Only explicit, bounded file-reference and DC corrections are retried.
        # FloodWait, network failures and generic RPC failures pass unchanged.
        for _ in range(MAX_REFERENCE_REFRESHES + MAX_DC_MIGRATIONS + 1):
            version, dc_id, document = self.reference_version, self.dc_id, self.document
            sender = await self.senders.get(dc_id)
            request = functions.upload.GetFileRequest(
                types.InputDocumentFileLocation(id=document.id, access_hash=document.access_hash,
                                                file_reference=document.file_reference, thumb_size=""),
                offset=offset, limit=CHUNK_SIZE, cdn_supported=False,
            )
            try:
                result = await self.client._call(sender, request)
            except (errors.FileReferenceExpiredError, errors.FileReferenceInvalidError,
                    errors.FilerefUpgradeNeededError):
                await self.refresh(version)
                continue
            except errors.FileMigrateError as exc:
                # Delayed errors from a previous DC reuse the corrected route.
                if dc_id == self.dc_id:
                    if self.migrations >= MAX_DC_MIGRATIONS or type(exc.new_dc) is not int or exc.new_dc <= 0:
                        raise DownloadError("文件数据中心多次变更，已停止本次下载；请稍后重试。") from None
                    self.dc_id = exc.new_dc
                    self.migrations += 1
                continue
            if isinstance(result, types.upload.FileCdnRedirect):
                # CDN encryption/reupload rules belong to Telethon's downloader.
                raise _SequentialRequired()
            if not isinstance(result, types.upload.File):
                raise DownloadError("Telegram 未返回预期的文件数据块，本次下载已停止。")
            data = result.bytes
            expected = min(CHUNK_SIZE, self.size - offset)
            if not isinstance(data, bytes) or len(data) != expected:
                raise DownloadError("下载数据块长度不符，文件未完成；不会将不完整文件交给后续搬运。")
            return data
        raise DownloadError("文件引用或数据中心修正次数已达上限，本次下载已停止。")


def _supported(client, message, connections):
    document = getattr(message, "document", None)
    return (connections > 1 and isinstance(document, types.Document)
            and type(document.size) is int and document.size >= PARALLEL_MIN_SIZE
            and type(document.dc_id) is int and document.dc_id > 0
            and getattr(client, "_sender", None) is not None
            and type(getattr(getattr(client, "session", None), "dc_id", None)) is int
            and all(callable(getattr(client, name, None)) for name in
                    ("_call", "_borrow_exported_sender", "_return_exported_sender", "get_messages")))


async def _parallel(client, message, path, connections, progress_callback):
    senders = _Senders(client)
    file = _File(client, message, message.document, senders)
    tasks, completed = {}, {}
    total_chunks = (file.size + CHUNK_SIZE - 1) // CHUNK_SIZE
    submitted = written = current = received = 0
    failure = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # The caller supplies a temporary .part path and owns final promotion.
        # A failed attempt never returns success, even if a partial file exists.
        with path.open("wb") as output:
            while written < total_chunks:
                while submitted < total_chunks and len(tasks) + len(completed) < connections:
                    task = asyncio.create_task(file.chunk(submitted * CHUNK_SIZE))
                    tasks[task] = submitted
                    submitted += 1
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                # An error in any lane stops the entire download immediately.
                for task in done:
                    if task.cancelled():
                        raise asyncio.CancelledError()
                    if task.exception() is not None:
                        raise task.exception()
                for task in done:
                    data = task.result()
                    completed[tasks.pop(task)] = data
                    # Transfer activity follows received bytes, even when a
                    # preceding offset is slow and ordered disk writes wait.
                    received += len(data)
                    await _progress(progress_callback, received, file.size)
                # Ordered writes bound buffering to connections*512KiB and
                # prevent sparse/out-of-order files masquerading as complete.
                while written in completed:
                    data = completed.pop(written)
                    if output.write(data) != len(data):
                        raise DownloadError("本地文件未完整写入，请检查磁盘空间和目录权限。")
                    current += len(data)
                    written += 1
            output.flush()
            if (received != file.size or current != file.size
                    or output.tell() != file.size or path.stat().st_size != file.size):
                raise DownloadError("下载文件最终长度不符，本次文件未完成。")
        return str(path)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await senders.close()
        except BaseException:
            if failure is None:
                raise
            if isinstance(failure, _SequentialRequired):
                raise DownloadError("并行下载连接未能完整归还，未继续启动 CDN 下载；请稍后重试。") from None
            _log.warning("下载结束时未能完整归还媒体连接；原始下载失败仍保留。")


async def download_media(client, message, path, *, connections=4, progress_callback=None):
    """Download to a caller-owned temporary path with 1..8 bounded reads.

    Small/unknown files, photos, unsupported client adapters and connections=1
    preserve Telethon's existing behavior. CDN fallback happens at most once,
    only after every parallel task and borrowed sender has been finalized.
    Complete-file reuse and successful archive registration belong to engine.
    """
    if type(connections) is not int or not 1 <= connections <= MAX_CONNECTIONS:
        raise DownloadError("下载并行通道必须是 1 到 8 之间的整数。")
    if _supported(client, message, connections):
        try:
            return await _parallel(client, message, Path(path), connections, progress_callback)
        except _SequentialRequired:
            _log.info("媒体需要 CDN 下载，已收束并行请求并交由 Telethon 单通道处理。")
    return await client.download_media(message, file=str(path), progress_callback=progress_callback)
