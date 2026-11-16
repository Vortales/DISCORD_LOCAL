#!/usr/bin/env python3
"""Collect visible Discord forum posts and their messages using the web UI.

This is an unofficial UI automation script, not a Discord API client. It opens
Discord in a persistent Chromium profile; you sign in yourself in the browser.
It does not extract tokens or inspect Discord's local cache. UI selectors can
change when Discord updates its website.

Install:
    python -m pip install playwright
    python -m playwright install chromium

Run:
    python discord_forum_collect.py

The configured server/forum IDs below are used by default; optional positional
IDs can override them. First run: sign in in the opened browser if needed. The
script waits for the forum to load and then continues automatically. Later runs
reuse .discord_browser_profile. Output: discord_forum_export/posts.jsonl and
downloaded attachments.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from playwright.async_api import async_playwright, Page, BrowserContext


DEFAULT_GUILD_ID = "1514607659758653560"
DEFAULT_FORUM_CHANNEL_ID = "1515423283606982776"


MESSAGE_SCRIPT = r"""() => {
  const nodes = [...document.querySelectorAll('[id^="chat-messages-"]')];
  return nodes.map(el => {
    const id = el.id || '';
    const authorEl = el.querySelector('[id^="message-username-"]')
      || el.querySelector('[class*="username"]');
    const timeEl = el.querySelector('time[datetime]');
    const attachments = [...el.querySelectorAll('a[href]')]
      .map(a => ({url: a.href, label: (a.innerText || a.getAttribute('aria-label') || '').trim()}))
      .filter(a => /(?:cdn\.discordapp\.com|media\.discordapp\.net)\/attachments\//i.test(a.url));
    const text = (el.innerText || '').trim();
    return {
      dom_id: id,
      message_id: id.split('-').pop(),
      author: authorEl ? (authorEl.innerText || '').trim() : null,
      timestamp: timeEl ? timeEl.dateTime : null,
      text,
      attachments
    };
  }).filter(m => m.dom_id && (m.text || m.attachments.length));
}"""

THREAD_LINKS_SCRIPT = r"""({guild, channel}) => {
  const prefix = `/channels/${guild}/${channel}/`;
  const found = new Map();
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href;
    if (!href.includes(prefix)) continue;
    const match = href.match(/\/channels\/(\d+)\/(\d+)\/(\d+)/);
    if (!match || match[1] !== guild || match[2] !== channel) continue;
    found.set(match[3], {
      id: match[3],
      url: `https://discord.com/channels/${guild}/${channel}/${match[3]}`,
      title: (a.innerText || a.getAttribute('aria-label') || '').trim()
    });
  }
  return [...found.values()];
}"""

SCROLLER_SCRIPT = r"""() => {
  const main = document.querySelector('main') || document.body;
  const candidates = [...main.querySelectorAll('*')]
    .filter(el => {
      const s = getComputedStyle(el);
      return /(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight + 80;
    });
  candidates.sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
  return candidates[0] || document.scrollingElement || document.documentElement;
}"""


def safe_name(value: str, fallback: str) -> str:
    value = unquote(value).strip()
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", value)
    value = value.strip(" .")
    return (value[:160] or fallback)


async def scroll_and_collect_forum(page: Page, guild_id: str, channel_id: str, max_scrolls: int) -> dict[str, dict]:
    posts: dict[str, dict] = {}
    no_growth = 0
    last_count = 0
    for _ in range(max_scrolls):
        for post in await page.evaluate(THREAD_LINKS_SCRIPT, {"guild": guild_id, "channel": channel_id}):
            posts[post["id"]] = post
        if len(posts) == last_count:
            no_growth += 1
        else:
            no_growth = 0
            last_count = len(posts)
        if no_growth >= 4:
            break
        await page.evaluate("""() => {
          const el = (%s)();
          el.scrollTop += Math.max(400, el.clientHeight * 0.8);
        }""" % SCROLLER_SCRIPT)
        await page.wait_for_timeout(850)

    # Some forum layouts load older cards at the top instead of the bottom.
    no_growth = 0
    last_count = len(posts)
    for _ in range(max_scrolls):
        for post in await page.evaluate(THREAD_LINKS_SCRIPT, {"guild": guild_id, "channel": channel_id}):
            posts[post["id"]] = post
        if len(posts) == last_count:
            no_growth += 1
        else:
            no_growth = 0
            last_count = len(posts)
        if no_growth >= 4:
            break
        await page.evaluate("""() => { const el = (%s)(); el.scrollTop = 0; }""" % SCROLLER_SCRIPT)
        await page.wait_for_timeout(850)
    return posts


async def collect_messages(page: Page, max_scrolls: int) -> list[dict]:
    gathered: dict[str, dict] = {}
    stable = 0
    previous = 0
    for _ in range(max_scrolls):
        for message in await page.evaluate(MESSAGE_SCRIPT):
            gathered[message["message_id"]] = message
        if len(gathered) == previous:
            stable += 1
        else:
            stable = 0
            previous = len(gathered)
        if stable >= 4:
            break
        await page.evaluate("""() => {
          const list = document.querySelector('[data-list-id="chat-messages"]');
          let el = list;
          while (el && el !== document.body) {
            const s = getComputedStyle(el);
            if (/(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight + 20) break;
            el = el.parentElement;
          }
          if (!el || el === document.body) el = document.scrollingElement || document.documentElement;
          el.scrollTop = 0;
        }""")
        await page.wait_for_timeout(900)
    return sorted(gathered.values(), key=lambda m: (m.get("timestamp") or "", m["message_id"]))


def attachment_filename(url: str, label: str, thread_id: str, message_id: str, index: int) -> str:
    query = parse_qs(urlparse(url).query)
    name = query.get("filename", [""])[0] or Path(urlparse(url).path).name or label
    name = safe_name(name, f"attachment-{index}")
    suffix = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return safe_name(f"{thread_id}_{message_id}_{index}_{name}_{suffix}", f"attachment-{suffix}")


async def download_attachments(context: BrowserContext, messages: list[dict], folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for message in messages:
        downloaded = []
        for index, attachment in enumerate(message.get("attachments", []), 1):
            url = attachment["url"]
            filename = attachment_filename(url, attachment.get("label", ""),
                                           message.get("thread_id", "thread"),
                                           message["message_id"], index)
            destination = folder / filename
            attachment["saved_as"] = str(destination)
            if not destination.exists():
                try:
                    response = await context.request.get(url, timeout=60_000)
                    if response.ok:
                        destination.write_bytes(await response.body())
                        downloaded.append(filename)
                    else:
                        attachment["download_error"] = f"HTTP {response.status}"
                        print(f"Warning: download failed ({response.status}): {url}", file=sys.stderr)
                except Exception as exc:
                    attachment["download_error"] = str(exc)
                    print(f"Warning: could not download {url}: {exc}", file=sys.stderr)
        message["attachments"] = message.get("attachments", [])


async def main_async(args: argparse.Namespace) -> None:
    guild_id, channel_id = str(args.guild_id), str(args.channel_id)
    channel_url = f"https://discord.com/channels/{guild_id}/{channel_id}"
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    profile = Path(args.profile)
    profile.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile.resolve()),
            headless=False,
            accept_downloads=True,
            viewport={"width": 1440, "height": 1000},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(channel_url, wait_until="domcontentloaded")
        print("Если потребуется, войдите в Discord в окне браузера. Ожидаю загрузку форум-канала…")
        await page.wait_for_function(
            "([guild, channel]) => location.pathname.startsWith(`/channels/${guild}/${channel}`)",
            arg=[guild_id, channel_id],
            timeout=300_000,
        )
        await page.wait_for_timeout(5000)

        posts = await scroll_and_collect_forum(page, guild_id, channel_id, args.max_scrolls)
        if not posts:
            print("Не нашёл ссылок на публикации. Проверьте ID сервера/канала и доступ к форуму.", file=sys.stderr)

        output_path = output_dir / "posts.jsonl"
        total_messages = 0
        with output_path.open("w", encoding="utf-8") as out:
            for index, post in enumerate(posts.values(), 1):
                print(f"[{index}/{len(posts)}] Собираю публикацию {post['id']}…")
                try:
                    await page.goto(post["url"], wait_until="domcontentloaded")
                    await page.wait_for_timeout(1800)
                    messages = await collect_messages(page, args.max_scrolls)
                    for message in messages:
                        message["thread_id"] = post["id"]
                    title = post.get("title") or await page.title()
                    record = {
                        "type": "post",
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                        "post_id": post["id"],
                        "title": title,
                        "url": post["url"],
                        "messages": messages,
                    }
                    if args.download_files:
                        await download_attachments(context, messages, output_dir / "attachments" / post["id"])
                    out.write(json.dumps(record, ensure_ascii=False) + "\n")
                    out.flush()
                    total_messages += len(messages)
                except Exception as exc:
                    print(f"Warning: publication {post['id']} skipped: {exc}", file=sys.stderr)

        print(f"Готово: {len(posts)} публикаций, {total_messages} сообщений. Результат: {output_path}")
        await context.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Собрать публикации и вложения из доступного Discord-форума через веб-интерфейс.")
    parser.add_argument("guild_id", nargs="?", default=DEFAULT_GUILD_ID, help=f"ID сервера (по умолчанию: {DEFAULT_GUILD_ID})")
    parser.add_argument("channel_id", nargs="?", default=DEFAULT_FORUM_CHANNEL_ID, help=f"ID форум-канала (по умолчанию: {DEFAULT_FORUM_CHANNEL_ID})")
    parser.add_argument("-o", "--output", default="discord_forum_export", help="Папка экспорта")
    parser.add_argument("--profile", default=".discord_browser_profile", help="Локальный профиль Chromium для сессии Discord")
    parser.add_argument("--max-scrolls", type=int, default=250, help="Лимит прокруток на форум/публикацию")
    parser.add_argument("--no-download-files", dest="download_files", action="store_false", help="Не скачивать вложения, только сохранить ссылки")
    parser.set_defaults(download_files=True)
    args = parser.parse_args()
    if args.max_scrolls < 1:
        parser.error("--max-scrolls must be at least 1")
    return args


def main() -> None:
    try:
        asyncio.run(main_async(parse_args()))
    except KeyboardInterrupt:
        print("Остановлено.", file=sys.stderr)
    except Exception as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
