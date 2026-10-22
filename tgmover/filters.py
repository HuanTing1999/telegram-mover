"""Deterministic text/link advertising rules; no network, OCR, or media AI."""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass, replace
from urllib.parse import unquote, urlsplit

import regex
from telethon import types, utils


@dataclass(frozen=True)
class FilterSettings:
    enabled: bool = True
    keywords: tuple[str, ...] = (
        "广告合作", "商务合作", "付费推广", "广告投放", "开户链接", "送彩金",
        "paid promotion", "sponsored post",
    )
    regex_patterns: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()
    allowed_keywords: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()
    block_all_links: bool = False
    block_buttons: bool = False
    apply_to_comments: bool = True
    apply_to_downloads: bool = False
    blocked_message_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    blocked: bool
    reasons: tuple[str, ...] = ()
    matched_ids: tuple[int, ...] = ()


class FilterConfigError(ValueError):
    """A rule is malformed or exceeds the documented resource bounds."""


_BOOL_FIELDS = ("enabled", "block_all_links", "block_buttons", "apply_to_comments", "apply_to_downloads")
_SEQUENCE_LIMITS = {
    "keywords": (200, 256), "allowed_keywords": (200, 256),
    "regex_patterns": (30, 1024),
    "blocked_domains": (200, 253), "allowed_domains": (200, 253),
    "blocked_message_keys": (2000, 40),
}
_URL = re.compile(r'''(?<![a-z0-9_@.-])(?:https?://|www\.|t\.me/)[^\s<>\[\]{}()"'，。；！？”’]+''', re.IGNORECASE)
_KEY = re.compile(r"(-?[1-9][0-9]{0,18}):([1-9][0-9]{0,9})\Z")


def _normal(text: str, *, compact: bool = True) -> str:
    result = unicodedata.normalize("NFKC", text).casefold()
    return "".join(char for char in result
                   if unicodedata.category(char) != "Cf" and (not compact or not char.isspace()))


def normalize_domain(value: str) -> str:
    """Normalize a host only, never reinterpret a URL/userinfo as a domain."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise FilterConfigError("域名必须为非空主机名，不能含首尾空白。")
    value = unicodedata.normalize("NFKC", value).casefold().removesuffix(".")
    if not value or any(char.isspace() or unicodedata.category(char).startswith("C") for char in value):
        raise FilterConfigError("域名不能包含空白或控制字符。")
    if any(char in value for char in "/\\@:#?*[]"):
        raise FilterConfigError("域名只填写主机名，不含协议、端口、路径、用户信息或通配符。")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    try:
        ascii_host = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise FilterConfigError("域名不是有效的 IDNA 主机名。") from exc
    labels = ascii_host.split(".")
    if (len(ascii_host) > 253 or len(labels) < 2
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)):
        raise FilterConfigError("域名必须是有效的多级主机名，例如 ads.example.com。")
    return ascii_host


def _compile(pattern: str):
    try:
        return regex.compile(pattern, regex.IGNORECASE | regex.VERSION1)
    except (regex.error, OverflowError, RecursionError) as exc:
        raise FilterConfigError(f"正则表达式无效：{exc}") from exc


def validate_filter_settings(settings: FilterSettings) -> FilterSettings:
    """Validate limits and return a copy with canonical IDNA domain tuples."""
    for field in _BOOL_FIELDS:
        if not isinstance(getattr(settings, field), bool):
            raise FilterConfigError(f"filters.{field} 必须为 true 或 false。")
    for field, (count, length) in _SEQUENCE_LIMITS.items():
        values = getattr(settings, field)
        if not isinstance(values, tuple) or len(values) > count:
            raise FilterConfigError(f"filters.{field} 必须是最多 {count} 项的字符串列表。")
        for value in values:
            if not isinstance(value, str) or not value.strip() or len(value) > length:
                raise FilterConfigError(f"filters.{field} 每项必须为 1–{length} 字符的非空字符串。")
            if field in {"keywords", "allowed_keywords"} and not _normal(value):
                raise FilterConfigError(f"filters.{field} 关键词不能仅含空白或零宽字符。")
    for pattern in settings.regex_patterns:
        _compile(pattern)
    for key in settings.blocked_message_keys:
        match = _KEY.fullmatch(key)
        if (not match or abs(int(match[1])) > 2**63 - 1 or int(match[2]) > 2**31 - 1):
            raise FilterConfigError("filters.blocked_message_keys 使用带符号聊天ID:正消息ID，例如 '-1001234567890:42'。")
    return replace(settings,
                   blocked_domains=tuple(normalize_domain(value) for value in settings.blocked_domains),
                   allowed_domains=tuple(normalize_domain(value) for value in settings.allowed_domains))


def _host(url: str) -> str | None:
    url = _normal(url, compact=False).strip().rstrip(".,;!?:，。；！？”’")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url):
        url = "https://" + url
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        # Accessing .port also rejects malformed or out-of-range port numbers.
        parsed.port
        return normalize_domain(unquote(host, errors="strict")) if host else None
    except (ValueError, UnicodeError):
        return None


def _entity_text(text: str, offset: int, length: int) -> str:
    """Telegram offsets count UTF-16 code units, rather than Python codepoints."""
    if not isinstance(offset, int) or not isinstance(length, int) or offset < 0 or length <= 0:
        return ""
    return text.encode("utf-16-le", errors="surrogatepass")[offset * 2:(offset + length) * 2].decode(
        "utf-16-le", errors="ignore")


def _content(message):
    text = getattr(message, "message", "") or ""
    fields, urls = [text], []
    filename = getattr(getattr(message, "file", None), "name", None)
    if filename:
        fields.append(filename)
    for entity in getattr(message, "entities", None) or ():
        if isinstance(entity, types.MessageEntityTextUrl):
            fields.append(entity.url)
            urls.append(entity.url)
        elif isinstance(entity, types.MessageEntityUrl):
            target = _entity_text(text, entity.offset, entity.length)
            if target:
                urls.append(target)
    buttons = []
    for row in getattr(getattr(message, "reply_markup", None), "rows", ()) or ():
        for button in getattr(row, "buttons", ()):
            buttons.append(button)
            fields.append(getattr(button, "text", "") or "")
            # Current TL layers put the action inside button.type; older
            # layers used KeyboardButtonUrl with a direct .url attribute.
            target = getattr(button, "url", None) or getattr(getattr(button, "type", None), "url", None)
            if target:
                fields.append(target)
                urls.append(target)
    for field in fields:
        urls.extend(_URL.findall(_normal(field, compact=False)))
    return fields, list(dict.fromkeys(urls)), bool(buttons)


def _domain_matches(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


class AdFilter:
    """Evaluate one message/album with explainable, bounded local rules.

    Manual blocks take priority; a whitelisted keyword releases an album from
    automatic rules. Domain allows
    only exempt the optional all-links rule; they never override block rules.
    Regex searches have a hard per-search timeout and timeout blocks the album.
    """

    def __init__(self, settings: FilterSettings):
        self.settings = validate_filter_settings(settings)
        self._keywords = tuple((value, _normal(value)) for value in self.settings.keywords)
        self._allowed_keywords = tuple(_normal(value) for value in self.settings.allowed_keywords)
        self._patterns = tuple(_compile(pattern) for pattern in self.settings.regex_patterns)
        self._blocked_keys = frozenset(self.settings.blocked_message_keys)

    def evaluate(self, batch, scope: str = "post", mode: str = "reupload") -> Decision:
        settings = self.settings
        if (not settings.enabled or (scope in {"comment", "comments"} and not settings.apply_to_comments)
                or (mode == "download_only" and not settings.apply_to_downloads)):
            return Decision(False)
        content = [(message, *_content(message)) for message in batch]
        manual_ids = tuple(getattr(message, "id", 0) for message, _, _, _ in content
                           if getattr(message, "peer_id", None) is not None
                           and f"{utils.get_peer_id(message.peer_id)}:{message.id}" in self._blocked_keys)
        if manual_ids:
            return Decision(True, ("手动屏蔽的聊天/消息ID",), tuple(dict.fromkeys(manual_ids)))
        for _, fields, _, _ in content:
            if any(allowed in _normal(field) for allowed in self._allowed_keywords for field in fields):
                return Decision(False, ("命中白名单关键词：整组放行",))
        reasons, matched_ids = [], []
        for message, fields, urls, buttons in content:
            matches = []
            identifier = getattr(message, "id", 0)
            texts = tuple(_normal(field) for field in fields)
            for original, normalized in self._keywords:
                if any(normalized in text for text in texts):
                    matches.append(f"关键词：{original}")
            for number, pattern in enumerate(self._patterns, 1):
                try:
                    if any(pattern.search(text, timeout=0.05) for field in fields
                           for text in dict.fromkeys((_normal(field, compact=False), _normal(field)))):
                        matches.append(f"正则规则 #{number}")
                except TimeoutError:
                    matches.append(f"正则规则 #{number} 超过 0.05 秒，保守拦截整组")
                    return Decision(True, tuple(dict.fromkeys([*reasons, *matches])),
                                    tuple(dict.fromkeys([*matched_ids, identifier])))
            for url in urls:
                host = _host(url)
                if host and _domain_matches(host, settings.blocked_domains):
                    matches.append(f"域名黑名单：{host}")
                if settings.block_all_links and (not host or not _domain_matches(host, settings.allowed_domains)):
                    matches.append(f"全部链接规则：{host or '无法识别域名的链接'}")
            if settings.block_buttons and buttons:
                matches.append("全部按钮规则")
            if matches:
                reasons.extend(matches)
                matched_ids.append(identifier)
        return Decision(bool(matched_ids), tuple(dict.fromkeys(reasons)), tuple(dict.fromkeys(matched_ids)))
