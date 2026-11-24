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
from typing import Any, Awaitable, Callable
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

VOTER_VIEW_SCRIPT = r"""() => {
  const overlays = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"], [role="tooltip"]')];
  const root = overlays[overlays.length - 1] || document.body;
  const users = [...root.querySelectorAll('[data-user-id], [class*="username" i]')].map(el => ({
    id: el.getAttribute('data-user-id'),
    name: (el.innerText || el.getAttribute('aria-label') || el.getAttribute('alt') || '').trim(),
    roles: [...el.querySelectorAll('[class*="role" i]')]
      .map(role => (role.innerText || role.getAttribute('aria-label') || '').trim()).filter(Boolean)
  })).filter(x => x.name);
  const roles = [...root.querySelectorAll('[class*="role" i]')]
    .map(el => (el.innerText || el.getAttribute('aria-label') || '').trim()).filter(Boolean);
  return {
    url: location.href,
    title: document.title,
    has_overlay: overlays.length > 0,
    overlay_text: (root.innerText || '').trim().slice(-12000),
    page_text: (document.body?.innerText || '').trim().slice(-12000),
    users: [...new Map(users.map(user => [`${user.id || ''}:${user.name}`, user])).values()],
    roles: [...new Set(roles)]
  };
}"""

POLL_TARGETS_SCRIPT = r"""(root, arg) => {
  const controls = [...root.querySelectorAll('a[href], button, [role="button"]')];
  const normalize = value => (value || '').replace(/\s+/g, ' ').trim();
  const countPattern = /^\d+\s+(?:голос(?:а|ов)?|votes?)(?:\s*[,·•-]?\s*\d+(?:[.,]\d+)?\s*%)?$/i;
  const matches = [];
  const visibleControls = [];
  for (const el of controls) {
    const label = normalize(el.innerText) || normalize(el.getAttribute('aria-label')) || normalize(el.title);
    const visible = !!(el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
    const kind = /просмотреть опрос|view poll/i.test(label) ? 'view_poll'
      : countPattern.test(label) ? 'vote_count' : null;
    visibleControls.push({
      tag: el.tagName.toLowerCase(), role: el.getAttribute('role'), label: label.slice(0, 240),
      href: el.href || null, target: el.getAttribute('target'), visible, disabled: !!el.disabled
    });
    if (!kind || !visible) continue;
    const index = matches.length;
    const key = `scraper-poll-${arg.key}-${index}`;
    el.setAttribute('data-scraper-poll-target', key);
    matches.push({key, index, kind, label, tag: el.tagName.toLowerCase(), href: el.href || null, target: el.getAttribute('target')});
  }
  return {targets: matches, controls: visibleControls};
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
    filename: str = "discord_forum_debug.zip",
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
    bundle_path = output_dir / filename
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
    post_url = f"{channel_url}/{post_id}"
    list_item_id = f"forum-channel-list-{channel_id}___{post_id}"
    locator = page.locator(f'[data-list-item-id="{list_item_id}"]')
    # Discord often loads the right list position for this route, but still requires a UI click to open the post.
    await page.goto(post_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)
    if not await locator.count():
        await page.goto(channel_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1200)

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
            page,
            output_dir,
            f"Target forum post {post_id} not found in the scrolled list",
            guild_id,
            channel_id,
            browser_errors,
            filename=f"discord_forum_debug_{post_id}.zip",
        )
        raise RuntimeError(f"Не нашёл публикацию {post_id} в списке после прокрутки. Архив диагностики: {bundle}")

    metadata = next(
        (item for item in await page.evaluate(THREAD_LINKS_SCRIPT, {"guild": guild_id, "channel": channel_id})
         if item["id"] == post_id),
        {},
    )
    await locator.scroll_into_view_if_needed()
    before_state = await page.evaluate("""() => {
      const text = document.body?.innerText || '';
      return {
        path: location.pathname,
        message_nodes: document.querySelectorAll('[id^="chat-messages-"], [data-message-id], [data-list-item-id*="chat-messages"]').length,
        dialogs: document.querySelectorAll('[role="dialog"], [aria-modal="true"]').length,
        text_length: text.length,
        text_tail: text.slice(-1200)
      };
    }""")

    async def detail_is_open() -> bool:
        return await page.evaluate("""before => {
          const text = document.body?.innerText || '';
          const postId = %s;
          const pathChangedToPost = !before.path.endsWith('/' + postId) && location.pathname.endsWith('/' + postId);
          const messageNodes = document.querySelectorAll(
            '[id^="chat-messages-"], [data-message-id], [data-list-item-id*="chat-messages"]'
          ).length;
          const dialogs = document.querySelectorAll('[role="dialog"], [aria-modal="true"]').length;
          return pathChangedToPost || messageNodes > before.message_nodes || dialogs > before.dialogs
            || Math.abs(text.length - before.text_length) > 100 || text.slice(-1200) !== before.text_tail;
        }""" % json.dumps(post_id), before_state)

    # The data-list-item is an empty focus target; click the nearest ancestor containing the card text.
    await locator.evaluate("""el => {
      let target = el;
      for (let i = 0; i < 6 && target; i++, target = target.parentElement) {
        if ((target.innerText || '').trim().length > 10) {
          target.click();
          return;
        }
      }
      el.click();
    }""")
    await page.wait_for_timeout(900)
    if not await detail_is_open():
        try:
            await locator.focus()
            await locator.press("Enter", timeout=3000)
        except Exception:
            pass
    try:
        await page.wait_for_function(
            """before => {
              const text = document.body?.innerText || '';
              const postId = %s;
              return (!before.path.endsWith('/' + postId) && location.pathname.endsWith('/' + postId))
                || document.querySelectorAll('[id^=\\\"chat-messages-\\\"], [data-message-id], [data-list-item-id*=\\\"chat-messages\\\"]').length > before.message_nodes
                || document.querySelectorAll('[role=\\\"dialog\\\"], [aria-modal=\\\"true\\\"]').length > before.dialogs
                || Math.abs(text.length - before.text_length) > 100 || text.slice(-1200) !== before.text_tail;
            }""" % json.dumps(post_id),
            arg=before_state,
            timeout=12_000,
        )
    except PlaywrightTimeoutError as exc:
        bundle = await save_debug_bundle(
            page,
            output_dir,
            f"Click did not open forum post {post_id}",
            guild_id,
            channel_id,
            browser_errors,
            filename=f"discord_forum_debug_{post_id}.zip",
        )
        raise RuntimeError(
            f"Карточка {post_id} найдена, но Discord не открыл публикацию после клика. "
            f"Архив диагностики: {bundle}"
        ) from exc
    await page.wait_for_timeout(2200)
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


async def is_target_post_open(page: Page, post_id: str, messages: list[dict[str, Any]]) -> bool:
    """Accept either the post URL or an inline thread view that still contains one of its message nodes."""
    if urlparse(page.url).path.rstrip("/").endswith(f"/{post_id}"):
        return True
    for message in messages:
        dom_id = message.get("dom_id")
        if not dom_id:
            continue
        selector = f'[data-list-item-id="{dom_id}"], [data-message-id="{dom_id}"], [id="{dom_id}"]'
        if await page.locator(selector).count():
            return True
    return False


async def inspect_poll_voters(
    page: Page,
    context: BrowserContext,
    messages: list[dict[str, Any]],
    max_scrolls: int,
    post_id: str,
    restore_post: Callable[[], Awaitable[None]],
) -> list[dict[str, Any]]:
    """Inspect vote-count links and view controls without selecting poll answers; return detailed diagnostics."""
    diagnostics: list[dict[str, Any]] = []
    sequence = 0
    for message in messages:
        text = message.get("text", "")
        poll_text = "\n".join([text, *(p.get("text", "") for p in message.get("polls", []))])
        if not message.get("polls") and not re.search(r"опрос|poll|голос|vote", poll_text, re.IGNORECASE):
            continue
        sequence += 1
        dom_id = message.get("dom_id")
        event: dict[str, Any] = {
            "message_id": message.get("message_id"),
            "dom_id": dom_id,
            "message_text_preview": text[:1200],
            "status": "started",
            "targets": [],
        }
        if not await is_target_post_open(page, post_id, messages):
            event.update(
                status="left_target_post_before_inspection",
                error=f"The browser left post {post_id}; voter scanning stopped rather than scrolling another route.",
                current_url=page.url,
            )
            diagnostics.append(event)
            message["poll_voter_details"] = event
            print(f"[poll-voters] stopping: current route is not target post {post_id}: {page.url}")
            try:
                await restore_post()
            except Exception as exc:
                event["restore_error"] = str(exc)
                print(f"[poll-voters] could not restore target post {post_id}: {exc}")
                break
            continue
        if not dom_id:
            event.update(status="missing_message_dom_id", error="The message extractor returned no DOM identifier.")
            diagnostics.append(event)
            message["poll_voter_details"] = event
            print(f"[poll-voters] message {message.get('message_id')}: no DOM identifier")
            continue

        # MESSAGE_SCRIPT stores data-list-item-id as dom_id on Discord; it is not necessarily an element id.
        selector = f'[data-list-item-id="{dom_id}"], [data-message-id="{dom_id}"], [id="{dom_id}"]'
        root = page.locator(selector).first
        try:
            root_count = await page.locator(selector).count()
            scroll_steps = 0
            while not root_count and scroll_steps < max(1, min(max_scrolls, 60)):
                if not await is_target_post_open(page, post_id, messages):
                    event.update(
                        status="left_target_post_during_lookup",
                        error="The browser left the target post during DOM lookup; scrolling was stopped.",
                        current_url=page.url,
                    )
                    break
                await page.evaluate(
                    """() => {
                      const scroller = (%s)();
                      scroller.scrollTop += Math.max(280, Math.floor(scroller.clientHeight * 0.72));
                    }""" % MESSAGE_SCROLLER_SCRIPT
                )
                scroll_steps += 1
                await page.wait_for_timeout(260)
                root_count = await page.locator(selector).count()
            event["root_lookup"] = {
                "selector": selector,
                "matches": root_count,
                "scroll_steps": scroll_steps,
                "page_url": page.url,
            }
            if not root_count:
                if event.get("status") == "started":
                    event.update(
                        status="message_root_not_found",
                        error="Message was in the collected snapshot but its live DOM node was not found after scrolling.",
                    )
                event["visible_message_root_count"] = await page.locator(
                    '[data-list-item-id*="chat-messages"], [data-message-id], [id^="chat-messages-"]'
                ).count()
                diagnostics.append(event)
                message["poll_voter_details"] = event
                print(f"[poll-voters] message {message.get('message_id')}: DOM node not found after {scroll_steps} scrolls")
                continue

            await root.scroll_into_view_if_needed(timeout=3500)
            extracted = await root.evaluate(POLL_TARGETS_SCRIPT, {"key": str(sequence)})
            targets = extracted.get("targets", [])
            event["visible_controls"] = extracted.get("controls", [])
            event["target_count"] = len(targets)
            if not targets:
                event.update(
                    status="no_count_or_view_controls",
                    error="No visible link/button had an exact vote-count label or a View Poll label.",
                )
                diagnostics.append(event)
                message["poll_voter_details"] = event
                print(f"[poll-voters] message {message.get('message_id')}: no matching count/view controls")
                continue

            print(
                f"[poll-voters] message {message.get('message_id')}: found {len(targets)} safe control(s) "
                f"after {scroll_steps} scrolls"
            )
            for target in targets:
                item: dict[str, Any] = {
                    "label": target.get("label"),
                    "kind": target.get("kind"),
                    "tag": target.get("tag"),
                    "href": target.get("href"),
                    "link_target": target.get("target"),
                    "clicked": False,
                }
                event["targets"].append(item)
                try:
                    # Re-tag after each click because a vote-count link may navigate or open a separate page.
                    refreshed = await root.evaluate(POLL_TARGETS_SCRIPT, {"key": f"{sequence}-refresh"})
                    current = next(
                        (candidate for candidate in refreshed.get("targets", [])
                         if candidate.get("index") == target.get("index")
                         and candidate.get("kind") == target.get("kind")
                         and candidate.get("label") == target.get("label")),
                        None,
                    )
                    item["controls_before_click"] = refreshed.get("controls", [])
                    if current is None:
                        item.update(status="control_missing_on_recheck", error="Control disappeared before it could be clicked.")
                        continue
                    control = page.locator(f'[data-scraper-poll-target="{current["key"]}"]')
                    await control.scroll_into_view_if_needed(timeout=3000)
                    before_url = page.url
                    before_pages = list(context.pages)
                    baseline = await page.evaluate(VOTER_VIEW_SCRIPT)
                    item["url_before"] = before_url
                    item["baseline_overlay"] = baseline.get("has_overlay")
                    item["baseline_user_count"] = len(baseline.get("users", []))
                    print(
                        f"[poll-voters] clicking {target.get('kind')} {target.get('label')!r} "
                        f"tag={target.get('tag')} href={target.get('href')!r}"
                    )
                    await control.click(timeout=4000)
                    item["clicked"] = True
                    if target.get("target") == "_blank":
                        await page.wait_for_timeout(500)
                    else:
                        try:
                            await page.wait_for_function(
                                """before => {
                                  const body = (document.body?.innerText || '').trim().slice(-12000);
                                  const overlays = document.querySelectorAll('[role="dialog"], [aria-modal="true"], [role="tooltip"]').length;
                                  return location.href !== before.url || body !== before.text
                                    || overlays > before.overlay_count;
                                }""",
                                arg={
                                    "url": before_url,
                                    "text": baseline.get("page_text", ""),
                                    "overlay_count": int(bool(baseline.get("has_overlay"))),
                                },
                                timeout=3000,
                            )
                            item["same_page_change_detected"] = True
                        except Exception as exc:
                            item["same_page_change_detected"] = False
                            item["same_page_wait_error"] = str(exc)
                    opened_pages = [candidate for candidate in context.pages if candidate not in before_pages]
                    result_pages: list[dict[str, Any]] = []
                    for result_page in ([page] if not page.is_closed() else []) + opened_pages:
                        try:
                            if result_page is not page:
                                try:
                                    await result_page.wait_for_load_state("domcontentloaded", timeout=3500)
                                except Exception:
                                    pass
                                await result_page.wait_for_timeout(350)
                            result = await result_page.evaluate(VOTER_VIEW_SCRIPT)
                            baseline_keys = {u.get("id") or u.get("name") for u in baseline.get("users", [])}
                            result["new_users_vs_baseline"] = [
                                u for u in result.get("users", [])
                                if (u.get("id") or u.get("name")) not in baseline_keys
                            ]
                            result["source_page"] = "same_page" if result_page is page else "opened_page"
                            result_pages.append(result)
                        except Exception as exc:
                            result_pages.append({"source_page": "capture_error", "url": getattr(result_page, "url", None), "error": str(exc)})
                    item["url_after"] = page.url if not page.is_closed() else None
                    item["opened_page_count"] = len(opened_pages)
                    item["results"] = result_pages
                    item["status"] = "captured" if result_pages else "clicked_but_no_page_capture"
                    user_count = sum(len(result.get("users", [])) for result in result_pages)
                    print(
                        f"[poll-voters] result for {target.get('label')!r}: pages={len(result_pages)}, "
                        f"visible_user_rows={user_count}, url={item.get('url_after')!r}"
                    )

                    # Close popups after recording their visible content; restore same-tab navigation if it occurred.
                    for opened_page in opened_pages:
                        try:
                            await opened_page.close()
                        except Exception:
                            pass
                    if not page.is_closed() and not await is_target_post_open(page, post_id, messages):
                        try:
                            print(f"[poll-voters] restoring target post {post_id} from {page.url}")
                            await restore_post()
                            item["restored_target_post"] = await is_target_post_open(page, post_id, messages)
                            item["url_after_restore"] = page.url
                            if not item["restored_target_post"]:
                                event["status"] = "target_post_restore_failed"
                                break
                        except Exception as exc:
                            item["restore_error"] = str(exc)
                            event["status"] = "target_post_restore_failed"
                            print(f"[poll-voters] restore failed for {post_id}: {exc}")
                            break
                    elif not page.is_closed():
                        try:
                            await page.keyboard.press("Escape")
                        except Exception:
                            pass
                except Exception as exc:
                    item.update(status="inspection_error", error=str(exc))
                    print(f"[poll-voters] control {target.get('label')!r} failed: {exc}")
            if event.get("status") == "started":
                event["status"] = "controls_inspected"
            diagnostics.append(event)
            message["poll_voter_details"] = event
        except Exception as exc:
            event.update(status="inspection_error", error=str(exc))
            diagnostics.append(event)
            message["poll_voter_details"] = event
            print(f"[poll-voters] message {message.get('message_id')} failed: {exc}")
    return diagnostics


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


def attach_page_error_handlers(page: Page, browser_errors: list[str]) -> None:
    def record(text: str) -> None:
        browser_errors.append(text[:1000])
        del browser_errors[:-100]

    page.on("console", lambda msg: record(f"console: {msg.text}") if msg.type == "error" else None)
    page.on("pageerror", lambda exc: record(f"page error: {exc}"))
    page.on(
        "requestfailed",
        lambda req: record(f"request failed: {req.url.split('?')[0]} ({req.failure})"),
    )


async def scrape_one_post(
    context: BrowserContext,
    guild_id: str,
    channel_id: str,
    post: dict[str, Any],
    index: int,
    total: int,
    args: argparse.Namespace,
    output_dir: Path,
    browser_errors: list[str],
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    async with semaphore:
        reuse_existing_page = bool(args.post_id and context.pages)
        page = context.pages[0] if reuse_existing_page else await context.new_page()
        if not reuse_existing_page:
            attach_page_error_handlers(page, browser_errors)
        record: dict[str, Any] = {
            "type": "post",
            "guild_id": guild_id,
            "channel_id": channel_id,
            "post_id": post["id"],
            "title": post.get("title"),
            "author": post.get("author"),
            "author_id": post.get("author_id"),
            "published_at": post.get("created_at"),
            "preview_text": post.get("preview_text"),
            "tags": post.get("tags", []),
            "url": post.get("url"),
            "messages": [],
            "thread_snapshot": None,
            "poll_voter_diagnostics": [],
            "poll_voter_visibility_note": (
                "Only voter names/IDs/roles visibly rendered by Discord to this logged-in account can be captured. "
                "Discord-hidden voters or details are unavailable and are not inferred."
            ),
        }
        try:
            print(f"[{index}/{total}] Открываю публикацию {post['id']}…")
            metadata = await open_forum_post(
                page, guild_id, channel_id, post["id"], args.max_scrolls, output_dir, browser_errors
            )
            record.update({
                "title": metadata.get("title") or record.get("title"),
                "author": metadata.get("author") or record.get("author"),
                "author_id": metadata.get("author_id") or record.get("author_id"),
                "published_at": metadata.get("created_at") or record.get("published_at"),
                "preview_text": metadata.get("preview_text") or record.get("preview_text"),
                "tags": metadata.get("tags") or record.get("tags", []),
                "url": metadata.get("url") or record.get("url"),
            })
            messages = await collect_messages(page, args.max_scrolls)
            for message in messages:
                message["thread_id"] = post["id"]

            async def restore_target_post() -> None:
                await open_forum_post(
                    page, guild_id, channel_id, post["id"], args.max_scrolls, output_dir, browser_errors
                )

            if args.inspect_voters:
                poll_events = await inspect_poll_voters(
                    page, context, messages, args.max_scrolls, post["id"], restore_target_post
                )
                record["poll_voter_diagnostics"] = [
                    {
                        "message_id": event.get("message_id"),
                        "status": event.get("status"),
                        "root_lookup": event.get("root_lookup"),
                        "target_count": event.get("target_count", len(event.get("targets", []))),
                        "targets": [
                            {
                                "label": target.get("label"),
                                "kind": target.get("kind"),
                                "clicked": target.get("clicked"),
                                "status": target.get("status"),
                                "href": target.get("href"),
                                "url_after": target.get("url_after"),
                                "opened_page_count": target.get("opened_page_count"),
                                "visible_user_rows": sum(
                                    len(result.get("users", [])) for result in target.get("results", [])
                                ),
                            }
                            for target in event.get("targets", [])
                        ],
                        "error": event.get("error"),
                    }
                    for event in poll_events
                ]
            else:
                record["poll_voter_diagnostics"] = [{"status": "disabled_by_user", "message": "Voter inspection was disabled."}]
            if not await is_target_post_open(page, post["id"], messages):
                print(f"[poll-voters] final safety restore to post {post['id']} from {page.url}")
                try:
                    await restore_target_post()
                    record["poll_navigation_restored"] = True
                except Exception as exc:
                    record["poll_navigation_restore_error"] = str(exc)
                    print(f"Warning: could not restore post {post['id']} after poll inspection: {exc}", file=sys.stderr)
            record["title"] = record.get("title") or await page.title()
            record["messages"] = messages
            if not messages:
                record["thread_snapshot"] = await page.evaluate(
                    THREAD_PAGE_SNAPSHOT_SCRIPT, {"postTitle": record.get("title", "")}
                )
            if args.download_files:
                await download_attachments(context, messages, output_dir / "attachments" / post["id"])
            print(f"[{index}/{total}] Готово: {post['id']} ({len(messages)} сообщений)")
        except Exception as exc:
            browser_errors.append(f"post {post['id']}: {exc}")
            record["error"] = str(exc)
            try:
                record["thread_snapshot"] = await page.evaluate(
                    THREAD_PAGE_SNAPSHOT_SCRIPT, {"postTitle": record.get("title", "")}
                )
            except Exception:
                pass
            print(f"Warning: publication {post['id']} failed: {exc}", file=sys.stderr)
        finally:
            if not reuse_existing_page:
                try:
                    await page.close()
                except Exception:
                    pass
        return record


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
        attach_page_error_handlers(page, browser_errors)
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
        failed_posts = 0
        semaphore = asyncio.Semaphore(1 if args.post_id else args.workers)
        tasks = [
            asyncio.create_task(
                scrape_one_post(
                    context, guild_id, channel_id, post, index, len(posts), args, output_dir, browser_errors, semaphore
                )
            )
            for index, post in enumerate(posts.values(), 1)
        ]
        with output_path.open("w", encoding="utf-8") as out:
            for completed_task in asyncio.as_completed(tasks):
                record = await completed_task
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                out.flush()
                total_messages += len(record.get("messages", []))
                failed_posts += int(bool(record.get("error")))

        if total_messages == 0:
            bundle = await save_debug_bundle(
                page,
                output_dir,
                "No message roots detected; inspect post-level thread_snapshot/error fields",
                guild_id,
                channel_id,
                browser_errors,
            )
            print(f"Warning: message roots were not detected; diagnostics saved to {bundle}")
        print(
            f"Готово: {len(posts)} публикаций, {total_messages} сообщений, "
            f"ошибок публикаций {failed_posts}, workers={1 if args.post_id else args.workers}. Результат: {output_path}"
        )
        await context.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Собрать публикации и вложения из доступного Discord-форума через веб-интерфейс.")
    parser.add_argument("guild_id", nargs="?", default=DEFAULT_GUILD_ID, help=f"ID сервера (по умолчанию: {DEFAULT_GUILD_ID})")
    parser.add_argument("channel_id", nargs="?", default=DEFAULT_FORUM_CHANNEL_ID, help=f"ID форум-канала (по умолчанию: {DEFAULT_FORUM_CHANNEL_ID})")
    parser.add_argument("-o", "--output", default="discord_forum_export", help="Папка экспорта")
    parser.add_argument("--profile", default=".discord_browser_profile", help="Локальный профиль Chromium для сессии Discord")
    parser.add_argument("--max-scrolls", type=int, default=250, help="Лимит прокруток на форум/публикацию")
    parser.add_argument("--workers", type=int, default=4, help="Число параллельных публикаций при полном сборе (1–8)")
    parser.add_argument("--post-id", default="", help="Тестовый режим: собрать только одну публикацию по ID")
    parser.add_argument("--no-inspect-voters", dest="inspect_voters", action="store_false", help="Не открывать безопасные элементы просмотра списка голосовавших")
    parser.add_argument("--wait-timeout", type=int, default=600, help="Сколько секунд ждать авторизацию и появления публикаций")
    parser.add_argument("--no-download-files", dest="download_files", action="store_false", help="Не скачивать вложения, только сохранить ссылки")
    parser.set_defaults(download_files=True, inspect_voters=True)
    args = parser.parse_args()
    if args.max_scrolls < 1:
        parser.error("--max-scrolls must be at least 1")
    if not 1 <= args.workers <= 8:
        parser.error("--workers must be between 1 and 8")
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
