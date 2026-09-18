# whosaid

Local, speaker-attributed transcription for Apple Silicon — who said what, on your Mac, nothing
leaves the machine.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Platform: macOS Apple Silicon](https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-lightgrey.svg)

![whosaid demo — speaker-attributed transcription on the terminal](docs/demo.gif)

**whosaid** pairs **Whisper** speech-to-text with **speaker diarization** to turn a meeting,
interview, call, or podcast recording into a transcript that says *who spoke when* — running fully
**offline and on-device** on an Apple Silicon Mac, with no cloud service, no API keys, and no Hugging
Face token. Think of it as `whisper` + speaker labels + voice-based speaker recognition, in one
command.

**Example output** (`meeting.speakers.txt`):

```text
[00:00:04] Alice: Thanks for jumping on, I know it's late for you.
[00:00:11] SPEAKER_01: No problem at all, happy to make it work.
[00:00:19] Alice: Let's start with the roadmap for next quarter.
[00:00:27] SPEAKER_01: Sounds good, I've got a few updates on that.
```

## Why whosaid

- **Meetings, interviews, calls, podcasts** — get a transcript where every turn is attributed to a
  person, not just a wall of text.
- **Privacy by construction** — audio, text, and voice embeddings never leave your Mac. There's no
  cloud step to opt out of, because there isn't one.
- **Your name on your own lines** — a one-time ~45s voice enrollment teaches whosaid your voice, so
  your turns read as your name instead of `SPEAKER_00`.
- **Tells you how many people spoke** — the number of distinct speakers is auto-detected and
  reported up front, with per-speaker turn counts and talk time.
- **Speaker cards to identify who's who** — for every speaker, whosaid writes a card of that voice's
  most representative snippets, so you can read a few lines and know who was talking.
- **Remembers people you name** — identify a speaker once with `whosaid relabel`, and their
  voiceprint is saved to a private local registry so they're auto-named in every future transcript.
- **Fast on long recordings** — recordings over ~15 min are diarized in parallel windows and
  stitched back into consistent speakers by voiceprint, so an 80-minute meeting is minutes, not
  tens of minutes, recovering the same speakers as a single-pass run.
- **No accounts, no API keys, no Hugging Face token** — every model comes from an open, ungated
  source.
- **Search everything you have recorded.** `whosaid index` builds a local full-text (and,
  with Ollama, meaning) index over every transcript in a meeting workspace; `whosaid search`
  finds the turn, `whosaid context` shows the minute around it, and `whosaid graph` answers
  who committed to what, when. See [Search your meetings](#search-your-meetings).
- **Hands-free from Voice Memos.** `whosaid watch install` puts a launchd agent on the Voice
  Memos folder (or any folder): stop recording, and a few minutes later the meeting is
  transcribed, summarized, and searchable. See [Hands-free ingest](#hands-free-ingest-whosaid-watch).
- **Usable from an AI agent, too.** `whosaid mcp` exposes transcribe, relabel, doctor, and the
  read-only workspace search tools as MCP tools for Claude Code, Claude Desktop, Kiro, and other
  MCP clients, with the same local-only guarantee as the CLI.

## How it compares

**Looking for a MacWhisper, whisperX, or aTrain alternative?** If you want speaker-attributed
transcription that runs fully offline on Apple Silicon and can put real *names* on voices — not just
`SPEAKER_00` labels — that's the gap whosaid fills. Here's how it stacks up:

| | whosaid | whisperX | plain mlx-whisper | cloud transcription APIs |
|---|---|---|---|---|
| Speaker labels | Yes | Yes | No | Varies by provider |
| Names speakers by voice | Yes (enrollment) | No | No | No |
| Remembers speakers across meetings | Yes — persistent local voiceprint registry | No | No | No |
| Runs fully offline | Yes | Partial — needs a gated model download | Yes | No |
| Parallel diarization on long audio | Yes | No | — (no diarization) | Varies by provider |
| Needs an account / token | No | Yes — Hugging Face token for gated pyannote models | No | Yes — API key |
| Install weight | `ffmpeg` + `uv`, ephemeral environments | `torch` + `pyannote` + the full HF stack | `mlx-whisper` only | None (network client only) |

## Quickstart

```bash
git clone https://github.com/sblattj/whosaid && cd whosaid   # get the code
./bootstrap.sh                                                # check deps, download models, install ~/.local/bin/whosaid
whosaid enroll                                                # ~45s reading a printed passage — teaches whosaid your voice
whosaid path/to/meeting.m4a                                   # transcribe + diarize + label -> meeting.speakers.txt (and friends)
```

If `~/.local/bin` is not on your shell's `PATH`, add it or invoke the installed command by its
absolute path. `WHOSAID_INSTALL_DIR=/another/bin ./whosaid install` selects another install
directory. The installed command is a symlink to the checkout, so updating the checkout updates the
command without copying or duplicating the implementation.

## Commands

| Command | What it does |
|---|---|
| `./bootstrap.sh [--yes]` (also `whosaid setup`) | Capability check, dependency install, model pre-download, and command installation. Idempotent — safe to re-run. |
| `whosaid install [--force]` | Install/update the command symlink in `~/.local/bin` (or `WHOSAID_INSTALL_DIR`). Refuses to replace an unrelated command, and refuses (unless `--force`) to install from a checkout under `/tmp`, `/private/tmp`, `/var/tmp`, or `$TMPDIR` — that symlink would dangle once the OS cleans the temp directory up. |
| `whosaid enroll [Name]` | Records ~45s from the mic reading a printed passage, saves `voices/<Name>.wav`. See [Speaker identity: enrollment clips vs. the registry](#speaker-identity-enrollment-clips-vs-the-registry). |
| `whosaid enroll <Name> --from FILE [--ss T] [--t D\|--to T] [--force]` | Extracts a clip from an existing recording instead of the mic (extract → verify → save, no interaction). `--ss`/`--t`/`--to` accept seconds or `M:SS`/`H:MM:SS`; same ≥15s/non-silent bar as mic enrollment. |
| `whosaid record [--label L]` | Foreground mic capture to `recordings/<timestamp>[-label].m4a`, then transcribes automatically. |
| `whosaid <audio>… [flags]` | The default command: transcribe + diarize + label one or more audio files. `whosaid transcribe <audio>…` is the same command written explicitly (matching the `whosaid_transcribe` MCP tool name). |
| `whosaid relabel <base> SPEAKER_02=Jane …` | Put real names on clusters after reading the speaker cards. Rewrites the transcript + cards and saves each named voiceprint to the local registry for future transcripts. No re-transcription. |
| `whosaid relabel <base> --auto` | Re-apply naming to an existing transcript with no assignments: re-runs registry matching + the absorb pass over the cached sidecar and rewrites the transcript + cards. Picks up voices enrolled after the transcript was made, and folds phantom cluster splits of one person into a single speaker. No re-transcription, no re-diarization. In a meeting workspace the base is `transcript`. See [Speaker identity: enrollment clips vs. the registry](#speaker-identity-enrollment-clips-vs-the-registry). |
| `whosaid samples <base> [-o DIR] [--audio FILE] [--per-speaker N] [--seconds S] [--json]` | Export one short representative WAV per speaker cluster — the longest diarized segment, clamped to `--seconds` (default 8) — so you can listen and confirm an identity before trusting an auto-label or enrolling. Cuts from the sidecar's own `source.path`, or an explicit `--audio FILE` for a sidecar written before that metadata existed. |
| `whosaid ingest <audio>… --into DIR [--action-items] [--engine E] [--index]` | Transcribe a batch into dated meeting folders (idempotent by content hash). `--engine` picks the action-item summarizer, `--index` runs roll-up + index afterwards. See [Meeting workspaces](#meeting-workspaces). |
| `whosaid roll-up <ws> [--action-items] [--index] [--owner NAME\|me] [--all-owners]` | Rebuild the workspace index (`_INDEX.md`), audit, the deduplicated action-item and dev-commitments corpora, and the owner's ranked `_WORKLIST-<Owner>.md`; `--index` then rebuilds the search index too. |
| `whosaid commitments <ws> [--owner NAME\|me] [--all-owners] [--json] [-o FILE]` | Print the ranked personal worklist (P1/P2/P3) on demand from the corpora, without a roll-up. See [Dev-commitments](#dev-commitments). |
| `whosaid index <ws> [--no-embed] [--rebuild]` | Build `<ws>/_search.db` (full text, optional embeddings, entity graph) and `<ws>/_WIKI.md`. See [Search your meetings](#search-your-meetings). |
| `whosaid search <ws> "<query>" [--mode exact\|meaning\|hybrid] [--speaker S] [--meeting M] [-k N] [--json]` | Search every speaker-labeled transcript; each hit is a meeting, a timestamp, a speaker, and a snippet. |
| `whosaid context <ws> <meeting> <HH:MM:SS> [--before S] [--after S] [--json]` | The verbatim turns around a moment, for reading a hit in place. |
| `whosaid graph <ws> items\|item AI-NNN\|person [Name]\|prs\|meetings\|speakers [--json]` | Entity views: action items with filters, one item's history, a person's commitments and asks, PR mentions, meeting coverage, talk share. |
| `whosaid wiki <ws> [--stdout] [-o FILE]` | Regenerate `<ws>/_WIKI.md` from the graph. |
| `whosaid watch run\|install\|uninstall\|status --into <ws>` | Hands-free ingest of new recordings via a launchd agent. See [Hands-free ingest](#hands-free-ingest-whosaid-watch). |
| `whosaid memos list\|pull\|delete "<title>"\|shortcut-recipe` | Voice Memos helpers that never touch the app's files or database. See [Voice Memos helpers](#voice-memos-helpers-whosaid-memos). |
| `whosaid doctor` | Read-only environment report, including Ollama reachability and the index status of `$WHOSAID_WORKSPACE`. |
| `whosaid version` \| `whosaid --version` \| `whosaid -V` | Print the installed version (plus a `git describe` suffix when run from a git checkout). |

### Enroll from an existing recording

Already have a clip of the person speaking? Skip the mic and cut a reference straight from it:

```bash
whosaid enroll Alice --from meeting.m4a --ss 3:20 --t 20   # or --to 3:40
```

## Meeting workspaces

One-off transcriptions are files; a recurring meeting series is a corpus. The meeting-workspace
layer gives that corpus a home: every recording is transcribed into a dated folder, each meeting
can carry generated action items and dev commitments, and one roll-up produces the index, the
audit, a living action-item list, and the dev-commitments corpus across all of them — still
entirely offline.

### `whosaid ingest` — a batch into dated folders

```bash
whosaid ingest weekly/*.m4a --into ./meetings --folder-by created \
  --tz America/Los_Angeles --action-items
```

Every file is transcribed with the full set of `whosaid <audio>…` flags passed through, into
`meetings/YYYY-MM-DD-HHMM/`. The timestamp comes from the recording's own container
`creation_time` rendered in `--tz` (default UTC), falling back to the file's mtime — so folder
order reflects when meetings actually happened, not when you got around to copying the files.
Ingest is idempotent by source sha256: re-running a batch never re-transcribes or duplicates a
recording.

With `--action-items`, each meeting also gets an `action-items.md`. Generation is pluggable: the
hook command receives the speaker-labeled transcript on stdin, plus `WHOSAID_SPEAKERS` and
`WHOSAID_TRANSCRIPT_PATH` in its environment, and whatever it writes to stdout becomes the
markdown. Pass it per-run with `--hook CMD`, or set `WHOSAID_ACTION_ITEMS_HOOK` once. With no
hook, a skeleton is written instead and everything stays offline. Example hook — illustrative
only; any command that turns stdin into markdown works:

```bash
#!/bin/sh
# Drafts action items with a local Ollama model.
ollama run llama3.2 "List this meeting's action items as markdown bullets (Owner: task):"
```

You no longer need a hook to get a real draft: `--engine ollama` (or the default `--engine
auto`, which uses Ollama when it is running) turns on the built-in summarizer described in
[Action items drafted by a local model](#action-items-drafted-by-a-local-model). `--engine`
implies `--action-items`. Add `--index` and, once every file is in, ingest runs
`whosaid roll-up <ws> --action-items` and `whosaid index <ws>` for you, so the new meetings are
searchable in the same command:

```bash
whosaid ingest weekly/*.m4a --into ./meetings --engine ollama --index
```

With `--commitments`, each meeting also gets a `commitments.md` — the first-person commitments
*you* made in it (see [Dev-commitments](#dev-commitments)). Roles set once via
`whosaid relabel --role` decide whose cues count: tag your own voice `self` (and your manager
`boss`), and the extractor tracks what you promised, ranking boss-requested items higher.

### `whosaid roll-up` — index, audit, and the action-item corpus

```bash
whosaid roll-up ./meetings --action-items
```

- **Coverage index** — `_INDEX.md` holds one row per meeting (created date, duration, and whether
  it is transcribed, diarized, and has action items), plus a nothing-missing audit that flags
  orphan directories and stale manifest entries, and a recurring-topics section that surfaces
  themes appearing across meetings. Once any dev commitments exist, a one-line open/total count
  section is appended too. Both output paths are overridable with `-o` and
  `--action-items-out`.
- **Action-item corpus** — with `--action-items`, `_ACTION-ITEMS.md` deduplicates items across
  meetings (by text similarity; threshold `--similarity-threshold`, 0.5–1.0, default 0.82) and
  groups them by owner, then status. Ids are stable (`AI-001`… and never renumber), each item
  carries `first_seen`/`last_seen` dates and its occurrence list, and open/ongoing/resolved
  statuses survive re-runs — so the corpus reads as living history across the series, not a
  per-meeting snapshot. Items can carry a free-form type, rendered in parens after the status,
  and pairs scoring just under the threshold are surfaced in a _Possible duplicates (review)_
  section at the end.
- **Dev-commitments corpus** — each meeting's `commitments.json` (from `ingest
  --commitments`) folds into `_COMMITMENTS.md` automatically whenever any exists: stable
  `CM-NNN` ids that never renumber, the same 0.82 text-similarity dedupe and 0.10 near-miss
  review band, grouped by status then speaker, with `**[boss]**` marking boss-requested items.
  Hand edits in `_COMMITMENTS.md` (mark one `done`, retitle, merge via `(merged CM-NNN)`) fold
  back on the next roll-up, exactly like the action-item corpus.
- **Personal worklist** `_WORKLIST-<Owner>.md`: whenever a commitments corpus or owner-attributed
  action items exist, roll-up also writes the owner's open items ranked into P1/P2/P3 (see
  [Dev-commitments](#dev-commitments)). The owner is `--owner NAME`, else the `self`-roled
  speaker, else `[workspace] owner`; `--all-owners` writes one file per participant. It is a
  regenerated view, never reconciled: edit the corpora, not the worklist.

Dedupe is text similarity (difflib, the `--similarity-threshold` above) and, when a local
Ollama answers on `127.0.0.1` with `[search] embed = true`, also embedding cosine: two texts
match when either the difflib ratio or the `[search] embed_model` cosine clears its threshold
(`[commitments] embed_threshold`, default 0.90), so "I'll write the rollout runbook for the
platform team" and "for the platform team write the rollout runbook" fold into one item. No
Ollama, `embed = false`, or a non-loopback URL means difflib only, with one log line saying so.

Roll-up is incremental and append-only by default: re-running with nothing new writes nothing.
`--rebuild` is the escape hatch — it resets the manifest and corpus and regenerates both from the
folders on disk.

State is plain JSON in the workspace directory — `_workspace.json` (the manifest; it points at
the dev-commitments corpus via `commitments_corpus`) and `_action-items.json` (the corpus; it
also records the `similarity_threshold` in effect), plus the parallel `_commitments.json`.
All are safe to read and hand-edit — marking an item `resolved` by hand is the intended way to
close one the extractor phrased wrong. Hand edits made directly in `_ACTION-ITEMS.md` (and,
same contract, `_COMMITMENTS.md`) are folded back on the next roll-up and survive re-runs:
statuses, types, retitles, and `(merged AI-NNN)` merge annotations (the merged item stays at
its id, rendered collapsed as `[merged → AI-NNN]`). Only `--rebuild` discards them. `--index`
runs `whosaid index <ws>` right after the roll-up.

## Search your meetings

A workspace with a dozen meetings is a corpus you cannot reread. `whosaid index` turns it into
something you can query: an exact full-text index over every turn, optional meaning search
through a local embedding model, an entity graph (people, meetings, action items, timestamped
commitments, PR mentions), and a generated wiki. Everything lives in the workspace, everything is
rebuildable, and nothing leaves the machine.

```bash
whosaid index ./meetings                     # build _search.db + _WIKI.md (embeds if Ollama is up)
whosaid index ./meetings --no-embed          # exact search only, no Ollama needed
whosaid search ./meetings "budget"           # every turn that says budget
whosaid search ./meetings "who is blocked" --mode meaning
whosaid search ./meetings '"token exchange"' --speaker Bob_Example --meeting 2026-09-16 -k 5
whosaid context ./meetings 2026-09-16-0900 00:00:06   # the verbatim minute around a hit
whosaid graph ./meetings items --owner Alice_Example --status open
whosaid graph ./meetings item AI-003         # one item: occurrences + timestamped commitments
whosaid graph ./meetings person Bob_Example  # talk share, commitments made, asks received
whosaid graph ./meetings prs | whosaid graph ./meetings meetings | whosaid graph ./meetings speakers
whosaid wiki ./meetings --stdout             # the generated wiki, regenerated from the graph
```

`<ws>` can be left out everywhere: the commands fall back to `$WHOSAID_WORKSPACE`, then to the
current directory when it holds `_workspace.json` or `whosaid.toml`. Any subfolder with a
`*.speakers.txt` counts as a meeting, dated or hand-named, so transcripts you made by hand are
indexed too.

**Three search modes.** `--mode exact` (the default) is SQLite FTS5: words, `"quoted phrases"`,
`AND`/`OR`/`NOT`, and `prefix*`, and it needs nothing but Python. `--mode meaning` embeds the
query with `nomic-embed-text` through Ollama on `127.0.0.1:11434` and returns the nearest turns,
so "who is blocked" finds "I'm stuck until the review lands" with no shared words. `--mode hybrid`
runs both and fuses the rankings, which is usually what you want once the index has embeddings.
Every hit prints the same way, and `--json` gives you one object per hit with the same fields:

```text
$ whosaid search ./meetings "budget"
whosaid: engine: exact
[2026-09-16-0900 @ 00:00:06] Bob_Example: I will review the »budget« spreadsheet before Thursday.
1 hit(s).

$ whosaid context ./meetings 2026-09-16-0900 00:00:06 --before 10 --after 10
whosaid: 2026-09-16-0900: 3 turn(s) between 0s and 16s
[00:00:01] Alice_Example: Let's start with the roadmap for next quarter.
[00:00:06] Bob_Example: I will review the budget spreadsheet before Thursday.
[00:00:14] Alice_Example: Great, and I will send the draft out to the team today.
```

Set Ollama up once if you want meaning search: `brew install ollama && brew services start ollama
&& ollama pull nomic-embed-text`. Without it, `index` skips the embedding pass with a note and
`search` runs exact-only; nothing fails.

**Graph views.** The entity tables are built from reliable signals only: the manifest, the
deduplicated action-item corpus, each meeting's `action-items.md`, and the transcripts
themselves. `items` filters by `--owner`, `--requester`, `--status`, and `--type`; `item AI-NNN`
shows one item's occurrences and every timestamped commitment behind it; `person [Name]` is the
per-owner view (talk share, what they committed to, what they asked others for); `prs` lists
pull-request numbers mentioned in speech or in the notes; `meetings` is coverage per folder; and
`speakers` is talk share across the workspace. Every view takes `--json`.

**The wiki.** `_WIKI.md` is a generated rollup: people, meetings, open items, and PR mentions,
each action item citing the `[meeting @ time]` it was committed at. It is regenerated by `index`
and by `whosaid wiki`, so it never drifts from the data. Edit the corpus, not the wiki.

**Per-workspace settings: `whosaid.toml`.** Optional, next to the transcripts. Every key has a
default; `WHOSAID_OWNER`, `WHOSAID_OLLAMA`, and `WHOSAID_SUMMARIZER_MODEL` override the matching
keys per run.

```toml
[workspace]
owner = "Alice_Example"          # whose action items this workspace tracks
aliases = ["Ali", "Alicia"]      # how the transcript may misspell the owner (default: first name)

[groups]                         # optional, ordered; drives the action-item sections
leadership = ["Bob_Example"]
team = ["Carol_Example", "Dan_Example"]

[summarizer]
engine = "auto"                  # auto | ollama | hook | none
model = "qwen2.5:14b"
timeout = 900                    # seconds per model call

[search]
ollama = "http://127.0.0.1:11434"
embed_model = "nomic-embed-text"
embed = true                     # false: exact search only, never contact Ollama

[commitments]                    # optional; ranks _WORKLIST-<Owner>.md. A list you set replaces
boss = []                        # the default list, so omit a key to keep the built-in cues.
deadline_cues = ["today", "tonight", "tomorrow", "eod", "end of day", "this week", "next week"]
blocking_cues = ["blocking", "blocked", "urgent", "asap", "critical", "hotfix", "prod", "outage", "customer", "release", "ship"]
embed_threshold = 0.90           # cosine at or above this is a duplicate (with [search] embed)
[commitments.weights]            # score = sum of the signals that fired
boss = 5
blocking = 4
deadline = 4
repeat = 2                       # per extra meeting
recent = 1
strong = 1
requested = 1
negative = -3

[watch]
source = ""                      # folder to watch (default: the macOS Voice Memos store)
```

**What `_search.db` is.** One SQLite file in the workspace: an FTS5 table of every turn (meeting,
timestamp, speaker, text), a table of stored embeddings keyed by turn, and the graph tables.
It is a derived artifact. Delete it whenever you like and run `whosaid index` again; `--rebuild`
does the same and also re-embeds every turn.

## Action items drafted by a local model

`whosaid ingest --action-items` can draft each meeting's `action-items.md` with a local model
instead of a hook. `--engine` (or `[summarizer] engine` in `whosaid.toml`) picks how:

| Engine | What happens |
|---|---|
| `auto` (default) | The hook if one is configured, else Ollama if it answers on localhost, else the skeleton. |
| `ollama` | The built-in summarizer below. If Ollama is down or the model is missing, it warns and writes the skeleton (exit 0). |
| `hook` | The `--hook` / `WHOSAID_ACTION_ITEMS_HOOK` command, exactly as before. |
| `none` | The skeleton only (speakers listed, no items). |

The model never gets to invent timestamps or evidence. The script picks the candidate turns
deterministically (every substantive turn by the workspace owner, every turn that names the
owner, plus one model pass over the rest for unnamed asks), then the model reads one turn at a
time (long turns in sentence groups) and answers either `SKIP` or bullets that each carry a short
verbatim quote. Every quote is checked against its turn before it is kept; a mismatch is flagged
rather than dropped silently, and the evidence turns are appended verbatim at the bottom of the
file.

Sections come from your config: the owner's own commitments, one section per `[groups]` entry
(asks from leadership, asks from the team, and so on), and an inferred section for the rest. The
file opens with a **DRAFT** banner so a reader knows it has not been reviewed, and ends with an
evidence block of the exact turns each bullet came from. Read the evidence before folding
anything into `_ACTION-ITEMS.md`.

Model choice: the default is `qwen2.5:14b` (`ollama pull qwen2.5:14b`; about a minute for an
hour-long meeting on an M-series Mac). `WHOSAID_SUMMARIZER_MODEL=qwen2.5:7b` is roughly twice as
fast and roughly twice as noisy. Ollama is only ever contacted on `127.0.0.1`.

## Hands-free ingest: `whosaid watch`

Record a meeting in Voice Memos, walk away, and have it transcribed, summarized, and searchable a
few minutes after you press stop. `whosaid watch` is a launchd LaunchAgent that watches a folder
(the macOS Voice Memos store by default) and pushes each new recording through
`whosaid ingest --action-items --commitments`, then `roll-up` (which also refreshes the
worklist), then `index`.

```bash
whosaid watch install --into ~/meetings --seed      # install; mark the memos already there as done
whosaid watch install --into ~/meetings --dry-run   # print the plist and every step; change nothing
whosaid watch status  --into ~/meetings             # installed? loaded? last run? (--json too)
whosaid watch run     --into ~/meetings --dry-run   # what WOULD be ingested right now
whosaid watch uninstall --into ~/meetings --purge   # stop the agent, remove state + staging + interpreter
```

How a pass works: every audio file in the source folder that is not yet in
`<ws>/.watch_state.json` and whose mtime has been stable for `[watch] stable_seconds` (default
120) is copied into `<ws>/.watch_staging/` and ingested from there; a file whose mtime is still
moving is treated as still recording or still syncing, and the pass waits (bounded by
`max_wait_seconds`) and rescans. A `.watch.lock` keeps two passes from overlapping. The agent
fires on folder changes (`WatchPaths`) and every `--interval` seconds (default 900) as a safety
net. `--seed` marks the recordings already present as done so an existing library is never
reprocessed. `--source DIR` watches any folder, not just Voice Memos; `--offline` bakes the
Hugging Face offline variables into the agent so a machine with no network access never tries to
fetch; `--env K=V` adds any other environment the agent should carry; `--engine E` on `run` picks
the summarizer.

**Full Disk Access, scoped to one binary.** The Voice Memos store is TCC-protected: only an
executable you have granted Full Disk Access can read it, and macOS grants that per executable
path. Granting it to your terminal or your everyday Python would privilege everything they run.
So `install` provisions one dedicated, ad-hoc-signed interpreter at
`$HOME/.local/opt/whosaid-watch/bin/whosaid-watch` (named so that is what the FDA list shows),
points the agent at it, and opens the right System Settings pane. That interpreter runs nothing
but the watcher; it copies each recording out of the store and hands the copy to `whosaid`, so
whosaid, `uv`, `ffmpeg`, and the models read an ordinary file and need no grant at all. Adding
that one path is the single manual step. Already granted a binary? `--interpreter PATH` reuses
it instead of provisioning a new one. Several workspaces can coexist: the label defaults to
`com.whosaid.watch.<8 hex of the workspace path>`, or pass `--label`.

Logs land in `<ws>/.watch.log` (launchd captures the watcher's stdout and stderr there).
`uninstall` unloads and removes the agent; `--purge` also removes the dedicated interpreter and
the workspace's `.watch_state.json`, `.watch.lock`, and `.watch_staging/`, never the log.

## Voice Memos helpers: `whosaid memos`

```bash
whosaid memos list                          # titles and sync state (reads a COPY of the database)
whosaid memos pull --latest -o ./inbox      # copy the newest recording out of the store
whosaid memos pull --title "Standup" -o .   # or one by exact title
whosaid memos delete "Standup" --yes        # delete through the app's own action
whosaid memos shortcut-recipe               # build (or print how to build) the Shortcut delete uses
```

`delete` never touches `CloudRecordings.db` or the `.m4a` files. It runs the Voice Memos
"Delete Recordings" App Intent through a one-action Shortcut named "Delete Voice Memo", which is
exactly what tapping Delete in the app does: iCloud stays in sync across your devices, the memo
lands in Recently Deleted (restorable for 30 days), and the app's database stays consistent.
Editing the store behind the app's back desyncs iCloud and can corrupt the library, which is why
there is no `--force` path that does. `shortcut-recipe` builds and signs that Shortcut for you
when signing is available; on a Mac with no iCloud account it prints the one-action recipe to
build by hand in the Shortcuts app (`--no-sign` prints the recipe only).

## Dev-commitments

A dev-commitment is a first-person promise the **self**-roled speaker made to someone else
("I'll send the migration plan by Friday"). Each meeting's transcript yields a
`commitments.md` + `commitments.json`; roll-up folds those into one living corpus,
`_COMMITMENTS.md` / `_commitments.json` — so "what did I promise across this whole series?"
has a single answer.

Extraction is a stdlib heuristic, no LLM and no network: a clause starting with a first-person
cue (`i'll`, `i will`, `i plan to`, `let me`, `i owe`, …) records the clause; negations
(`i won't`, `i can't`) are kept but flagged `negative`, and question clauses are skipped. Roles
drive it: with roles set only the `self` speaker's cues count, and when the turn before a
commitment was a different speaker asking or directing ("can you…", "please…"), the item
records `requested_by` — priority `high` when that speaker's role is `boss`. A pluggable hook
(`--hook CMD` on the commitments subcommand, or `WHOSAID_COMMITMENTS_HOOK`) can replace the
heuristic; it receives the transcript on stdin plus `WHOSAID_SPEAKERS` and `WHOSAID_ROLES`.

The corpus works exactly like the action-item corpus: stable `CM-NNN` ids that never renumber,
the same 0.82 text-similarity dedupe with a 0.10 near-miss review band, and hand edits in
`_COMMITMENTS.md` folded back on the next roll-up. An item's rendered shape:

```text
- **CM-002** [open] (Stephen) 2026-09-14-1802 → 2026-09-21-1802 (2×): **[boss]** update the exec deck
```

### The ranked worklist: `_WORKLIST-<Owner>.md` and `whosaid commitments`

The corpus answers "what did I promise"; the worklist answers "what should I do first". Every
roll-up (and `whosaid commitments <ws>` on demand) takes the union of the owner's commitments
(`CM-NNN`, speaker = owner) and the action items assigned to them (`AI-NNN`, owner matched
case-insensitively with `_`/space interchangeable, plus `[workspace] aliases`), folds the two
sources together (an action item that restates a commitment appears once, as `(also AI-012)`),
and ranks what is open. Deterministic, no LLM:

| Tier | Rule |
|---|---|
| **P1** | boss-requested (`priority high`, requester role `boss`, `[commitments] boss`, or a `[groups] leadership` name when no role is recorded), a blocking/urgency cue (`blocking`, `urgent`, `asap`, `prod`, `outage`, `customer`, `release`, `ship`, …), a deadline cue (`tomorrow`, `eod`, `by Friday`, `next week`, an ISO date, …), or seen in 3+ meetings |
| **P2** | seen in 2 meetings, requested by anyone, or a strong cue (`i'll own`, `i will`, `i promise`, `i owe`, …) in the latest meeting |
| **P3** | the rest (weak cues such as `i can`, `let me`, `i plan to` earn nothing) |

A negated commitment (`i won't`) is never P1 and carries a penalty. Within a tier the order is
score (sum of the signal weights), then most recent, then id, and every line says why:

```text
- **CM-002** [open] 2026-09-14-1802 → 2026-09-21-1802 (2×) P1 · boss · due=tomorrow · 2 meetings: update the exec deck (also AI-012)
```

Resolved and merged items sit under `## Done / history`. The file is a regenerated view (hand
edits are overwritten on the next run; ids never renumber; the JSON corpora stay the source of
truth), so close or retitle items in `_COMMITMENTS.md` / `_ACTION-ITEMS.md`. The owner is
`--owner NAME`, or `--owner me` (the default): the speaker with role `self` in the newest
meeting's `commitments.json` or `# Role:` header, else `[workspace] owner` / `WHOSAID_OWNER`.
`--all-owners` writes one file per participant. `whosaid commitments <ws> --json` emits
`{owner, generated_from, items: [{id, source, text, status, tier, score, why, first_seen,
last_seen, occurrences, requested_by, negative, …}]}` for scripts and the MCP tool. Cue lists,
boss names, weights, and the embedding threshold are overridable in `whosaid.toml`
`[commitments]` (see the example under [Search your meetings](#search-your-meetings)).

## Use it from an AI agent (MCP)

whosaid's local, private, GPU transcription and speaker diarization are also exposed as MCP tools,
so any MCP client — Claude Code, Claude Desktop, and others — can call them directly instead of
shelling out to the CLI. Launch is `whosaid mcp`, a stdio server that needs only `uv`, which whosaid
already requires; audio never leaves the machine, exactly as with the CLI.

Add it to your MCP client config:

```json
{
  "mcpServers": {
    "whosaid": { "command": "/ABSOLUTE/PATH/TO/whosaid", "args": ["mcp"] }
  }
}
```

`command` is the path to the `whosaid` script itself — the checkout's `./whosaid`, or the installed
`~/.local/bin/whosaid` symlink.

| Tool | What it does |
|---|---|
| `whosaid_transcribe` | Transcribes + diarizes an audio file and writes the labeled transcript, speaker cards, and a sidecar for relabeling. |
| `whosaid_relabel` | Names `SPEAKER_NN` clusters and remembers them — saved to the local registry and auto-applied to every future transcript. Optional `roles` ({Name: role}) tags speakers (self/boss/peer/report/external). |
| `whosaid_list_speakers` | Read-only: lists enrolled voices and registry names (with role tags) already known. |
| `whosaid_doctor` | Read-only readiness check — models cached, deps present, mic/audio devices — run this first when a transcribe fails. |
| `whosaid_enroll_from_file` | Enrolls a named voice from an existing audio clip, no mic needed. |
| `whosaid_samples` | Exports one short representative WAV per speaker cluster (longest segment, clamped to `seconds`) so you can listen and confirm an identity before trusting a label. |

`enroll` and `record` (microphone capture) stay CLI-only — they need an interactive terminal and
Microphone permission. The first `whosaid_transcribe` call downloads ~1.5 GB of models; call
`whosaid_doctor` first to check readiness.

### Workspace search tools (read-only)

The [workspace search](#search-your-meetings) layer is exposed too, so an agent can answer "what
did we decide about the budget" from the transcripts instead of reading them whole. Every tool
takes an optional `workspace` argument and otherwise uses `WHOSAID_WORKSPACE` from the server's
environment; there is no current-directory fallback. None of them rebuilds the index: `whosaid
index` (or the watcher) owns writes, and the tools only read `_search.db`.

| Tool | What it does |
|---|---|
| `whosaid_search` | Turn-level hits (meeting folder, timestamp, speaker, snippet) for a query in `exact`, `meaning`, or `hybrid` mode, with speaker/meeting filters. |
| `whosaid_context` | The verbatim turns around one hit, so an agent reads a minute instead of a transcript. |
| `whosaid_items` | The action-item corpus, filterable by owner, requester, status, and type. |
| `whosaid_item` | One item with its occurrences and timestamped commitments. |
| `whosaid_person` | A person's talk share, the commitments they made, and the asks they received. |
| `whosaid_meetings` | Every meeting folder with coverage figures. |
| `whosaid_prs` | Pull-request numbers mentioned in speech or in the notes. |
| `whosaid_speakers` | Talk share per speaker across the workspace. |
| `whosaid_workspace_status` | Whether the index exists, how many turns it holds, and whether Ollama is reachable. |
| `whosaid_worklist` | The owner's ranked worklist (P1/P2/P3 with score and why) from the commitments and action-item corpora, the `whosaid commitments --json` payload; `owner` defaults to `me`. |

Resources, all reading `WHOSAID_WORKSPACE`: `whosaid://workspace/wiki` (`_WIKI.md`),
`whosaid://workspace/action-items` (`_ACTION-ITEMS.md`), `whosaid://workspace/index`
(`_INDEX.md`), and per meeting `whosaid://workspace/meeting/{folder}/transcript` and
`whosaid://workspace/meeting/{folder}/action-items`.

Register it with the workspace set. Claude Code:

```bash
claude mcp add --scope user whosaid -e WHOSAID_WORKSPACE=$HOME/meetings -- whosaid mcp
```

Kiro (`~/.kiro/settings/mcp.json`) and any client that takes the same JSON shape:

```json
{
  "mcpServers": {
    "whosaid": {
      "command": "whosaid",
      "args": ["mcp"],
      "env": { "WHOSAID_WORKSPACE": "/Users/<you>/meetings" }
    }
  }
}
```

Write the workspace as an absolute path (most clients do not expand `$HOME` inside `env`), and use
the absolute path to the `whosaid` script for `command` if it is not on the client's `PATH`.

### Key flags (on `whosaid <audio>…`)

| Flag | Meaning |
|---|---|
| `-o, --outdir DIR` | Output directory (default: alongside the input file). |
| `-m, --model NAME` | Whisper model to use. |
| `--accurate` | Use the full `large-v3` model instead of the default `large-v3-turbo`. |
| `-l, --lang LANG` | Force the transcription language. |
| `-f, --format FMT` | Output format: `txt`, `srt`, `vtt`, `tsv`, `json`, or `all`. |
| `-n, --name NAME` | Override the output base name (single input only; default: derived from the input filename). |
| `--speakers N` | Exact number of speakers. Overrides auto-detect (and any `--min/--max-speakers`). |
| `--min-speakers N` | Lower bound on the auto-detected speaker count. |
| `--max-speakers N` | Upper bound on the auto-detected speaker count; also lowers the hard cap of 20. Use a range when you know roughly who was in the room but not exactly — an estimate that lands on the bound is reported as unreliable rather than shipped as a result. |
| `--expected-speakers A,B,C` | Roster of people you expect in this recording (comma-separated, repeatable). Each name must already be a known voice — an enrolled clip or a registry entry. Clustering is **anchored** to their voiceprints: a turn within `--anchor-threshold` of one of them is pinned to that person, everyone else is clustered into new speakers, and a listed person who never speaks is dropped. This is the flag for a recurring team with varying attendance, where `--speakers N` would merge distinct people. |
| `--anchor-threshold F` | Per-turn cosine required before `--expected-speakers` pins a turn to a known voice. Default `0.70`; turns below it fall through to ordinary clustering rather than taking a low-confidence name. |
| `-j, --jobs N` | Parallel diarization workers for long audio (default: auto, ~cores−2, capped at 8). |
| `--chunk-seconds S` | Window length for parallel diarization (default: auto — about `--jobs` windows, min 300s). |
| `--no-chunk` | Diarize the whole file in a single pass (disable parallel chunking). |
| `--match-threshold F` | Cosine similarity a known voice must reach before it may claim a cluster (alias `--ref-threshold`). Default `0.50`; a cluster whose best candidate scores below `F` keeps its anonymous `SPEAKER_NN` label rather than taking a low-confidence name. Raise it (e.g. `0.6`) if you see wrong names, lower it to catch more. |
| `--absorb-threshold F` | Cosine similarity at which a *still-unnamed* cluster is folded into a known voice, merging phantom splits of one person. Default `0.85`. |
| `--no-diarize` | Skip diarization; write the plain transcript only. |

### Environment variables

| Variable | Purpose |
|---|---|
| `WHOSAID_MODEL` | Default Whisper model, overridden by `-m`. |
| `WHOSAID_LANG` | Default transcription language, overridden by `-l`. |
| `WHOSAID_VOICE_REFS` | Override the directory of enrollment voice clips (default: `voices/`). |
| `WHOSAID_SPEAKER_DB` | Local speaker registry of named voiceprints (default: `~/.config/whosaid/speakers.json`). Private, never pushed. |
| `WHOSAID_MATCH_THRESHOLD` | Default registry/reference match threshold, overridden by `--match-threshold` (default: `0.50`). |
| `WHOSAID_ABSORB_THRESHOLD` | Default absorb-pass threshold, overridden by `--absorb-threshold` (default: `0.85`). |
| `WHOSAID_ANCHOR_THRESHOLD` | Default per-turn anchoring threshold for `--expected-speakers`, overridden by `--anchor-threshold` (default: `0.70`). |
| `DIARIZE_EMB_NAME` | Speaker-embedding model. Default is NeMo `nemo_en_titanet_small.onnx` (English-native, ~2.5× faster than ERes2Net in sherpa's benchmark). Alternatives from the same release: `3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx` (English ERes2Net) or `…_zh-cn_…` for Mandarin. Registry voiceprints are keyed by model, so switching re-enrolls speakers. |
| `WHOSAID_REC_DEVICE` | avfoundation input device used by `record` and `enroll`. |
| `WHOSAID_INSTALL_DIR` | Command install directory used by `whosaid install` (default: `~/.local/bin`). |
| `WHOSAID_ACTION_ITEMS_HOOK` | Default action-items hook for `ingest --action-items` (transcript on stdin, markdown on stdout), overridden by `--hook`. |
| `WHOSAID_WORKSPACE` | Default meeting workspace for `index`, `search`, `context`, `graph`, `wiki`, `watch`, for the index-status line in `whosaid doctor`, and for the read-only workspace tools and resources of `whosaid mcp`. |
| `WHOSAID_OWNER` | Whose action items a workspace tracks; overrides `[workspace] owner` in `whosaid.toml`. |
| `WHOSAID_OLLAMA` | Ollama base URL for embeddings and the built-in summarizer (default: `http://127.0.0.1:11434`). Localhost is the only supported destination. |
| `WHOSAID_SUMMARIZER_MODEL` | Ollama model for `--engine ollama` (default: `qwen2.5:14b`); overrides `[summarizer] model`. |
| `WHOSAID_BIN` | The `whosaid` command the watcher runs (default: the script that launched `whosaid watch`). |
| `WHOSAID_COMMITMENTS_HOOK` | Default hook for dev-commitments extraction (`ingest --commitments`), overridden per-run by `--hook`. Receives the transcript on stdin plus `WHOSAID_SPEAKERS`/`WHOSAID_ROLES`. |
| `HF_HOME` | Hugging Face cache location (where the Whisper model lands). |
| `SHERPA_DIARIZE_CACHE` | Diarization model cache location (default: `~/.cache/sherpa-diarization`). |

## Speaker identity: enrollment clips vs. the registry

whosaid has two independent ways to put a name on a `SPEAKER_NN` cluster, and knowing which one
is driving a given label matters — this was GitHub issue #1's other point of confusion.

**1. Enrollment clips (`voices/`, `WHOSAID_VOICE_REFS`).** Every `*.wav`/`*.m4a` file in the
directory is embedded *fresh on every transcribe* and passed to the diarizer as `--ref
Name=path` (the `--ref "$refname=$ref"` loop in the `whosaid` script, run before each diarize
call). Written by `whosaid enroll [Name]` (mic, or `--from FILE` to cut a clip from an existing
recording). Because a clip is re-embedded every run, it survives a `DIARIZE_EMB_NAME` change
with no extra step: the embedding is always current for whichever model is active.

**2. The registry (`~/.config/whosaid/speakers.json`, `WHOSAID_SPEAKER_DB`).** A JSON file of
`{name, model, embedding, added}` records, written once by `whosaid relabel SPEAKER_XX=Name`
(or `--save-speaker` on a transcribe) and reused forever with no re-embedding. Each entry is
keyed by the embedding `model` it was saved under, so switching `DIARIZE_EMB_NAME` silently
orphans every registry entry for the old model — they don't error, they just stop matching —
until you relabel again under the new model.

**Role tags.** A registry entry may carry an optional `"role"` string — the conventional set is
`self`, `boss`, `peer`, `report`, `external`, and free-form lowercase tags are allowed. Set one
with `whosaid relabel <base> --role Karen=boss` (repeatable) or the `roles` map on the MCP
`whosaid_relabel` tool; it is stored on the registry entry, preserved when the voiceprint is
re-saved, and rendered as `# Role: Karen = boss` header lines in `<base>.speakers.txt`, a
`Karen  [boss]` label on the speaker card, and a top-level `"roles"` key in the sidecar.
Roles carry semantics downstream: `self` marks your own voice, and a `boss`-roled speaker's
requests rank higher in action items and the [dev-commitments](#dev-commitments) corpus.

**Which one actually names a cluster.** `name_clusters()` (`lib/diarize_sherpa.py`) runs three
passes in order, and a later pass only touches a cluster the earlier ones left unnamed: (1)
registry one-best, (2) `--ref` enrollment clips, (3) absorb (folds a still-unnamed cluster into
a known voice — registry or clip — above `--absorb-threshold`). Passes 1 and 2 both gate on
`--match-threshold` (default `0.50`, env `WHOSAID_MATCH_THRESHOLD`): below it a cluster keeps
its anonymous label rather than take a low-confidence name.

**Inspecting each.** `ls voices/` lists enrollment clips; `whosaid doctor` prints the registry's
voiceprint count and path; the MCP `whosaid_list_speakers` tool lists both at once.
`<base>.diarization.json`'s `registry_matches` records which pass named (or nearly named) every
cluster, so you can tell exactly which mechanism fired — and `whosaid samples <base>` lets you
listen to a clip per cluster to confirm a label before trusting either one.

## How it works

```
 audio file
     |
     v
 MLX Whisper (Metal GPU)  ------------->  <base>.txt / .srt / .vtt / .tsv / .json
     |
     v
 sherpa-onnx diarization (CPU)  -------->  <base>.rttm
     |
     v
 cosine-match vs voices/*.wav  --------->  <base>.speakers.txt
```

Transcription and diarization run as two independent local stages that get merged at the end.
Transcription uses MLX Whisper (`mlx-community/whisper-large-v3-turbo` by default, or the full
`large-v3` with `--accurate`) on the Mac's GPU via Metal, through a hallucination-hardened decode
path: the temperature-fallback ladder stays enabled, `condition_on_previous_text` is turned off, and
a hallucination-silence threshold keeps dead air from turning into repeated-token filler. Diarization
runs on the CPU via sherpa-onnx offline diarization: pyannote's `segmentation-3.0` ONNX model finds
who's speaking when, a speaker-embedding model (NeMo TitaNet-small by default, configurable via
`DIARIZE_EMB_NAME`) embeds each turn, and clustering (optionally hinted by
`--speakers N`) groups turns into speakers. Both diarization models are small (~30 MB total), ungated
GitHub releases — no Hugging Face token required — cached locally in `~/.cache/sherpa-diarization/`.
Finally, every clip in `voices/` — plus every voiceprint in your local registry — is embedded the
same way and matched to a cluster by cosine similarity (a match at or above `--match-threshold`,
default 0.50, names the cluster);
unmatched clusters keep a `SPEAKER_00`-style label. Aside from the one-time model downloads,
everything runs in ephemeral `uv` environments, so there's no persistent Python install left behind
on your machine.

**Tell it who to expect.** When you already know the roster, `--expected-speakers Alice,Bob,Carol`
turns diarization from a guess into a lookup. Every listed name must already be a known voice (an
enrolled clip in `voices/`, or a registry entry), and clustering is then *anchored*: each turn's
voiceprint is compared to each expected person's, a turn at cosine ≥ `--anchor-threshold` (default
`0.70`) is pinned directly to that person, and only the *residual* turns — guests, strangers, and
the occasional turn that scored low — go through the ordinary count estimate and clustering. Anyone
on the list who never speaks is silently dropped, so one roster works for a standing meeting whose
attendance changes week to week. That is the difference from `--speakers N`: an exact count merges
two distinct people the moment fewer than N show up, and auto-detect over-segments and then leaves a
major speaker's averaged cluster unmatched. The threshold sits deliberately high — TitaNet-small
scores same-speaker turns at ~0.90 (p10 ~0.72) against ~0.25 for strangers (p90 ~0.45), and a false
anchor puts a real name on the wrong words, while a missed one merely falls through to clustering
where the absorb pass can still recover it. Each anchor's result (`turns`, `mean_cosine`) lands in
the sidecar under `anchors`, and each anchored cluster in `registry_matches` with `pass: "anchor"`.
Because anchoring needs per-turn voiceprints, this flag forces the chunked diarization path at any
recording length.

**Long recordings run in parallel.** For audio over ~15 minutes, diarization splits into
non-overlapping time windows that are segmented and embedded concurrently across CPU workers, then a
single global clustering pass over every turn's voiceprint recovers speakers that stay consistent
across window boundaries. Because the clustering sees all turns at once, it recovers the same
speakers as a single-pass run while finishing several times faster. Pass `--no-chunk` to force a single pass, or
`--jobs`/`--chunk-seconds` to tune it.

## Output files

| File | Contents |
|---|---|
| `<base>.txt` | Plain transcript. |
| `<base>.srt` | SubRip subtitles. |
| `<base>.vtt` | WebVTT subtitles. |
| `<base>.tsv` | Tab-separated segments with timestamps. |
| `<base>.json` | Full Whisper segment output. |
| `<base>.rttm` | Raw diarization turns, standard RTTM format. |
| `<base>.speakers.txt` | Speaker-labeled transcript: Whisper text merged with diarization turns and enrollment names; carries `# Role: NAME = ROLE` header lines when speakers have role tags. |
| `<base>.speaker-cards.txt` | One card per speaker with turn count, talk time, and representative snippets — read it to identify who each `SPEAKER_NN` is, then name them with `whosaid relabel`. |
| `<base>.diarization.json` | Cached segments + per-cluster voiceprints, so `whosaid relabel` can rename and persist speakers without re-diarizing. Also carries `registry_matches` and `source` (below). |
| `<base>.samples/` | Created on demand by `whosaid samples <base>`: one short representative WAV per speaker cluster (`SPEAKER_NN[-Name].wav`), for a quick human listen before trusting an auto-label. |

A meeting workspace (`whosaid ingest --into <ws>`) adds these at the workspace root. Underscored
files are generated and safe to delete; the dotfiles are the watcher's working state.

| File | Contents |
|---|---|
| `<ws>/YYYY-MM-DD-HHMM/` | One meeting: the source audio, `transcript.*` outputs as above, and `action-items.md` / `action-items.json` when requested. |
| `_workspace.json` | The manifest: one entry per ingested meeting with its source sha256 (what makes ingest idempotent). |
| `_INDEX.md` | Coverage index and nothing-missing audit, from `whosaid roll-up`. |
| `_ACTION-ITEMS.md`, `_action-items.json` | The deduplicated action-item corpus and its state (stable `AI-NNN` ids, statuses, occurrences). Hand-editable. |
| `_search.db` | SQLite: FTS5 over every turn, stored embeddings, and the entity graph tables. Rebuilt by `whosaid index`; delete freely. |
| `_WIKI.md` | The generated wiki, regenerated by `whosaid index` and `whosaid wiki`. |
| `whosaid.toml` | Optional per-workspace settings (owner, groups, summarizer, search, watch). The one file here you write by hand. |
| `.watch_state.json`, `.watch.lock`, `.watch_staging/`, `.watch.log` | `whosaid watch` state: recordings already handled, the overlap guard, copies staged out of the Voice Memos store, and the agent's log. |

The sidecar's three machine-readable extras, so a consumer never has to scrape stderr or shell out to
`ffprobe`:

| Sidecar key | Contents |
|---|---|
| `registry_matches` | One record per naming decision, **including near-misses**: `{"cluster": "SPEAKER_03", "name": "Alice", "similarity": 0.919, "threshold": 0.5, "matched": true, "pass": "registry"}`. `pass` is `registry`, `ref`, or `absorb`; `matched: false` means the cluster stayed `SPEAKER_NN` because `similarity < threshold`. Refreshed by `whosaid relabel --auto`, and also printed in the transcribe JSON line. |
| `source` | Recording provenance: `{"path": "/abs/path.m4a", "duration_seconds": 1834.2, "creation_time": "2026-09-14T18:02:11.000000Z"}`. `creation_time` is the container tag, or `null` when the file carries none. |
| `roles` | Optional — present only when at least one speaker carries a registry role: `{"Karen": "boss"}`. Read by downstream consumers such as the commitments extractor. |

In a meeting workspace, `ingest --commitments` additionally writes per-meeting
`commitments.md` / `commitments.json`, which `roll-up` folds into the workspace-level
`_COMMITMENTS.md` / `_commitments.json` corpus — see [Dev-commitments](#dev-commitments).

**Match confidence and the threshold.** Auto-naming only asserts a name when the cluster's cosine
similarity to a known voiceprint reaches `--match-threshold` (default `0.50`); below it the cluster
keeps its `SPEAKER_NN` label — see the `>= ref_threshold` guards in `name_clusters()`
(`lib/diarize_sherpa.py`). The default was raised from `0.40` to `0.50` because on real meeting
audio TitaNet-small produced wrong assertions in the 0.40–0.53 band, while genuine same-speaker
matches score far higher — in the end-to-end test the enrolled reference matches its cluster at
**0.986**, against **0.194** for the nearest stranger, so 0.50 sits in a wide empty gap. Use
`registry_matches` to see exactly how close every near-miss came, then lower the threshold
deliberately if a real speaker is being missed.

## Troubleshooting

- **`whosaid: command not found` after it used to work.** `whosaid install` makes
  `~/.local/bin/whosaid` a *symlink* to the checkout — it doesn't copy the implementation. If that
  checkout lived in a temp directory (`/tmp`, `/private/tmp`, `$TMPDIR`, e.g. a clone under
  `/private/tmp/whosaid-latest/`), the OS eventually purges it and the symlink starts pointing at
  nothing, so the shell reports a plain "command not found" with no hint why. `whosaid install`
  (and `./bootstrap.sh`) now refuse to install from a temp-directory checkout in the first place
  unless you pass `--force`. To fix an already-dangling install: clone/move the checkout to a
  stable location (e.g. `~/code/whosaid`) and run `whosaid install` again from there. Because the
  installed symlink itself is what's broken, `whosaid doctor` can't be run by name to confirm this
  — run `./whosaid doctor` from the fresh checkout instead; it reports a `DANGLING` install line
  when it finds this.
- **Enroll/record produces silence.** This is almost always a macOS microphone permission problem:
  go to System Settings → Privacy & Security → Microphone and grant access to your terminal app.
  macOS feeds an unauthorized app *silent zeros* instead of an error, so whosaid detects this by
  checking the captured volume rather than trusting a clean exit code.
- **Doesn't run on my Intel Mac.** MLX is Apple-Silicon-only, so whosaid requires an `arm64` Mac.
- **First run is slow.** The first `./bootstrap.sh` (or first transcribe, if you skip it) downloads
  the Whisper model (~1.5 GB) and the diarization models (~30 MB). Every run after that uses the
  local cache.
- **Long recordings degrade into repeated text.** This is Whisper's well-known
  hallucination/repetition-collapse failure mode, most likely on long or low-signal audio. It's why
  whosaid calls the `mlx-whisper` library directly (`lib/transcribe_mlx.py`) instead of the bare
  CLI: the bare CLI's single-temperature default is exactly the configuration that lets this happen,
  whereas the library call keeps the temperature-fallback ladder, disables conditioning on previous
  text, and applies a hallucination-silence threshold.
- **Speakers show up as `SPEAKER_00` / `SPEAKER_01` instead of a name.** No enrolled voice or
  registry entry matched closely enough. Read `<base>.speaker-cards.txt` to tell who each cluster is,
  then run `whosaid relabel <base> SPEAKER_01=Name` — this labels them and remembers them for next
  time. (Enrollment via `whosaid enroll <Name>` still works too.) Naming uses a cosine-similarity
  threshold (`--match-threshold`, default 0.50), so a short or noisy sample can fall just short of
  it. Check `registry_matches` in `<base>.diarization.json` for the exact similarity of every
  near-miss, then lower the threshold deliberately if a real speaker is being missed.
- **The same person shows up as two speakers (phantom split), or a known voice stays
  `UNIDENTIFIED`.** On long recordings the diarizer can split one voice across several clusters.
  A registry/enrolled voice names its single closest cluster, so the extra clusters used to stay
  unnamed. whosaid now runs an *absorb pass*: any still-unnamed cluster whose voiceprint is within
  `--absorb-threshold` (default 0.85) of a known voice is folded into that person, and the speaker
  cards merge those clusters into one card. To apply this to a transcript you already have, run
  `whosaid relabel <base> --auto` — it re-names from the registry + absorb pass with no
  re-transcription.
- **Distinct people get merged into one speaker (or the count is too low).** The speaker-embedding
  model must match the spoken language. whosaid defaults to an English-native model (NeMo
  TitaNet-small); on English audio the Mandarin-trained model cannot tell similar voices apart and
  collapses them. For
  predominantly Mandarin audio, set `DIARIZE_EMB_NAME` to the `…zh-cn…` model from the same release.
- **The speaker count looks one too high, with a cluster that has ~1 second of speech.** Forcing
  `--speakers N` too high can carve a phantom cluster out of crosstalk. Omit `--speakers` to
  auto-detect, which is usually more accurate.
- **Auto-detect reports far too many speakers on a long recording (it used to always say 20).**
  Fixed in the current version. The speaker count is now estimated by average-linkage
  agglomerative clustering of the per-turn voiceprints, cut at cosine `0.58` (env
  `WHOSAID_COUNT_THRESHOLD`; calibrated against real TitaNet-small embeddings, where
  same-speaker turns measure ~0.90 and different speakers ~0.25). The previous estimator
  compared each turn to a single earlier turn rather than to a cluster, so on any recording
  with real channel variation it kept opening new clusters until it pinned at the hard cap of
  20. The cap is now a **bound, not a target**: if the estimate lands on it, the count is
  reported as untrustworthy — a `WARNING:` line is written into `<base>.speaker-cards.txt` right
  under the count, and `count_warning` / `count_estimate` appear in `<base>.diarization.json` and
  in the JSON on stdout. If you know roughly how many people were present, pass
  `--min-speakers`/`--max-speakers`; if you know exactly, pass `--speakers N`.
  Note this estimator runs on the **chunked** path (recordings over 15 minutes, or any
  `--chunk-seconds`); shorter recordings use sherpa's own clustering, which takes an exact count
  only, so a `--min-speakers`/`--max-speakers` range there is reported as unenforced unless the
  two are equal.
- **`whosaid search --mode meaning` says Ollama is unreachable, or `index` skipped embeddings.**
  Meaning and hybrid search need Ollama with `nomic-embed-text` on `127.0.0.1:11434`
  (`brew install ollama && brew services start ollama && ollama pull nomic-embed-text`, or point
  `WHOSAID_OLLAMA` at where it listens). Nothing else breaks: `index` still builds the full-text
  tables and `search` falls back to exact mode, and `whosaid doctor` prints an Ollama line so you
  can see which state you are in. Once Ollama is up, run `whosaid index <ws>` again to embed the
  turns that were skipped.
- **`no search index at <ws>/_search.db; run: whosaid index <ws>`.** Search, context, the graph
  views, and the MCP workspace tools only read the index; none of them builds it. Run
  `whosaid index <ws>` once (and again after adding meetings, or use `ingest --index` /
  `roll-up --index` / the watcher so it stays current). The same message from `whosaid doctor`
  means `WHOSAID_WORKSPACE` points at a workspace that has not been indexed yet.
- **`whosaid watch` never sees new memos, or `watch run` exits 3 with "cannot read the source".**
  The dedicated interpreter does not have Full Disk Access yet. Open System Settings, Privacy &
  Security, Full Disk Access, and add the exact path `whosaid watch install` printed (default
  `$HOME/.local/opt/whosaid-watch/bin/whosaid-watch`), then
  `launchctl kickstart -k gui/$(id -u)/<label>` or just wait for the next interval.
  `whosaid watch status --into <ws>` shows the label and whether the agent is loaded;
  `<ws>/.watch.log` has the pass-by-pass detail. Granting your terminal FDA instead would work but
  privileges everything you run from it, which is exactly what the dedicated binary avoids.

## Testing

`./test/e2e.sh` is a fully offline smoke test: it synthesizes a two-speaker dialog with two macOS
`say` voices, builds a one-clip voice enrollment for one of them, and runs the real transcribe +
diarize + name pipeline against it end to end — then asserts the speaker-labeled transcript names
the enrolled speaker and labels the other speaker distinctly. Run `./bootstrap.sh` once first so the
models are cached locally; the test itself makes no network calls.

`./test/version_test.sh` is a fast, offline unit test that checks `whosaid version`, `--version`,
and `-V` all print a matching `whosaid X.Y.Z` line and exit 0.

`./test/install_guard_test.sh` covers the temp-directory install guard: refusal without
`--force`, success with it, `install --check-only`, and `whosaid doctor` detecting a dangling
install symlink.

`./test/enroll_from_file_test.sh` covers `whosaid enroll --from` in isolation (time-window parsing,
the 15s/silence floor, and the `--force` overwrite guard) with synthesized `say` audio — no model
download required.

`./test/samples_test.sh` covers `whosaid samples` (longest-segment picking, the `--seconds` clamp,
`--per-speaker`, naming, output format, and the `source`/`--audio` fallback) against a hand-written
sidecar and synthesized `say` audio — no model download required.

The workspace-search layer has its own offline tests, all against synthetic workspaces with
placeholder speakers and `--no-embed` so Ollama is never contacted:

- `./test/cli_test.sh` checks the launcher: every new command in `whosaid help`, usage hints and
  `--help` paths, then an end-to-end `roll-up`, `index`, `search`, `context`, `graph`, `wiki`,
  `roll-up --index`, `doctor`, and a stub `ingest --index` over a two-meeting workspace.
- `./test/search_test.sh` covers `lib/search.py`: the FTS5 build, exact queries with filters,
  `context`, `speakers`, `status`, and the no-Ollama fallback.
- `python3 test/graph_test.py` covers `lib/graph.py`: the entity tables, each view, and the wiki.
- `python3 test/action_items_test.py` covers the built-in summarizer with a fake model:
  candidate selection, quote verification, section assignment, and the evidence block.
- `./test/watch_test.sh` covers `lib/watch.py` against a temp source folder and a stub
  `whosaid`: the stable-mtime wait, staging, state, `--seed`, `--dry-run`, and the plist.
- `python3 test/mcp_descriptions_test.py` also checks the new read-only workspace tools.

`./test/roles_test.sh` covers speaker role tags offline: `--role`/`--save-role` validation, role
preservation across registry re-saves, the `# Role:` header lines in `.speakers.txt`, the
sidecar's `roles` key, and the `[role]` card label — no model download required.

`./test/commitments_test.sh` covers the dev-commitments extractor and corpus offline: cue,
negation, and question handling, `self`-role gating, boss-requested priority, the `CM-NNN`
roll-up dedupe and hand-edit reconcile, and the semantic dedupe under `WHOSAID_EMBED_FAKE=1`
versus the difflib fallback when the embed server is unreachable; no model download required.

`./test/worklist_test.sh` covers the ranked worklist offline: every tier rule, ordering, why
strings, owner resolution (`--owner`, `me`, the toml owner, aliases), the CM + AI union and its
cross-source dedupe, `_WORKLIST-<Owner>.md` as a regenerated view, `--all-owners`, `--json`, and
the `whosaid commitments` launcher command.

## License

MIT — see [LICENSE](LICENSE).
