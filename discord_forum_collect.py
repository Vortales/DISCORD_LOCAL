#!/usr/bin/env python3
"""Collect visible Discord forum posts and their messages using the web UI.

This is an unofficial UI automation script, not a Discord API client. It opens
Discord in a persistent Chromium profile; you sign in yourself in the browser.
It does not extract tokens or inspect Discord's local cache. UI selectors can
change when Discord updates its website.

Run:
    python discord_forum_collect.py

On first run, the script installs Playwright and its Chromium browser if absent.
This requires internet access. The configured server/forum IDs below are used
by default; optional positional IDs can override them. Sign in in the opened
browser if needed. The script waits for the forum to load and continues
automatically. Later runs reuse .discord_browser_profile. Output:
discord_forum_export/posts.jsonl and downloaded attachments.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


def ensure_playwright_package() -> None:
    """Install Playwright into the current interpreter if it is missing."""
    if importlib.util.find_spec("playwright") is None:
        print("Playwright not found; installing it into this Python environment…")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "playwright"],
            check=True,
        )


ensure_playwright_package()
from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError as PlaywrightTimeoutError  # noqa: E402


DEFAULT_GUILD_ID = "1514607659758653560"
DEFAULT_FORUM_CHANNEL_ID = "1515423283606982776"


MESSAGE_SCRIPT = r"""() => {
  const selector = '[id^="chat-messages-"], [data-list-item-id*="chat-messages"], '
    + '[data-message-id], [class*="messageListItem"], li[class*="message"]';
  const nodes = [...new Set(document.querySelectorAll(selector))];
  return nodes.map(el => {
    const id = el.id || '';
    const sourceId = el.getAttribute('data-message-id') || el.getAttribute('data-list-item-id') || id;
    const numericIds = sourceId.match(/[0-9]{16,22}/g) || [];
    const messageId = numericIds.length ? numericIds[numericIds.length - 1] : id.split('-').pop();
    const authorEl = el.querySelector('[id^="message-username-"]')
      || el.querySelector('[class*="username"]');
    const authorNode = authorEl?.closest('[data-user-id]') || el.querySelector('[data-user-id]');
    const authorId = authorNode?.getAttribute('data-user-id') || null;
    const visibleUsers = [...el.querySelectorAll('[data-user-id]')].map(user => ({
      id: user.getAttribute('data-user-id'),
      name: (user.innerText || user.getAttribute('aria-label') || user.getAttribute('alt') || '').trim(),
      roles: [...user.querySelectorAll('[class*="role" i]')]
        .map(role => (role.innerText || role.getAttribute('aria-label') || '').trim()).filter(Boolean)
    })).filter(user => user.id && user.name);
    const timeEl = el.querySelector('time[datetime]');
    const attachments = [...el.querySelectorAll('a[href]')]
      .map(a => ({url: a.href, label: (a.innerText || a.getAttribute('aria-label') || '').trim()}))
      .filter(a => /(?:cdn\.discordapp\.com|media\.discordapp\.net)\/attachments\//i.test(a.url));
    const text = (el.innerText || '').trim();
    const controls = [...el.querySelectorAll('button, [role="button"]')].map(control => ({
      text: (control.innerText || '').trim(),
      aria_label: control.getAttribute('aria-label'),
      title: control.getAttribute('title')
    })).filter(item => item.text || item.aria_label || item.title);
    const poll_or_reaction_details = controls.filter(item =>
      /poll|vote|voted|votes|react|reaction|голос|опрос|реакц/i.test(
        `${item.text} ${item.aria_label || ''} ${item.title || ''}`
      )
    );
    const pollElements = [
      ...(el.matches('[data-poll], [class*="poll" i]') ? [el] : []),
      ...el.querySelectorAll('[data-poll], [class*="poll" i]')
    ];
    const polls = [...new Set(pollElements)].map(poll => ({
      text: (poll.innerText || '').trim(),
      data_poll: poll.getAttribute('data-poll'),
      aria_label: poll.getAttribute('aria-label')
    })).filter(poll => poll.text || poll.data_poll || poll.aria_label);
    return {
      dom_id: sourceId || id,
      message_id: messageId,
      author: authorEl ? (authorEl.innerText || '').trim() : null,
      author_id: authorId,
      visible_users: [...new Map(visibleUsers.map(user => [user.id, user])).values()],
      timestamp: timeEl ? timeEl.dateTime : null,
      edited_timestamp: el.querySelector('time[datetime][aria-label*="edit" i]')?.dateTime || null,
      text,
      attachments,
      poll_or_reaction_details,
      polls,
      visible_controls: controls
    };
  }).filter(m => m.dom_id && (m.text || m.attachments.length));
}"""

THREAD_PAGE_SNAPSHOT_SCRIPT = r"""({postTitle}) => {
  const root = document.querySelector(
    '[data-list-id*="chat-messages"], [data-list-id*="forum-post"], [data-list-id*="thread"], '
    + '[role="dialog"], [role="tabpanel"], [class*="threadSidebar"], [role="main"], main'
  ) || document.body;
  const controls = [...root.querySelectorAll('button, [role="button"]')].map(el => ({
    text: (el.innerText || '').trim(),
    aria_label: el.getAttribute('aria-label'),
    title: el.getAttribute('title'),
    data_list_item_id: el.getAttribute('data-list-item-id')
  })).filter(x => x.text || x.aria_label || x.title);
  const visible_users = [...root.querySelectorAll('[data-user-id]')].map(el => ({
    id: el.getAttribute('data-user-id'),
    name: (el.innerText || el.getAttribute('aria-label') || el.getAttribute('alt') || '').trim(),
    roles: [...el.querySelectorAll('[class*="role" i]')]
      .map(role => (role.innerText || role.getAttribute('aria-label') || '').trim()).filter(Boolean)
  })).filter(x => x.id && x.name);
  const polls = [...root.querySelectorAll('[data-poll], [class*="poll" i]')].map(el => ({
    text: (el.innerText || '').trim(),
    data_poll: el.getAttribute('data-poll'),
    aria_label: el.getAttribute('aria-label')
  })).filter(x => x.text || x.data_poll || x.aria_label);
  const elements = [...root.querySelectorAll('[data-list-item-id], [data-message-id], [id*="chat-messages"], [data-poll]')]
    .slice(0, 150).map(el => ({
      tag: el.tagName,
      id: el.id,
      role: el.getAttribute('role'),
      data_list_item_id: el.getAttribute('data-list-item-id'),
      data_message_id: el.getAttribute('data-message-id'),
      data_poll: el.getAttribute('data-poll'),
      class_name: typeof el.className === 'string' ? el.className.slice(0, 120) : '',
      text: (el.innerText || '').trim().slice(0, 500)
    }));
  return {
    url: location.href.split('?')[0],
    title: document.title,
    root: {tag: root.tagName, id: root.id, role: root.getAttribute('role')},
    text: (() => {
      const fullText = (root.innerText || '').trim();
      const titleIndex = postTitle ? fullText.indexOf(postTitle) : -1;
      return (titleIndex >= 0 ? fullText.slice(titleIndex) : fullText.slice(-16000)).slice(0, 16000);
    })(),
    timestamps: [...root.querySelectorAll('time[datetime]')].map(el => ({datetime: el.dateTime, label: el.getAttribute('aria-label')})),
    visible_users: [...new Map(visible_users.map(user => [user.id, user])).values()],
    controls,
    polls,
    elements
  };
}"""

THREAD_LINKS_SCRIPT = r"""({guild, channel}) => {
  const prefix = `/channels/${guild}/${channel}/`;
  const found = new Map();
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href;
    if (!href.includes(prefix)) continue;
    const match = href.match(/\/channels\/(\d+)\/(\d+)\/(\d+)/);
    if (!match || match[1] !== guild || match[2] !== channel) continue;
    const card = a.closest('article') || a.parentElement?.parentElement || a.parentElement;
    const timeEl = card?.querySelector('time[datetime]');
    const authorEl = card?.querySelector('[id^="message-username-"], [class*="username"]');
    const authorNode = authorEl?.closest('[data-user-id]') || card?.querySelector('[data-user-id]');
    const tags = [...(card?.querySelectorAll('[class*="tag"]') || [])]
      .map(tag => (tag.innerText || '').trim()).filter(Boolean);
    found.set(match[3], {
      id: match[3],
      url: `https://discord.com/channels/${guild}/${channel}/${match[3]}`,
      title: (a.innerText || a.getAttribute('aria-label') || '').trim(),
      author: authorEl ? (authorEl.innerText || '').trim() : null,
      author_id: authorNode?.getAttribute('data-user-id') || null,
      created_at: timeEl ? timeEl.dateTime : null,
      preview_text: (card?.innerText || '').trim().slice(0, 4000),
      tags: [...new Set(tags)]
    });
  }
  // Discord sometimes renders forum cards as clickable divs instead of anchors.
  const cards = document.querySelectorAll(
    '[data-post-id], [data-list-item-id*="forum" i], [id*="forum-post" i], article, [role="article"]'
  );
  for (const card of cards) {
    const attrs = [...card.attributes].map(a => `${a.name}=${a.value}`).join(' ');
    const listItemId = card.getAttribute('data-list-item-id') || '';
    const directId = card.getAttribute('data-post-id');
    const marker = `forum-channel-list-${channel}___`;
    let postId = listItemId.startsWith(marker) ? listItemId.slice(marker.length) : null;
    if (!postId) {
      const routePrefix = `/channels/${guild}/${channel}/`;
      const routeIndex = attrs.indexOf(routePrefix);
      const routeTail = routeIndex >= 0 ? attrs.slice(routeIndex + routePrefix.length) : '';
      const routeId = routeTail.match(/^[0-9]{16,22}/);
      postId = routeId ? routeId[0] : null;
    }
    if (!postId && directId && /^[0-9]{16,22}$/.test(directId)) postId = directId;
    if (!postId) {
      const explicitPost = `${card.id} ${listItemId}`.match(/(?:forum[-_]?post|post)[^0-9]{0,12}([0-9]{16,22})/i);
      if (explicitPost) postId = explicitPost[1];
    }
    if (!postId || !/^[0-9]{16,22}$/.test(postId) || found.has(postId)) continue;

    // The forum list item itself is a zero-text focus target; climb to its card.
    let container = card;
    for (let parent = card.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
      if (parent.querySelectorAll(`[data-list-item-id^="${marker}"]`).length > 1) break;
      const text = (parent.innerText || '').trim();
      if (text.length > 4000) break;
      if (text.length > 0) container = parent;
      if (parent.querySelector('time[datetime]') && text.length > 20) {
        container = parent;
        break;
      }
    }
    const timeEl = container.querySelector('time[datetime]');
    const authorEl = container.querySelector('[id^="message-username-"], [class*="username"]');
    const authorNode = authorEl?.closest('[data-user-id]') || container.querySelector('[data-user-id]');
    found.set(postId, {
      id: postId,
      url: `https://discord.com/channels/${guild}/${channel}/${postId}`,
      title: (container.querySelector('h1, h2, h3, [role="heading"]')?.innerText || '').trim(),
      author: authorEl ? (authorEl.innerText || '').trim() : null,
      author_id: authorNode?.getAttribute('data-user-id') || null,
      created_at: timeEl ? timeEl.dateTime : null,
      preview_text: (container.innerText || '').trim().slice(0, 4000),
      tags: [...new Set([...container.querySelectorAll('[class*="tag"]')].map(tag => (tag.innerText || '').trim()).filter(Boolean))]
    });
  }
  return [...found.values()];
}"""

SCROLLER_SCRIPT = r"""channel => {
  const marker = `forum-channel-list-${channel}___`;
  const items = [...document.querySelectorAll(`[data-list-item-id^="${marker}"]`)];
  const item = items.find(el => /^[0-9]{16,22}$/.test(el.getAttribute('data-list-item-id').slice(marker.length))) || items[0];
  for (let el = item; el && el !== document.body; el = el.parentElement) {
    const style = getComputedStyle(el);
    if (/(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 20) return el;
  }
  const root = document.querySelector('main, [role="main"], #app-mount') || document.body;
  const candidates = [...root.querySelectorAll('*')].filter(el => {
    const style = getComputedStyle(el);
    return /(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 80;
  });
  candidates.sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
  return candidates[0] || document.scrollingElement || document.documentElement;
}"""

MESSAGE_SCROLLER_SCRIPT = r"""() => {
  const marker = document.querySelector(
    '[data-list-id="chat-messages"], [data-list-id*="chat-messages"], '
    + '[id^="chat-messages-"], [data-list-item-id*="chat-messages"]'
  );
  for (let el = marker; el && el !== document.body; el = el.parentElement) {
    const style = getComputedStyle(el);
    if (/(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 20) return el;
  }
  const root = document.querySelector('main, [role="main"], #app-mount') || document.body;
  const candidates = [...root.querySelectorAll('*')].filter(el => {
    const style = getComputedStyle(el);
    return /(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 80;
  });
  candidates.sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
  return candidates[0] || document.scrollingElement || document.documentElement;
}"""


def safe_name(value: str, fallback: str) -> str:
    value = unquote(value).strip()
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", value)
    value = value.strip(" .")
    return (value[:160] or fallback)


async def save_debug_bundle(
    page: Page,
    output_dir: Path,
    reason: str,
    guild_id: str,
    channel_id: str,
    browser_errors: list[str],
) -> Path:
    """Save a single ZIP with a screenshot and non-secret page diagnostics."""
    screenshot: bytes | None = None
    screenshot_error = None
    try:
        screenshot = await page.screenshot(full_page=False, timeout=15_000)
    except Exception as exc:
        screenshot_error = str(exc)

    try:
        page_data = await page.evaluate("""() => {
          const candidates = [...document.querySelectorAll(
            'article, [role="article"], [data-post-id], [data-list-item-id], [id*="forum" i]'
          )].slice(0, 80).map(el => ({
            tag: el.tagName,
            id: el.id,
            role: el.getAttribute('role'),
            class_name: typeof el.className === 'string' ? el.className.slice(0, 180) : '',
            data_post_id: el.getAttribute('data-post-id'),
            data_list_item_id: el.getAttribute('data-list-item-id'),
            text_preview: (el.innerText || '').trim().slice(0, 140)
          }));
          const links = [...document.querySelectorAll('a[href]')]
            .filter(a => a.href.includes('/channels/')).slice(0, 80)
            .map(a => ({href: a.href.split('?')[0], text: (a.innerText || '').trim().slice(0, 100)}));
          return {
            url: location.href.split('?')[0],
            title: document.title,
            ready_state: document.readyState,
            body_text_preview: (document.body?.innerText || '').slice(0, 2500),
            candidate_count: document.querySelectorAll(
              'article, [role="article"], [data-post-id], [data-list-item-id], [id*="forum" i]'
            ).length,
            candidates,
            channel_links: links
          };
        }""")
    except Exception as exc:
        page_data = {"page_inspection_error": str(exc)}

    report = {
        "reason": reason,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "guild_id": guild_id,
        "channel_id": channel_id,
        "browser_errors": browser_errors[-80:],
        "screenshot_error": screenshot_error,
        "page": page_data,
        "privacy_note": "Contains a screenshot and short visible text snippets. It does not include cookies, tokens, local storage, or password values.",
    }
    bundle_path = output_dir / "discord_forum_debug.zip"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("diagnostics.json", json.dumps(report, ensure_ascii=False, indent=2))
        if screenshot is not None:
            archive.writestr("page.png", screenshot)
    return bundle_path


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
          const el = (%s)(%s);
          el.scrollTop += Math.max(400, el.clientHeight * 0.8);
        }""" % (SCROLLER_SCRIPT, json.dumps(channel_id)))
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
        await page.evaluate("""() => { const el = (%s)(%s); el.scrollTop = 0; }""" % (SCROLLER_SCRIPT, json.dumps(channel_id)))
        await page.wait_for_timeout(850)
    return posts


async def open_forum_post(
    page: Page,
    guild_id: str,
    channel_id: str,
    post_id: str,
    max_scrolls: int,
    output_dir: Path,
    browser_errors: list[str],
) -> dict[str, Any]:
    """Find a forum card in the list and click it; direct URL navigation alone only loads the list in some Discord builds."""
    channel_url = f"https://discord.com/channels/{guild_id}/{channel_id}"
    await page.goto(channel_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(1800)
    list_item_id = f"forum-channel-list-{channel_id}___{post_id}"
    locator = page.locator(f'[data-list-item-id="{list_item_id}"]')

    for _ in range(max_scrolls):
        if await locator.count():
            break
        await page.evaluate(
            "() => { const el = (%s)(%s); el.scrollTop += Math.max(400, el.clientHeight * 0.85); }"
            % (SCROLLER_SCRIPT, json.dumps(channel_id))
        )
        await page.wait_for_timeout(750)
    if not await locator.count():
        bundle = await save_debug_bundle(
            page, output_dir, f"Target forum post {post_id} not found in the scrolled list", guild_id, channel_id, browser_errors
        )
        raise RuntimeError(f"Не нашёл публикацию {post_id} в списке после прокрутки. Архив диагностики: {bundle}")

    metadata = next(
        (item for item in await page.evaluate(THREAD_LINKS_SCRIPT, {"guild": guild_id, "channel": channel_id})
         if item["id"] == post_id),
        {},
    )
    await locator.scroll_into_view_if_needed()
    await locator.click()
    try:
        await page.wait_for_function(
            "postId => location.pathname.endsWith('/' + postId)", arg=post_id, timeout=20_000
        )
    except PlaywrightTimeoutError:
        # Some client builds open the post pane without changing the route.
        await page.wait_for_timeout(2500)
    await page.wait_for_timeout(1800)
    return metadata


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
        if stable >= (7 if gathered else 4):
            break
        await page.evaluate("() => { const el = (%s)(); el.scrollTop = 0; }" % MESSAGE_SCROLLER_SCRIPT)
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
        if not Path(p.chromium.executable_path).is_file():
            print("Playwright Chromium not found; downloading it now (this may take a while)…")
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, "-m", "playwright", "install", "chromium"],
                    check=True,
                )
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(
                    "Could not install Chromium. Check internet access and rerun the script."
                ) from exc
            if not Path(p.chromium.executable_path).is_file():
                raise RuntimeError("Chromium installation finished, but its executable is still missing.")

        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile.resolve()),
            headless=False,
            accept_downloads=True,
            viewport={"width": 1440, "height": 1000},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        browser_errors: list[str] = []

        def record_browser_error(text: str) -> None:
            browser_errors.append(text[:1000])
            del browser_errors[:-80]

        page.on("console", lambda msg: record_browser_error(f"console: {msg.text}") if msg.type == "error" else None)
        page.on("pageerror", lambda exc: record_browser_error(f"page error: {exc}"))
        page.on(
            "requestfailed",
            lambda req: record_browser_error(f"request failed: {req.url.split('?')[0]} ({req.failure})"),
        )
        await page.goto(channel_url, wait_until="domcontentloaded")
        print(
            "Если Discord попросит войти, сделайте это в открытом окне. "
            f"Скрипт ждёт появления публикаций (до {args.wait_timeout} сек.)…"
        )
        try:
            await page.wait_for_function(
                "([guild, channel]) => location.pathname.startsWith(`/channels/${guild}/${channel}`) "
                "&& !document.querySelector('input[type=\\\"password\\\"]') "
                "&& !!document.querySelector('main, [role=\\\"main\\\"], #app-mount') "
                "&& (document.body.innerText || '').trim().length > 80",
                arg=[guild_id, channel_id],
                timeout=args.wait_timeout * 1000,
            )
        except PlaywrightTimeoutError as exc:
            bundle = await save_debug_bundle(
                page, output_dir, "Timed out waiting for the Discord channel UI", guild_id, channel_id, browser_errors
            )
            raise RuntimeError(
                "Страница канала не загрузилась вовремя. Диагностика собрана в один файл: "
                f"{bundle}. В архиве есть снимок страницы и техническая информация."
            ) from exc
        await page.wait_for_timeout(2500)

        if args.post_id:
            posts = {
                args.post_id: {
                    "id": args.post_id,
                    "url": f"https://discord.com/channels/{guild_id}/{channel_id}/{args.post_id}",
                    "title": "",
                    "author": None,
                    "created_at": None,
                    "preview_text": None,
                    "tags": [],
                }
            }
        else:
            posts = await scroll_and_collect_forum(page, guild_id, channel_id, args.max_scrolls)
        if not posts:
            bundle = await save_debug_bundle(
                page, output_dir, "Channel loaded but no forum cards were detected", guild_id, channel_id, browser_errors
            )
            raise RuntimeError(
                "Страница загрузилась, но карточки публикаций не распознаны. "
                f"Один архив диагностики: {bundle}. В нём есть скриншот, структура страницы и ошибки браузера; "
                "перед отправкой проверьте видимые фрагменты сообщений и имена."
            )

        output_path = output_dir / (f"post_{args.post_id}_test.jsonl" if args.post_id else "posts.jsonl")
        total_messages = 0
        with output_path.open("w", encoding="utf-8") as out:
            for index, post in enumerate(posts.values(), 1):
                print(f"[{index}/{len(posts)}] Собираю публикацию {post['id']}…")
                try:
                    if args.post_id:
                        post.update(await open_forum_post(
                            page, guild_id, channel_id, args.post_id, args.max_scrolls, output_dir, browser_errors
                        ))
                    else:
                        await page.goto(post["url"], wait_until="domcontentloaded")
                        await page.wait_for_timeout(3000)
                    messages = await collect_messages(page, args.max_scrolls)
                    for message in messages:
                        message["thread_id"] = post["id"]
                    thread_snapshot = None
                    if not messages:
                        thread_snapshot = await page.evaluate(
                            THREAD_PAGE_SNAPSHOT_SCRIPT, {"postTitle": post.get("title", "")}
                        )
                    title = post.get("title") or await page.title()
                    record = {
                        "type": "post",
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                        "post_id": post["id"],
                        "title": title,
                        "author": post.get("author"),
                        "author_id": post.get("author_id"),
                        "published_at": post.get("created_at"),
                        "preview_text": post.get("preview_text"),
                        "tags": post.get("tags", []),
                        "url": post["url"],
                        "messages": messages,
                        "thread_snapshot": thread_snapshot,
                    }
                    if args.download_files:
                        await download_attachments(context, messages, output_dir / "attachments" / post["id"])
                    out.write(json.dumps(record, ensure_ascii=False) + "\n")
                    out.flush()
                    total_messages += len(messages)
                except Exception as exc:
                    print(f"Warning: publication {post['id']} skipped: {exc}", file=sys.stderr)

        if total_messages == 0:
            bundle = await save_debug_bundle(
                page, output_dir, "No message roots detected inside any forum post", guild_id, channel_id, browser_errors
            )
            print(
                "Warning: карточки собраны, но message DOM-селекторы не нашли сообщений. "
                f"В posts.jsonl добавлен видимый снимок каждой публикации; диагностика: {bundle}"
            )
        print(f"Готово: {len(posts)} публикаций, {total_messages} сообщений. Результат: {output_path}")
        await context.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Собрать публикации и вложения из доступного Discord-форума через веб-интерфейс.")
    parser.add_argument("guild_id", nargs="?", default=DEFAULT_GUILD_ID, help=f"ID сервера (по умолчанию: {DEFAULT_GUILD_ID})")
    parser.add_argument("channel_id", nargs="?", default=DEFAULT_FORUM_CHANNEL_ID, help=f"ID форум-канала (по умолчанию: {DEFAULT_FORUM_CHANNEL_ID})")
    parser.add_argument("-o", "--output", default="discord_forum_export", help="Папка экспорта")
    parser.add_argument("--profile", default=".discord_browser_profile", help="Локальный профиль Chromium для сессии Discord")
    parser.add_argument("--max-scrolls", type=int, default=250, help="Лимит прокруток на форум/публикацию")
    parser.add_argument("--post-id", default="", help="Тестовый режим: собрать только одну публикацию по ID")
    parser.add_argument("--wait-timeout", type=int, default=600, help="Сколько секунд ждать авторизацию и появления публикаций")
    parser.add_argument("--no-download-files", dest="download_files", action="store_false", help="Не скачивать вложения, только сохранить ссылки")
    parser.set_defaults(download_files=True)
    args = parser.parse_args()
    if args.max_scrolls < 1:
        parser.error("--max-scrolls must be at least 1")
    if args.wait_timeout < 1:
        parser.error("--wait-timeout must be at least 1")
    if args.post_id and not re.fullmatch(r"[0-9]{16,22}", args.post_id):
        parser.error("--post-id must be a Discord snowflake (16–22 digits)")
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
