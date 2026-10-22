"""Read forum topics and forward to an existing topic, using MTProto only.

Forum topic IDs belong to a supergroup. They are not linked-channel discussion
roots. General has ID 1, but sends to General use ordinary group routing.
"""

from __future__ import annotations

from telethon import functions, helpers, types, utils


class ForumError(RuntimeError):
    """An incomplete or invalid forum operation that must not be guessed."""


def _topic_id(value, *, allow_default=False):
    minimum = 0 if allow_default else 1
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 2**31 - 1:
        raise ForumError("话题 ID 必须是有效的正整数；0 表示整个源群或目标默认位置。")
    return value


def _forum(peer):
    if not isinstance(peer, types.Channel) or not peer.megagroup or not peer.forum:
        raise ForumError("该聊天不是启用了话题的超级群组，请选择带话题的群组。")


async def _read(client, request, read):
    return await read(lambda: client(request)) if read is not None else await client(request)


def _row(topic):
    return {"id": topic.id, "title": topic.title or f"话题 {topic.id}",
            "closed": bool(topic.closed), "hidden": bool(topic.hidden)}


async def get_forum_topic(client, peer, topic_id, *, read=None):
    """Validate an existing topic without creating, joining, or reopening it."""
    _forum(peer)
    topic_id = _topic_id(topic_id)
    response = await _read(client, functions.messages.GetForumTopicsByIDRequest(
        peer=peer, topics=[topic_id]), read)
    for topic in response.topics:
        if isinstance(topic, types.ForumTopic) and topic.id == topic_id:
            return topic
    raise ForumError("所选话题不存在、已删除或当前账号不可见，请重新读取群组话题列表。")


async def list_forum_topics(client, peer, *, read=None, page_size=100):
    """Read every topic page, preserving real IDs including hidden General.

    A short page is not the end: Telegram may return fewer items than requested.
    Pagination uses all three server offsets and fails explicitly if a cursor
    cannot be advanced; callers must not mistake a partial list for completion.
    """
    _forum(peer)
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
        raise ForumError("话题列表分页大小须为 1 至 100。")
    offset_date, offset_id, offset_topic = None, 0, 0
    cursors, seen, rows = set(), set(), {}
    while True:
        response = await _read(client, functions.messages.GetForumTopicsRequest(
            peer=peer, offset_date=offset_date, offset_id=offset_id,
            offset_topic=offset_topic, limit=page_size), read)
        page = list(response.topics)
        if not page:
            break
        for topic in page:
            seen.add(topic.id)
            if isinstance(topic, types.ForumTopic):
                rows[topic.id] = _row(topic)
        count = getattr(response, "count", 0)
        if count and len(seen) >= count:
            break
        last = next((item for item in reversed(page) if isinstance(item, types.ForumTopic)), None)
        if last is None:
            raise ForumError("话题列表分页未返回可继续读取的游标，请稍后重新读取。")
        if response.order_by_create_date:
            date = last.date
        else:
            top = next((item for item in response.messages if item.id == last.top_message), None)
            if top is None:
                raise ForumError("话题列表缺少下一页所需的消息时间，未将不完整列表作为完成结果。请重试。")
            date = top.date
        cursor = (date, last.top_message, last.id)
        if cursor in cursors:
            raise ForumError("话题列表分页没有继续前进，请稍后重新读取；尚未采用不完整列表。")
        cursors.add(cursor)
        offset_date, offset_id, offset_topic = cursor
    if 1 not in rows:
        general = await get_forum_topic(client, peer, 1, read=read)
        rows[1] = _row(general)
    return sorted(rows.values(), key=lambda item: (item["id"] != 1, item["title"].casefold(), item["id"]))


async def forward_to_topic(client, destination, batch, from_peer, topic_id):
    """Forward one ordered batch, with complete random-ID response mapping.

    Do not retry this helper after a network failure: Telegram may have accepted
    the write. The mover owns pending markers and definite-rejection retries.
    High-level Telethon forward_messages has no target forum-topic parameter.
    """
    topic_id = _topic_id(topic_id, allow_default=True)
    batch = list(batch)
    ids = [item.id if isinstance(item, types.Message) else item for item in batch]
    if (not ids or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in ids)
            or len(set(ids)) != len(ids)):
        raise ForumError("转发话题需要一组有效的源消息 ID。")
    destination = await client.get_input_entity(destination)
    from_peer = await client.get_input_entity(from_peer)
    random_ids, seen = [], set()
    for _ in ids:
        value = helpers.generate_random_long()
        while value == 0 or value in seen:
            value = helpers.generate_random_long()
        random_ids.append(value)
        seen.add(value)
    request = functions.messages.ForwardMessagesRequest(
        from_peer=from_peer, id=ids, to_peer=destination, random_id=random_ids,
        top_msg_id=topic_id if topic_id > 1 else None,
    )
    result = await client(request)
    updates = ([result.update] if isinstance(result, types.UpdateShort)
               else result.updates if isinstance(result, (types.Updates, types.UpdatesCombined)) else [])
    random_to_id = {item.random_id: item.id for item in updates if isinstance(item, types.UpdateMessageID)}
    message_ids = {item.message.id for item in updates
                   if isinstance(item, (types.UpdateNewChannelMessage, types.UpdateNewMessage))}
    if any(random_to_id.get(value) not in message_ids for value in random_ids):
        # Telethon's generic mapper logs the entire result on missing mappings,
        # including private message bodies. Reject safely before that warning.
        raise ForumError("话题转发未获得完整目标消息 ID，请保留待核对记录并人工核对目标，勿直接重发。")
    mapped = client._get_response_message(request, result, destination)
    messages = list(mapped) if isinstance(mapped, (list, tuple)) else [mapped]
    if len(messages) != len(ids) or any(not isinstance(item, types.Message) for item in messages):
        raise ForumError("话题转发未获得完整目标消息 ID，请保留待核对记录并人工核对目标，勿直接重发。")
    if len({item.id for item in messages}) != len(messages):
        raise ForumError("话题转发返回了重复目标 ID，请人工核对待确认的发送，勿直接重发。")
    for message in messages:
        if utils.get_peer_id(message.peer_id) != utils.get_peer_id(destination):
            raise ForumError("话题转发返回了不同目标群组，已停止；请人工核对待确认的发送。")
        header = message.reply_to
        actual = (getattr(header, "reply_to_top_id", None) or getattr(header, "reply_to_msg_id", None)) if header else None
        if topic_id > 1 and actual != topic_id:
            raise ForumError("话题转发返回的目标话题不匹配，已停止；请人工核对待确认的发送。")
        if topic_id <= 1 and header and getattr(header, "forum_topic", False) and actual not in (None, 1):
            raise ForumError("预期发送到通用话题，但返回了其他话题；请人工核对待确认的发送。")
    return messages
