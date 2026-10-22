"""Interpret chat inputs before passing them to Telethon's entity resolver.

Telegram message URLs identify a message inside a chat. Telethon get_entity
expects the chat itself, and numeric strings otherwise look like phone numbers.
This module performs no network requests and never joins a chat.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit


class PeerReferenceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ChatReference:
    peer: object = field(repr=False)
    kind: str
    from_message: bool = False
    allow_title: bool = False


_HOSTS = frozenset({"t.me", "telegram.me", "telegram.dog"})
_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}\Z", re.ASCII)
_INVITE = re.compile(r"[A-Za-z0-9_-]+\Z", re.ASCII)
_SPECIAL = frozenset({"share", "iv", "proxy", "socks", "addstickers", "addemoji", "addlist",
                      "setlanguage", "login", "confirmphone", "bg", "giftcode", "boost", "nft"})
_MAX_ID = 2**63 - 1


def _integer(value):
    number = int(value)
    if not number or not -_MAX_ID <= number <= _MAX_ID:
        raise PeerReferenceError("聊天 ID 必须是非零有效整数。")
    return number


def _channel(value):
    if not re.fullmatch(r"[0-9]+", value) or not 0 < int(value) <= _MAX_ID - 10**12:
        raise PeerReferenceError("私有频道帖子链接中的频道 ID 无效，请重新复制链接。")
    # This is Telethon's marked channel-ID formula, not string concatenation.
    return -(10**12 + int(value))


def _username(value):
    if not _USERNAME.fullmatch(value):
        raise PeerReferenceError("Telegram 用户名格式无效，请填写 @用户名或频道／群组链接。")
    return "@" + value


def _invite(value):
    if not _INVITE.fullmatch(value):
        raise PeerReferenceError("Telegram 邀请链接格式无效，请重新复制链接。")
    return ChatReference("https://t.me/+" + value, "invite")


def parse_chat_reference(value) -> ChatReference:
    if isinstance(value, bool) or value is None:
        raise PeerReferenceError("请填写频道／群组链接、@用户名或聊天 ID。")
    if isinstance(value, int):
        return ChatReference(_integer(value), "id")
    if not isinstance(value, str):
        # Internal callers already have Telegram Peer/InputPeer/Channel objects.
        return ChatReference(value, "entity")
    value = value.strip()
    if not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise PeerReferenceError("聊天地址为空或格式无效，请重新复制频道／群组链接。")
    if re.fullmatch(r"-?[0-9]+", value):
        return ChatReference(_integer(value), "id")
    if value.startswith("@"):
        return ChatReference(_username(value[1:]), "username")
    if re.match(r"(?i)^(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/", value):
        value = "https://" + value
    if "://" not in value:
        if "/" in value or value.startswith("+"):
            raise PeerReferenceError("请填写完整 Telegram 链接、@用户名或聊天 ID。")
        if _USERNAME.fullmatch(value):
            # Bare English names might be a username or an exact dialog title.
            return ChatReference(value, "username", allow_title=True)
        return ChatReference(value, "title", allow_title=True)
    try:
        url = urlsplit(value)
        if url.username or url.password or url.port:
            raise ValueError
    except ValueError:
        raise PeerReferenceError("Telegram 链接格式无效，请重新复制。") from None
    if url.scheme.lower() == "tg":
        query = parse_qs(url.query, keep_blank_values=True)
        if "direct" in query:
            raise PeerReferenceError("此链接指向频道私信界面，请改用频道主页或普通帖子链接。")

        def one(key):
            values = query.get(key, [])
            if len(values) != 1 or not values[0]:
                raise PeerReferenceError("Telegram 链接缺少有效的聊天信息，请重新复制。")
            return values[0]

        action = url.netloc.lower()
        if action == "resolve":
            return ChatReference(_username(one("domain")), "username", from_message="post" in query)
        if action == "privatepost":
            return ChatReference(_channel(one("channel")), "id", from_message=True)
        if action == "join":
            return _invite(one("invite"))
        raise PeerReferenceError("此 Telegram 链接不是频道／群组或帖子链接。")
    host = (url.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if url.scheme.lower() not in {"http", "https"} or host not in _HOSTS:
        raise PeerReferenceError("请使用 Telegram 的 t.me、telegram.me 链接或 @用户名。")
    if "direct" in parse_qs(url.query, keep_blank_values=True):
        raise PeerReferenceError("此链接指向频道私信界面，请改用频道主页或普通帖子链接。")
    path = unquote(url.path).strip("/")
    parts = path.split("/") if path else []
    if not parts or any(not part for part in parts):
        raise PeerReferenceError("链接缺少频道／群组地址，请复制完整链接。")
    if parts[0] == "joinchat":
        if len(parts) != 2:
            raise PeerReferenceError("Telegram 邀请链接格式无效，请重新复制。")
        return _invite(parts[1])
    if parts[0].startswith("+"):
        if len(parts) != 1:
            raise PeerReferenceError("Telegram 邀请链接格式无效，请重新复制。")
        return _invite(parts[0][1:])
    if parts[0] == "c":
        if len(parts) not in {3, 4} or any(not p.isascii() or not p.isdigit() or int(p) <= 0 for p in parts[2:]):
            raise PeerReferenceError("私有帖子链接应包含频道 ID 和帖子 ID，请重新复制完整链接。")
        return ChatReference(_channel(parts[1]), "id", from_message=True)
    if parts[0] == "s":
        parts = parts[1:]
    if (not parts or parts[0] in _SPECIAL or len(parts) > 3
            or any(not p.isascii() or not p.isdigit() or int(p) <= 0 for p in parts[1:])):
        raise PeerReferenceError("此链接不是可识别的频道／群组或帖子链接，请重新复制。")
    return ChatReference(_username(parts[0]), "username", from_message=len(parts) > 1)
