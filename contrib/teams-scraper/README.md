# Teams chat scraper

`whosaid teams ingest` reads a JSON export of Microsoft Teams chat messages.
[`teams-chat-scraper.js`](teams-chat-scraper.js) produces that export: it is
plain page JavaScript that runs inside an already signed-in Teams web tab. You
can drive it over the Chrome DevTools Protocol from any client (cdp-toolkit's
`evaluate_script`, Playwright, a raw CDP socket), or paste it into the tab's
console. It lives in `contrib/` because it needs a live browser session; whosaid
itself stays offline and only reads the JSON the scraper hands over.

## Running it

1. Open `teams.cloud.microsoft` and sign in. Optionally set
   `window.__TS_CUTOFF_DAYS = 14` first (the default is 7).
2. Evaluate the whole file in the tab. It installs `window.__ts` and starts the
   scrape without waiting for it to finish (see *Long scrapes* below).
3. Poll with short calls until `window.__ts.done` is `true`, watching
   `JSON.stringify(window.__ts.progress)`. A non-null `window.__ts.error` means
   the scrape failed.
4. Save `JSON.stringify(window.__ts.result)` to a file and ingest it:

```bash
whosaid teams ingest raw_by_chat.json --into ~/meetings --tz America/Los_Angeles --self Alice_Example
```

## Output: the hand-off JSON

`window.__ts.result` is `raw_by_chat`, an object keyed by chat name:

```json
{"Project sync": {"chat": "Project sync", "harvested": 40, "kept": 12, "reachedCutoff": true,
  "messages": [
    {"chat": "Project sync", "author": "Doe, Jane (Vendor, consultant)",
     "ts": "2026-09-24T16:05:06.000Z", "epoch": 1790265906000,
     "text": "i got the telemetry for the widget"}]}}
```

`whosaid teams ingest` accepts that map, and also a flat list of the same
records (`messages.json`), NDJSON with one record per line (`messages.ndjson`),
or `{"messages": [...]}`. Each record's fields:

| Field | Meaning |
|---|---|
| `chat` | Chat display name (left rail). |
| `author` | Author display name, as Teams renders it. |
| `epoch` (or `epoch_ms`) | The message's `data-mid`: epoch milliseconds, and the dedup key. |
| `ts` (or `timestamp_iso`) | The same instant as ISO 8601. It is used only when there is no epoch. |
| `text` | The message body as plain text. |
| `chat_id`, `url` | Optional: the Teams conversation id and page URL. |

Records may be unsorted and may repeat. Ingest dedups on the epoch, so
re-running a scrape over an overlapping window is safe.

## DOM contract (Teams web, `teams.cloud.microsoft`)

- **Chat list:** `[role="treeitem"][data-item-type="chat"]`. Folders are
  `data-item-type="custom-folder"` or `"chats"`. The left tree is virtualized,
  so scroll it to enumerate every chat.
- **Open thread rows:** `[data-tid="chat-pane-item"]` wrapping
  `[data-tid="chat-pane-message"]`.
- **Timestamp:** `data-mid` on the message element is epoch milliseconds, and a
  child `<time datetime="ISO">` agrees with it.
- **Author:** `[id^="author-"]` innerText. Consecutive messages from one author
  share one header, so carry the last-seen author forward.
- **Body:** `[id^="content-"]`, falling back to the message innerText.
- **History is virtualized:** the scroll container is
  `[data-tid="message-pane-list-viewport"]`. Scrolling up lazy-loads older
  messages and evicts off-screen ones, so harvest into a map keyed by
  `data-mid` on every scroll frame, not once at the end.
- **Filter pills (issue #57):** the rail's `Unread`, `Channels`, `Chats` and
  `Meeting chats` pills are `button[aria-pressed="true"]` when active. They are
  **sticky per client**, and a pressed pill hides chats from the rail (for example,
  `Chats` hides meeting chats). The scraper clears any pressed pill before it lists
  chats, waits for the rail to settle, and records what it cleared in
  `window.__ts.pills`. If the chat count changed after clearing it sets
  `window.__ts.progress.warning`, so a run that would have skipped chats does not
  look complete. It presses your pill(s) again when the scrape ends.

Scrape loop per chat: click the chat row, then scroll the viewport up in steps
of about 0.85 × `clientHeight` with a ~550 ms settle, re-harvesting each frame.
Stop when the oldest `data-mid` is older than the cutoff, the top is reached, or
several frames make no progress.

**Long scrapes and the CDP call limit.** A single `Runtime.evaluate` call is
capped at about 15 s, and a multi-chat scrape runs for minutes. Start the async
scrape detached (start the promise, return at once, keep results on a `window`
object), then poll that object with short calls. One awaited long call times
out and loses its return value.

## Scope

- Left-rail 1:1 and group chats only. Channel posts are a different surface and
  are not covered.
- Text bodies only. Reactions, attachments, images and quoted-reply bodies are
  flattened to text or dropped.
