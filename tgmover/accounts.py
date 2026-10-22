"""Local account/session index; never stores login inputs or app credentials."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata

from .login_input import normalize_phone


class AccountStoreError(ValueError):
    """The account index is invalid, conflicting, or unavailable."""


def _session_path(value: str | Path) -> Path:
    try:
        candidate = str(value)
        if not candidate or "\x00" in candidate:
            raise ValueError
        # This is the same suffix rule used by Telethon's SQLiteSession.
        if candidate.casefold().endswith(".session"):
            candidate = candidate[:-8] + ".session"
        else:
            candidate += ".session"
        return Path(candidate).expanduser().resolve()
    except (OSError, TypeError, ValueError, RuntimeError):
        raise AccountStoreError("会话文件路径无效，请重新选择会话。") from None


def _session_key(path: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()[:24]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AccountStoreError("本地账号索引包含重复字段，原文件未被修改。")
        result[key] = value
    return result


def _label(value: str, identifier: int) -> str:
    if not isinstance(value, str) or len(value) > 120 or any(unicodedata.category(char).startswith("C") for char in value):
        raise AccountStoreError("账号显示名称须为不含控制字符、最多 120 字符的文本。")
    value = value.strip()
    # Caller labels should be first_name/username, never a phone fallback.
    # Avoid persisting an accidentally supplied phone in an otherwise safe label.
    if re.search(r"(?<![\w])\+?\d[\d\s()\-]{5,}\d(?![\w])", value):
        return f"账号 {identifier}"
    return value or f"账号 {identifier}"


class AccountStore:
    def __init__(self, config_path: str | Path):
        try:
            self.config_path = Path(config_path).expanduser().resolve()
        except (OSError, TypeError, ValueError, RuntimeError):
            raise AccountStoreError("配置文件路径无效，无法读取本地账号列表。") from None
        self.path = self.config_path.parent / "data" / "accounts.json"

    def session_for_phone(self, phone: str) -> Path:
        """Compute a stable opaque session stem without creating any files."""
        canonical = normalize_phone(phone)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
        return self.config_path.parent / "data" / "accounts" / digest

    def list_accounts(self) -> list[dict]:
        try:
            if not self.path.exists():
                return []
            if self.path.stat().st_size > 1024 * 1024:
                raise AccountStoreError("本地账号索引过大，未读取或覆盖；请先检查索引文件。")
            with self.path.open("r", encoding="utf-8") as handle:
                rows = json.load(handle, object_pairs_hook=_unique_object)
        except AccountStoreError:
            raise
        except (OSError, UnicodeError, ValueError, RecursionError):
            raise AccountStoreError("本地账号索引损坏或不可读取，原文件未被修改。") from None
        if not isinstance(rows, list) or len(rows) > 1000:
            raise AccountStoreError("本地账号索引结构无效，原文件未被修改。")
        seen = set()
        result = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"id", "key", "label", "session"}:
                raise AccountStoreError("本地账号索引字段无效，原文件未被修改。")
            identifier = row["id"]
            if isinstance(identifier, bool) or not isinstance(identifier, int) or not 1 <= identifier <= 2**63 - 1:
                raise AccountStoreError("本地账号索引的账号标识无效，原文件未被修改。")
            if (not isinstance(row["session"], str) or not Path(row["session"]).is_absolute()
                    or not isinstance(row["key"], str) or not re.fullmatch(r"[0-9a-f]{24}", row["key"])):
                raise AccountStoreError("本地账号索引的会话信息无效，原文件未被修改。")
            normalized = _session_path(row["session"])
            key = _session_key(normalized)
            if key != row["key"] or key in seen or _label(row["label"], identifier) != row["label"]:
                raise AccountStoreError("本地账号索引存在冲突或不安全字段，原文件未被修改。")
            seen.add(key)
            result.append({"id": identifier, "key": key, "label": row["label"], "session": str(normalized)})
        return result

    @contextmanager
    def _lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = None
        locked = False
        try:
            handle = self.path.with_suffix(".lock").open("a+b")
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            yield
        except OSError:
            raise AccountStoreError("本地账号列表暂时不可写或正被其他进程使用，原索引已保留。") from None
        finally:
            if handle is not None:
                if locked:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def remember(self, user_id: int, label: str, session: Path) -> dict:
        """Register an already-authorized session; never move or delete it."""
        if isinstance(user_id, bool) or not isinstance(user_id, int) or not 1 <= user_id <= 2**63 - 1:
            raise AccountStoreError("只能登记已登录账号的有效用户标识。")
        normalized, display = _session_path(session), _label(label, user_id)
        if not normalized.is_file():
            raise AccountStoreError("登录会话文件尚不存在，请先完成该账号登录。")
        item = {"id": user_id, "key": _session_key(normalized), "label": display, "session": str(normalized)}
        temporary = None
        with self._lock():
            rows = self.list_accounts()
            existing = next((row for row in rows if row["key"] == item["key"]), None)
            if existing and (existing["id"] != user_id or _session_path(existing["session"]) != normalized):
                raise AccountStoreError("该会话已登记为其他账号，未覆盖原记录。请重新核对会话。")
            updated = [item if row["key"] == item["key"] else row for row in rows]
            if existing is None:
                if len(rows) >= 1000:
                    raise AccountStoreError("本地账号索引已达到 1000 条上限，未覆盖原记录。")
                updated.append(item)
            try:
                descriptor, name = tempfile.mkstemp(prefix=".accounts-", suffix=".tmp", dir=self.path.parent)
                temporary = Path(name)
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(updated, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                temporary = None
            except (OSError, ValueError, UnicodeError):
                raise AccountStoreError("账号列表保存失败，原索引与会话文件已保留。") from None
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
        return dict(item)
