# Discord Forum Collector

Collects a Discord forum using the logged-in Chromium web UI (no bot or bot token). The GUI and collector are the project; generated exports, diagnostics, screenshots, and archives are not source files and should stay out of version control.

## Run

```bash
python discord_forum_gui.py
```

Choose **Полный сбор** to export the forum or **Тест публикации** to inspect a single post. The first run installs Playwright/Chromium if needed. Sign in to Discord in the opened browser; the local profile is reused on later runs.

To run the collector directly:

```bash
python discord_forum_collect.py [GUILD_ID] [FORUM_CHANNEL_ID] --workers 4
```

Default IDs are `1514607659758653560` (server) and `1515423283606982776` (forum channel). Single-post test mode uses `--post-id POST_ID`. To scrape forum posts in the collector's discovery order and stop after a chosen post (inclusive), use `--stop-after-post-id POST_ID`; this mode runs sequentially so no later post is started. The GUI has a matching stop-after field for full collection. The ID must be present in the discovered forum cards.

Exports are written under `discord_forum_export/` by default. Poll messages include `poll_voter_details`, with the detected count link, its URL, navigation result, visible user rows, and failure status; each post also includes a concise `poll_voter_diagnostics` summary. Voter names, IDs, and roles are recorded only when Discord visibly exposes them to the signed-in account; information Discord hides cannot be recovered or inferred.
