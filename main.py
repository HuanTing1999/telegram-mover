#!/usr/bin/env python3
"""Run from this directory: python main.py --config config.toml."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import sqlite3

from telethon import TelegramClient, errors

from tgmover.auth import AuthError, login
from tgmover.config import ConfigError, load_config
from tgmover.engine import Mover, SyncError
from tgmover.downloads import DownloadError
from tgmover.limits import LimitError, LimitStop, clear_stop, status as limits_status
from tgmover.state import State


class ProcessLock:
    """OS-released lock: also prevents two processes sharing the user session."""
    def __init__(self, path: Path):
        self.path, self.handle = path, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            raise SyncError(f"另一个进程正在使用会话或状态：{self.path}") from exc
        return self

    def __exit__(self, *_):
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


async def run(config, state, *, check=False):
    config.session.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(config.session), config.api_id, config.api_hash,
                            flood_sleep_threshold=0, request_retries=0,
                            raise_last_call_error=True, receive_updates=False)
    try:
        await login(client)
        mover = Mover(client, config, state)
        if check:
            await mover.prepare()
            logging.info("检查通过：用户会话、源/目标和评论区配置可解析；未发送内容。")
        else:
            await mover.run()
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description="Telegram 用户账号媒体备份与频道/群组增量搬运")
    parser.add_argument("--config", default="config.toml", help="TOML 配置文件")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="登录并检查聊天和 linked chat，不发送内容")
    action.add_argument("--once", action="store_true", help="覆盖 poll_interval，只运行一轮")
    action.add_argument("--validate-config", action="store_true", help="仅检查配置，不连接 Telegram")
    action.add_argument("--show-pending", action="store_true", help="离线查看发送结果不确定的任务")
    action.add_argument("--retry-pending", metavar="KEY", help="确认目标已人工清理后解除该任务阻塞；本次不发送")
    action.add_argument("--limits-status", action="store_true", help="离线查看账号额度、冷却和停止状态")
    action.add_argument("--resume-account", type=int, metavar="ACCOUNT_ID", help="人工检查账号后解除停止；不清计数/冷却，本次不发送")
    parser.add_argument("--route", help="恢复操作对应的 route；多任务同名时必须指定")
    args = parser.parse_args()
    if args.route and not args.retry_pending:
        parser.error("--route 只能与 --retry-pending 一起使用")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    try:
        config = load_config(args.config)
        if args.validate_config:
            print("配置有效。尚未连接 Telegram。")
            return 0
        if args.once:
            config = replace(config, poll_interval=0)
        session_file = str(config.session)
        if not session_file.endswith(".session"):
            session_file += ".session"
        locks = {Path(str(config.database) + ".lock"), Path(session_file + ".mover.lock")}
        with ExitStack() as stack:
            for path in sorted(locks, key=str):
                stack.enter_context(ProcessLock(path))
            state = State(config.database)
            stack.callback(state.close)
            if args.limits_status:
                print(json.dumps(limits_status(state), ensure_ascii=False, indent=2))
                return 0
            if args.resume_account is not None:
                accounts = limits_status(state)
                if not any(row["account_id"] == args.resume_account and row["stopped_reason"] for row in accounts):
                    raise SyncError("找不到该账号的停止记录；请先用 --limits-status 核对。")
                clear_stop(state, args.resume_account)
                print("已解除账号停止标记。发送计数及服务器冷却时间保留；本次未连接或发送。")
                return 0
            if args.show_pending:
                rows = [dict(row) for row in state.conn.execute("SELECT * FROM operations ORDER BY started_at")]
                print(json.dumps(rows, ensure_ascii=False, indent=2))
                return 0
            if args.retry_pending:
                rows = [dict(row) for row in state.conn.execute("SELECT * FROM operations WHERE key=?", (args.retry_pending,))]
                if args.route:
                    rows = [row for row in rows if row["route"] == args.route]
                if len(rows) != 1:
                    raise SyncError("未找到唯一的待确认任务；用 --show-pending 查看并按需指定 --route。")
                row = rows[0]
                state.finish_operation(row["route"], row["scope"], row["key"])
                print("已解除阻塞。请确保目标中该次不确定发送的内容已清理，下次运行将重新处理。")
                return 0
            asyncio.run(run(config, state, check=args.check))
        logging.info("完成。映射、下载记录及增量进度已保存。")
        return 0
    except LimitStop as exc:
        logging.info("已暂停：%s", exc)
        return 0
    except (ConfigError, AuthError, SyncError, DownloadError, LimitError, OSError, sqlite3.Error, errors.RPCError) as exc:
        logging.error("%s", exc)
        return 2
    except (KeyboardInterrupt, EOFError):
        logging.warning("已停止；已完成的进度保留，未确认发送会在下次运行前要求核对。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
