#!/usr/bin/env python3
"""Read-only Discord server exporter using the official Discord bot API.

Install:  python -m pip install -U discord.py
Run:      DISCORD_BOT_TOKEN='...' python discord_export.py GUILD_ID -o export.jsonl

The bot must be a member of the server and have View Channels + Read Message
History. To export message text, enable Message Content Intent both here and
in the bot's Discord Developer Portal settings. This script does not read local
Discord cache files or use a user account token (self-bot).

Output is JSON Lines: one guild record, channel records, then message records.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import discord


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def channel_record(channel: Any) -> dict[str, Any]:
    category = getattr(channel, "category", None)
    parent = getattr(channel, "parent", None)
    return {
        "type": "channel",
        "id": str(channel.id),
        "name": channel.name,
        "channel_type": str(channel.type),
        "position": getattr(channel, "position", None),
        "category_id": str(category.id) if category else None,
        "category_name": category.name if category else None,
        "parent_id": str(parent.id) if parent else None,
        "topic": getattr(channel, "topic", None),
        "is_thread": isinstance(channel, discord.Thread),
        "created_at": iso(channel.created_at),
    }


def message_record(message: discord.Message, channel: Any) -> dict[str, Any]:
    return {
        "type": "message",
        "id": str(message.id),
        "channel_id": str(channel.id),
        "channel_name": channel.name,
        "author_id": str(message.author.id),
        "author_name": str(message.author),
        "content": message.content,
        "created_at": iso(message.created_at),
        "edited_at": iso(message.edited_at),
        "attachments": [
            {"id": str(a.id), "filename": a.filename, "url": a.url, "size": a.size}
            for a in message.attachments
        ],
        "embeds": [e.to_dict() for e in message.embeds],
        "reply_to_id": str(message.reference.message_id)
        if message.reference and message.reference.message_id else None,
        "pinned": message.pinned,
    }


async def channels_to_export(guild: discord.Guild, include_threads: bool) -> list[Any]:
    channels: list[Any] = sorted(guild.channels, key=lambda c: (getattr(c, "position", 0), c.id))
    if not include_threads:
        return channels

    seen = {c.id for c in channels}
    for thread in guild.threads:  # active threads, including forum posts
        if thread.id not in seen:
            channels.append(thread)
            seen.add(thread.id)

    parents = [c for c in guild.channels if isinstance(c, (discord.TextChannel, discord.ForumChannel))]
    for parent in parents:
        try:
            async for thread in parent.archived_threads(limit=None):
                if thread.id not in seen:
                    channels.append(thread)
                    seen.add(thread.id)
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"Warning: cannot list archived threads in #{parent.name}: {exc}", file=sys.stderr)
    return channels


async def run_export(args: argparse.Namespace) -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set the bot token in the DISCORD_BOT_TOKEN environment variable.")

    intents = discord.Intents.none()
    intents.guilds = True
    intents.messages = True
    intents.message_content = True
    client = discord.Client(intents=intents)
    result: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    output_path = Path(args.output)

    @client.event
    async def on_ready() -> None:
        try:
            guild = client.get_guild(args.guild_id)
            if guild is None:
                raise RuntimeError("Guild not found in the bot's server list. Check the ID and confirm the bot joined it.")

            output_path.parent.mkdir(parents=True, exist_ok=True)
            records = 0
            with output_path.open("w", encoding="utf-8") as out:
                def emit(record: dict[str, Any]) -> None:
                    nonlocal records
                    out.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                    records += 1

                emit({"type": "guild", "id": str(guild.id), "name": guild.name})
                for channel in await channels_to_export(guild, args.include_threads):
                    emit(channel_record(channel))
                    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                        continue
                    try:
                        limit = args.limit or None
                        async for message in channel.history(limit=limit, oldest_first=True):
                            emit(message_record(message, channel))
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        print(f"Warning: skipped #{channel.name} ({channel.id}): {exc}", file=sys.stderr)

            print(f"Exported {records} records to {output_path}")
            if not result.done():
                result.set_result(None)
        except Exception as exc:
            if not result.done():
                result.set_exception(exc)
        finally:
            await client.close()

    try:
        await client.start(token)
        await result
    finally:
        if not client.is_closed():
            await client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export accessible Discord channels and messages as JSON Lines.")
    parser.add_argument("guild_id", type=int, help="Discord server (guild) ID")
    parser.add_argument("-o", "--output", default="discord-export.jsonl", help="Output JSONL path")
    parser.add_argument("--limit", type=int, default=0, help="Maximum messages per channel (0 = all accessible history)")
    parser.add_argument("--include-threads", action="store_true", help="Include active and accessible public archived threads/forum posts")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be zero or greater")
    return args


def main() -> None:
    try:
        asyncio.run(run_export(parse_args()))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
