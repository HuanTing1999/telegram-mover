"""Editable, incomplete GUI drafts without borrowing login credentials.

The runtime loader deliberately validates a complete configuration. This module
instead lets the desktop application open and save its first, incomplete draft.
Relative session/storage paths remain untouched and are resolved by load_config
against the selected configuration file when a run starts.
"""

from __future__ import annotations

import copy
import os
import sys
import tempfile
import tomllib
from dataclasses import asdict
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import tomli_w

from .filters import FilterSettings
from .limits import LimitSettings


class ConfigDraftError(ValueError):
    """An editable draft cannot be loaded or safely saved."""


_LOGIN_KEYS = frozenset({
    "phone", "phonenumber", "telephone", "mobilephone", "otp", "code",
    "verificationcode", "logincode", "phonecode", "phonecodehash", "smscode",
    "password", "passwd", "passphrase", "authkey", "authorizationkey",
    "token", "bottoken", "accesstoken", "authtoken", "refreshtoken",
    "sessionstring", "stringsession",
})
_TABLES = ("telegram", "sync", "storage", "limits", "filters")


def default_config_path() -> Path:
    """Use the user's existing root-level config, including frozen launches."""
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
    return base / "config.toml"


def _template_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)) / "config.example.toml"
    return Path(__file__).resolve().parents[1] / "config.example.toml"


def _selected_path(path: str | Path) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise ConfigDraftError("配置文件路径无效，请重新选择配置文件。") from exc


def _merge(defaults: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Copy both sides; preserve all existing values and unknown fields."""
    result = copy.deepcopy(defaults)
    for key, value in values.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _validate(draft: Any, *, reject_login: bool = True) -> None:
    if not isinstance(draft, dict):
        raise ConfigDraftError("配置草稿必须是 TOML 字典。")
    active: set[int] = set()

    def walk(value: Any, location: str) -> None:
        if isinstance(value, (dict, list, tuple)):
            if id(value) in active:
                raise ConfigDraftError("配置草稿包含循环引用，无法保存为 TOML。")
            active.add(id(value))
            try:
                if isinstance(value, dict):
                    for key, item in value.items():
                        if not isinstance(key, str):
                            raise ConfigDraftError("TOML 配置项名称必须是文本。")
                        normalized = "".join(character for character in key.casefold() if character.isalnum())
                        if reject_login and (normalized in _LOGIN_KEYS or normalized.endswith(("password", "token", "otp"))):
                            raise ConfigDraftError("配置草稿不能保存手机号、验证码、登录密码或令牌；请仅在登录窗口临时输入。")
                        walk(item, f"{location}.{key}" if location else key)
                else:
                    for item in value:
                        walk(item, location)
            finally:
                active.remove(id(value))
        elif not isinstance(value, (str, bool, int, float, datetime, date, time)):
            # Never echo values or their repr: an invalid value might be a secret.
            raise ConfigDraftError("配置草稿包含 TOML 不支持的值；请使用文本、数字、布尔值、日期、数组或表。")

    try:
        walk(draft, "")
    except RecursionError as exc:
        raise ConfigDraftError("配置草稿嵌套层数过多，无法保存为 TOML。") from exc
    for section in _TABLES:
        if section in draft and not isinstance(draft[section], dict):
            raise ConfigDraftError(f"[{section}] 必须是 TOML 表。")
    try:
        # The writer and parser together also reject malformed datetime values,
        # unsupported subclasses, and other TOML structural problems up front.
        tomllib.loads(tomli_w.dumps(draft))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ConfigDraftError("配置草稿无法表示为有效 TOML；请检查字段类型和嵌套结构。") from exc


def draft_defaults() -> dict[str, dict]:
    """Read bundled examples and add every configurable policy default."""
    template = _template_path()
    try:
        with template.open("rb") as handle:
            values = tomllib.load(handle)
    except (OSError, ValueError, UnicodeError) as exc:
        raise ConfigDraftError("无法读取软件附带的配置示例，请检查安装文件是否完整。") from exc
    defaults: dict[str, Any] = {
        "telegram": {"api_id": 0, "api_hash": ""},
        "sync": {"source_topic_id": 0, "target_topic_id": 0},
        "storage": {"auto_cleanup_forwarded": False, "download_connections": 4},
        "limits": asdict(LimitSettings()),
        "filters": asdict(FilterSettings()),
    }
    result = _merge(defaults, values)
    _validate(result)
    return result


def read_draft(path: str | Path) -> dict[str, dict]:
    """Read without writing/network access, accepting incomplete application IDs.

    Environment credentials are deliberately never read into an editable draft.
    Unknown fields survive a read/edit/save round trip for manual review.
    """
    selected = _selected_path(path)
    defaults = draft_defaults()
    try:
        with selected.open("rb") as handle:
            values = tomllib.load(handle)
    except FileNotFoundError:
        return defaults
    except tomllib.TOMLDecodeError as exc:
        # Parser exception text can include offending source values.
        line = getattr(exc, "lineno", None)
        suffix = f"（第 {line} 行）" if line else ""
        raise ConfigDraftError(f"配置文件不是有效 TOML{suffix}，请检查引号、表名与字段格式。") from exc
    except UnicodeError as exc:
        raise ConfigDraftError("配置文件必须使用 UTF-8 文本编码。") from exc
    except OSError as exc:
        raise ConfigDraftError("无法读取所选配置文件，请检查路径与读取权限。") from exc
    _validate(values)
    result = _merge(defaults, values)
    _validate(result)
    return result


def effective_credentials_hint() -> str:
    """Mention environment overrides by name only, including empty overrides."""
    names = [name for name in ("API_ID", "API_HASH") if name in os.environ]
    if names:
        return "检测到环境变量 " + "、".join(names) + "；运行时它们会优先覆盖配置中的同名凭证。"
    return "未检测到 API_ID / API_HASH 环境变量；运行时使用配置文件中的应用凭证。"


def write_draft(path: str | Path, draft: dict[str, Any]) -> None:
    """Atomically replace only the supplied TOML draft; never persist login data."""
    _validate(draft)
    selected = _selected_path(path)
    temporary: Path | None = None
    try:
        # A sibling keeps os.replace on the same filesystem. Opening in binary
        # mode avoids Windows newline conversion and satisfies tomli_w.dump.
        descriptor, name = tempfile.mkstemp(prefix=f".{selected.name}.", suffix=".tmp", dir=selected.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            tomli_w.dump(draft, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, selected)
        temporary = None
    except (OSError, TypeError, ValueError, OverflowError) as exc:
        raise ConfigDraftError("配置保存失败，原配置已保留。请检查文件夹权限、可用空间或文件占用后重试。") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
