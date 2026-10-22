"""Conservative, local-only cleanup of confirmed archive-and-forward files.

The caller supplies the directories the user has currently authorized. Paths
in SQLite never grant permission to another directory. Message mappings,
cursors, pending operations and quota history are never removed here.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Iterable

from .state import State


_SCOPES = {"post", "posts", "comment", "comments"}
_REPARSE_POINT = 0x400


class _UnsafeFile(Exception):
    """A registered path no longer identifies the approved download."""


def _canonical(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _plain_components(path: Path) -> bool:
    """Reject links/junctions before resolve() can conceal them."""
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
            return False
    return True


def _approved_roots(roots: Iterable[str | Path] | str | Path) -> list[Path]:
    if isinstance(roots, (str, Path)):
        roots = [roots]
    approved = []
    for root in roots:
        path = Path(os.path.abspath(root))
        try:
            if not _plain_components(path) or (path.exists() and not path.is_dir()):
                continue
            if _canonical(path.resolve()) != _canonical(path):
                continue
        except (OSError, RuntimeError):
            continue
        if _canonical(path) not in {_canonical(item) for item in approved}:
            approved.append(path)
    return approved


def _file_location(row: dict, roots: list[Path]) -> tuple[Path, Path] | None:
    path = Path(row["path"])
    if (not path.is_absolute() or ".." in path.parts
            or _canonical(path) != os.path.normcase(str(path))):
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", row["route"]) or row["scope"] not in _SCOPES:
        return None
    source_id = row["source_id"]
    if isinstance(source_id, bool) or not isinstance(source_id, int) or source_id <= 0:
        return None
    for root in roots:
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            continue
        if (len(parts) != 4 or parts[:3] != (row["route"], row["scope"], str(source_id))
                or not parts[3].startswith(f"{source_id}_")
                or parts[3].lower().endswith(".part")
                or parts[3].endswith((".", " "))
                or re.search(r'[<>:"|?*\x00-\x1f]', parts[3])):
            continue
        if row.get("root") and _canonical(Path(row["root"])) != _canonical(root):
            continue
        if not _plain_components(path):
            return None
        if not path.resolve().is_relative_to(root.resolve()):
            return None
        return path, root
    return None


def _same_identity(info: os.stat_result, row: dict) -> bool:
    if not stat.S_ISREG(info.st_mode) or info.st_size != row["size"]:
        return False
    if (getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
            or getattr(info, "st_nlink", 1) > 1):
        return False
    fields = ("st_dev", "st_ino", "mtime_ns")
    present = [row.get(field) is not None for field in fields]
    # File size and directory structure cannot prove ownership of a legacy
    # file: a user may have replaced it with another same-size file. Never
    # obtain a fresh stat during migration to claim that replacement.
    if not all(present):
        return False
    def number(value):
        return int(value, 0) if isinstance(value, str) else int(value)
    return (
        number(row["st_dev"]) == info.st_dev and number(row["st_ino"]) == info.st_ino
        and int(row["mtime_ns"]) == info.st_mtime_ns
    )


def _unlink_verified(path: Path, rows: list[dict]) -> None:
    """Windows deletes the verified open file, avoiding a path-swap race."""
    if os.name != "nt":
        if not _plain_components(path) or not all(_same_identity(path.lstat(), row) for row in rows):
            raise _UnsafeFile()
        path.unlink()
        return

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    final_name = kernel.GetFinalPathNameByHandleW
    final_name.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    final_name.restype = wintypes.DWORD
    disposition = kernel.SetFileInformationByHandle
    disposition.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    disposition.restype = wintypes.BOOL

    # DELETE | FILE_READ_ATTRIBUTES, all sharing, OPEN_EXISTING,
    # FILE_FLAG_OPEN_REPARSE_POINT. Never follow a last-component link.
    handle = create(str(path), 0x10000 | 0x80, 1 | 2 | 4, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    fd = None
    try:
        fd = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
        info = os.fstat(fd)
        if not all(_same_identity(info, row) for row in rows):
            raise _UnsafeFile()
        name = ctypes.create_unicode_buffer(32768)
        length = final_name(handle, name, len(name), 0)
        if not length or length >= len(name):
            raise ctypes.WinError(ctypes.get_last_error())
        actual = name.value
        if actual.startswith("\\\\?\\UNC\\"):
            actual = "\\\\" + actual[8:]
        elif actual.startswith("\\\\?\\"):
            actual = actual[4:]
        if _canonical(Path(actual)) != _canonical(path) or not _plain_components(path):
            raise _UnsafeFile()
        marked = wintypes.BOOL(True)
        if not disposition(handle, 4, ctypes.byref(marked), ctypes.sizeof(marked)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if fd is not None:
            os.close(fd)  # Owns and closes the transferred Windows handle.
        else:
            close(handle)


def _snapshot(state: State) -> tuple[dict, dict, dict]:
    signatures = {}
    for row in state.conn.execute("SELECT id, signature FROM routes"):
        try:
            value = json.loads(row["signature"])
            signatures[row["id"]] = value if isinstance(value, dict) else {}
        except (ValueError, TypeError):
            signatures[row["id"]] = {}
    messages = { (row["route"], row["scope"], row["source_id"]): dict(row)
                 for row in state.conn.execute("SELECT * FROM messages") }
    pending = defaultdict(set)
    for row in state.conn.execute("SELECT route, scope, source_ids FROM operations"):
        try:
            ids = json.loads(row["source_ids"])
            if not isinstance(ids, list) or any(type(item) is not int for item in ids):
                raise ValueError()
            pending[(row["route"], row["scope"])].update(ids)
        except (ValueError, TypeError):
            pending[(row["route"], row["scope"])].add(None)
    return signatures, messages, pending


def _eligible(row: dict, signatures: dict, messages: dict, pending: dict) -> bool:
    route, scope, source_id = row["route"], row["scope"], row["source_id"]
    signature = signatures.get(route, {})
    mode = signature.get("comment_mode") if scope in {"comment", "comments"} else signature.get("mode")
    if scope not in _SCOPES or mode != "download_then_forward":
        return False
    message = messages.get((route, scope, source_id))
    def confirmed(item):
        return (item is not None and item["status"] == "done"
                and type(item["target_id"]) is int and item["target_id"] > 0)
    if not confirmed(message):
        return False
    uncertain = pending.get((route, scope), set())
    if None in uncertain or source_id in uncertain:
        return False
    grouped_id = message["grouped_id"]
    if grouped_id:
        group = [item for key, item in messages.items()
                 if key[:2] == (route, scope) and item["grouped_id"] == grouped_id]
        if any(not confirmed(item)
               or item["source_id"] in uncertain for item in group):
            return False
        # Accommodate explicit album membership from imported/newer records.
        for item in group:
            try:
                metadata = json.loads(item["metadata"])
                members = metadata.get("album_ids", []) if isinstance(metadata, dict) else []
                if not isinstance(members, list):
                    return False
                if any(type(member) is not int
                       or not confirmed(messages.get((route, scope, member)))
                       or member in uncertain for member in members):
                    return False
            except (ValueError, TypeError):
                return False
    return True


def _mark(state: State, rows: list[dict], status: str, error: str | None = None) -> None:
    for row in rows:
        values = (status, error, row["route"], row["scope"], row["source_id"], row["path"])
        for table in ("download_files", "downloads"):
            state.conn.execute(
                f"UPDATE {table} SET cleanup_status=?, cleanup_error=?, "
                "cleaned_at=CASE WHEN ? IN ('deleted','missing') THEN CURRENT_TIMESTAMP ELSE NULL END "
                "WHERE route=? AND scope=? AND source_id=? AND path=?",
                (values[0], values[1], values[0], *values[2:]),
            )


def _empty_directories(path: Path, root: Path) -> None:
    # Only the tool's exact id/scope/route directories, never root or a tree.
    directory = path.parent
    for _ in range(3):
        if directory == root:
            break
        try:
            if not _plain_components(directory):
                break
            directory.rmdir()
        except OSError:
            break
        directory = directory.parent


def cleanup_forwarded(
    state: State, *, roots: Iterable[str | Path] | str | Path = (),
    route_id: str | None = None, scope: str | None = None,
    source_ids: Iterable[int] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """Remove only registered, confirmed ``download_then_forward`` media.

    ``roots`` is explicit user authorization, normally ``[config.download_dir]``.
    Auto-cleanup must also pass the current route, scope and complete batch ids,
    after committing mappings and clearing its pending operation. Historical
    copies in another directory remain registered until that root is selected.
    """
    result = {"deleted_files": 0, "freed_bytes": 0, "missing_files": 0,
              "skipped_files": 0, "failed_files": 0, "failures": [],
              "skip_reasons": {}, "stopped": False}
    approved = _approved_roots(roots)
    selected_ids = set(source_ids) if source_ids is not None else None
    reasons = Counter()
    if state.conn.in_transaction:
        raise ValueError("Cleanup requires completed message-state transactions.")
    # Serialize checking pending/mappings and deleting files against a second
    # local process. A crash after unlink but before commit becomes 'missing'
    # on the next attempt, with zero newly freed bytes.
    with state.conn:
        state.conn.execute("BEGIN IMMEDIATE")
        state.conn.execute("""
            INSERT OR IGNORE INTO download_files(
                route, scope, source_id, path, size, root, st_dev, st_ino,
                mtime_ns, cleanup_status, cleaned_at, cleanup_error, updated_at)
            SELECT route, scope, source_id, path, size, root, st_dev, st_ino,
                mtime_ns, cleanup_status, cleaned_at, cleanup_error, updated_at FROM downloads
        """)
        references = defaultdict(list)
        for item in state.conn.execute("SELECT * FROM download_files WHERE cleanup_status NOT IN ('deleted','missing')"):
            row = dict(item)
            references[_canonical(Path(row["path"]))].append(row)
        signatures, messages, pending = _snapshot(state)
        for rows in references.values():
            selected = [row for row in rows if (route_id is None or row["route"] == route_id)
                        and (scope is None or row["scope"] == scope)
                        and (selected_ids is None or row["source_id"] in selected_ids)]
            if not selected:
                continue
            if should_stop and should_stop():
                result["stopped"] = True
                break
            try:
                locations = [_file_location(row, approved) for row in rows]
                if not all(locations):
                    reasons["未授权目录或路径结构不安全"] += 1
                    result["skipped_files"] += 1
                    continue
                if not all(_eligible(row, signatures, messages, pending) for row in rows):
                    reasons["未确认转发、其他模式、相册未完成或存在待核对记录"] += 1
                    result["skipped_files"] += 1
                    continue
                if any(any(row.get(field) is None for field in ("st_dev", "st_ino", "mtime_ns"))
                       for row in rows):
                    reasons["旧版记录缺少文件身份，已保留，请人工处理"] += 1
                    result["skipped_files"] += 1
                    continue
                path, root = locations[0]
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    _mark(state, rows, "missing")
                    result["missing_files"] += 1
                    continue
                if not all(_same_identity(info, row) for row in rows):
                    reasons["文件已变化、不是普通文件或存在共享硬链接"] += 1
                    result["skipped_files"] += 1
                    continue
                _unlink_verified(path, rows)
                _mark(state, rows, "deleted")
                result["deleted_files"] += 1
                result["freed_bytes"] += info.st_size
                _empty_directories(path, root)
            except _UnsafeFile:
                reasons["删除前文件身份或路径发生变化"] += 1
                result["skipped_files"] += 1
            except OSError as exc:
                # Never expose file names/message text or turn a completed
                # forward into a failure. Permission/locked files can retry.
                error = type(exc).__name__
                _mark(state, rows, "error", error)
                result["failed_files"] += 1
                result["failures"].append({"error_type": error, "errno": exc.errno})
    result["skip_reasons"] = dict(reasons)
    return result
