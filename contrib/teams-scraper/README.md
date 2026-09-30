# Teams chat scraper contract

`whosaid teams ingest` reads a JSON export of Microsoft Teams chat messages.
This directory documents the browser scraper that produces that export: the
Teams web client's DOM contract and the output shape. The scraper itself drives
an authenticated browser session over the Chrome DevTools Protocol, so it lives
outside whosaid's core. whosaid stays offline and only reads the JSON hand-off.

## Output: the hand-off JSON

Either a list of message records or an object with a `messages` list:

```json
[
  {"chat": "Project sync", "author": "Doe, Jane (Vendor, consultant)",
   "timestamp_iso": "2026-09-24T16:05:06Z", "epoch_ms": 1790265906000,
   "text": "i got the telemetry for the widget"}
]
```

| Field | Meaning |
|---|---|
| `chat` | Chat display name (left rail). |
| `author` | Author display name, as Teams renders it. |
| `timestamp_iso` | The message's `<time datetime>` value. |
| `epoch_ms` | The message's `data-mid`: epoch milliseconds, and the dedup key. |
| `text` | The message body as plain text. |
| `chat_id`, `url` | Optional: the Teams conversation id and page URL. |

Records may be unsorted and may repeat. Ingest dedups on `epoch_ms`, so
re-running a scrape over an overlapping window is safe.

```bash
whosaid teams ingest teams-export.json --into ~/meetings --tz America/Los_Angeles
```

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
