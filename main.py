from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Mapping

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.command import GreedyStr

if __package__:
    from .wevote.commands import VoteError, parse_choices, parse_create, parse_id
    from .wevote.formatting import HELP, format_list, format_notification, format_poll
    from .wevote.store import VoteStore
else:
    from wevote.commands import VoteError, parse_choices, parse_create, parse_id
    from wevote.formatting import HELP, format_list, format_notification, format_poll
    from wevote.store import VoteStore


PLUGIN_NAME = "astrbot_plugin_wevote"
POLL_INTERVAL = 5
SEND_TIMEOUT = 30


def can_manage(event: AstrMessageEvent, config: Mapping) -> bool:
    if event.is_admin():
        return True
    whitelist = config.get("initiator_whitelist", [])
    if isinstance(whitelist, list) and event.get_sender_id() in {
        str(value).strip() for value in whitelist if isinstance(value, (str, int))
        and not isinstance(value, bool)
    }:
        return True
    raw = getattr(event.message_obj, "raw_message", None)
    sender = raw.get("sender", {}) if isinstance(raw, Mapping) else {}
    return isinstance(sender, Mapping) and sender.get("role") in ("owner", "admin")


def room_key(event: AstrMessageEvent) -> str:
    # 群聊开启独立用户会话时，各成员的 UMO 不同，不能用 UMO 隔离计票。
    return json.dumps([str(event.get_platform_id()), str(event.get_self_id()),
                       str(event.get_group_id())], ensure_ascii=False)


class WeVotePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = VoteStore(StarTools.get_data_dir(PLUGIN_NAME) / "votes.sqlite3")
        self._task: asyncio.Task | None = None
        self._notification_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.notify_pending()
            except Exception:
                logger.exception("WeVote 结算任务失败，下轮重试。")
            await asyncio.sleep(POLL_INTERVAL)

    async def notify_pending(self) -> None:
        async with self._notification_lock:
            for poll in self.store.pending(time.time()):
                try:
                    sent = await asyncio.wait_for(
                        self.context.send_message(
                            poll.umo, MessageChain([Plain(format_notification(poll))])
                        ), timeout=SEND_TIMEOUT,
                    )
                    if not sent:
                        logger.warning(f"WeVote 投票 #{poll.id} 未匹配发送平台，保留待发送状态。")
                        continue
                    self.store.mark_notified(poll.id)
                except Exception:
                    logger.exception(f"WeVote 投票 #{poll.id} 结算发送失败，下轮重试。")

    async def terminate(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None
        self.store.close()

    async def _private_stats(self, event: AstrMessageEvent, text: str) -> bool:
        try:
            # OneBot 私聊 API 显式携带机器人 ID；失败时绝不把统计回退到群聊。
            await asyncio.wait_for(event.bot.send_private_msg(
                user_id=int(event.get_sender_id()),
                self_id=int(event.get_self_id()),
                message=[{"type": "text", "data": {"text": text}}],
            ), timeout=SEND_TIMEOUT)
            return True
        except Exception:
            logger.exception("WeVote 保密统计私聊发送失败。")
            return False

    @filter.command("投票")
    async def vote(self, event: AstrMessageEvent, args: GreedyStr):
        event.stop_event()
        if event.get_platform_name() != "aiocqhttp" or not event.get_group_id():
            yield event.plain_result("WeVote 仅支持 QQ / OneBot v11 群聊。")
            return
        user_id = event.get_sender_id()
        if not user_id:
            yield event.plain_result("无法识别发送者，未执行投票操作。")
            return
        parts = str(args).strip().split(maxsplit=1)
        if not parts or parts[0] == "帮助":
            yield event.plain_result(HELP)
            return
        command, tail = parts[0], parts[1] if len(parts) == 2 else ""
        room = room_key(event)
        manager = can_manage(event, self.config)
        try:
            if command in ("发起", "结束", "取消") and not manager:
                raise VoteError("仅群主、群管理员、AstrBot 管理员或白名单用户可执行此操作。")
            if command == "发起":
                poll = self.store.create(room, str(event.unified_msg_origin), user_id,
                                         parse_create(tail), time.time())
                text = format_poll(poll, include_stats=poll.public)
            elif command in ("选择", "改票"):
                tokens = tail.split()
                if len(tokens) < 2:
                    raise VoteError(f"格式：/投票 {command} 投票ID 选项编号，例如 12 1 3。")
                poll = self.store.cast(room, parse_id(tokens[0]), user_id,
                                       parse_choices(tokens[1:]), time.time(),
                                       change=command == "改票")
                text = f"投票 #{poll.id} {'改票' if command == '改票' else '投票'}成功。"
            elif command == "查看":
                poll = self.store.get(room, parse_id(tail.strip()), time.time())
                text = format_poll(poll, include_stats=poll.public)
                if not poll.public and manager and poll.status != "cancelled":
                    sent = await self._private_stats(event, format_poll(poll, include_stats=True))
                    text += ("\n保密统计已通过私聊发送。" if sent else
                             "\n保密统计私聊发送失败，请先添加机器人为好友后重试。")
            elif command == "列表":
                if tail:
                    raise VoteError("格式：/投票 列表")
                text = format_list(self.store.active(room, time.time()))
            elif command in ("结束", "取消"):
                poll = self.store.finish(room, parse_id(tail.strip()), time.time(),
                                         cancel=command == "取消")
                text = f"投票 #{poll.id} 已{'取消' if command == '取消' else '提前结束'}，结算通知将发送到本群。"
            else:
                text = HELP
        except VoteError as exc:
            text = str(exc)
        except sqlite3.Error:
            logger.exception("WeVote 数据库操作失败。")
            text = "投票数据读写失败，请管理员检查日志后重试。"
        yield event.plain_result(text)
