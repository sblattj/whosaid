# Changelog

All notable changes to whosaid are documented here. This project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) and the format of
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Opt-in AI layer for live insights (CLOUD): with `[live] ai = "claude"` in the workspace, the live follower asks Claude every 3 minutes of talk (at most 40 calls, 90 s and 1500 output tokens each) for topic labels, a short recap and next steps, and the Live page shows them ("So far (AI)"). Off by default and separate from `[summarizer]`. Answers live in `<start>.ai.json`; local labels are kept as `localLabel`; a restart or a failed call waits for new talk instead of calling again.
- `ClaudeCLI(max_tokens=...)` caps output tokens through `CLAUDE_CODE_MAX_OUTPUT_TOKENS`.
- `test/live_quality.py --judge`: Claude (`claude -p`, Sonnet) grades every topic label 1-5 from the topic's own lines and suggests a better one; about 4k tokens per call graded. `ClaudeCLI.tokens` counts the tokens of its calls.

### Changed
- Live topic labels read as topics. A label is now the topic's best phrase or term weighted by how much it reads as a thing: "the build" and capitalized product names ("PR", kept in the transcript's own spelling) score up; verbs, -ly hedges, abstractions ("ability", "Monday") and people (the local speaker registry, or "thanks Zoe") score down. On a 22-call sample a Claude judge rated 22% of labels good (4-5 of 5), up from 17%, and none are names now.

## [1.24.4] - 2026-10-30

### Fixed
- The app no longer loses Full Disk Access (and asks for Desktop access again) after every update. Builds were signed ad hoc, so macOS saw each one as a new app. `whosaid app build` now signs with a local "whosaid local signing" certificate when the login keychain has it, so the grant survives updates; `WHOSAID_SIGN_IDENTITY` overrides it.
- `app/live-shot.sh` runs the app against an empty throwaway workspace, so a screenshot run never reads ~/Desktop.

### Added
- `whosaid app sign-setup`: once per Mac, makes that certificate in the login keychain. `whosaid autorecord setup` lists it next to `whosaid app install`.

## [1.24.3] - 2026-10-30

### Fixed
- Live summaries are whole sentences: one speaker's lines are joined before sentences are picked, leading filler ("So yeah, ...") is dropped, and fragments, run-ons and sentences that stop mid-thought are passed over when a whole one exists.
- Live next steps get the same cleanup, and a short promise inside an unpunctuated run of lines ("i'll just share the link") is still found.
- Topic labels: no more pairs of words that were never said together ("Mind · Pager"), no possessives ("Zoe'S"), and more filler words kept out of labels. This also tidies the Topics library.

### Added
- `test/live_quality.py` scores labels, summaries and next steps over live sessions and a sample of workspace meetings, so a change can be measured before and after.
- `app/live-shot.sh` screenshots the Live now page for a copied session, built by the checkout's own code; `app/selftest.sh` now starts its own copy of the app (`open -n`) instead of reusing one that is already running.

## [1.24.2] - 2026-10-30

### Added
- The macOS app has an icon: two speech bubbles (yellow for me, blue for them) with a waveform, so Cmd+Tab and the Dock stop showing the blank placeholder. `app/Icon/make-icon.sh` redraws `AppIcon.icns` from `app/Icon/draw-icon.swift`.

## [1.24.1] - 2026-10-30

### Fixed

- A finished live session reopened in the app no longer shows insights from an older
  whosaid: `.insights.json` now records the release that built it (`builder`), and the
  app runs `whosaid live insights build --if-stale`, which rebuilds when the file is
  missing, older than the transcript, or from another release, and leaves it alone
  otherwise.

## [1.24.0] - 2026-10-30

### Added

- MCP `whosaid_live_insights`: the call in progress as topics (label, share, terms,
  first/last clock), rolling summaries and agreed or suggested next steps, built
  fresh from the newest live transcript.
- MCP `whosaid_live_ask`: the live transcript lines that best match a question,
  with clock and me/them, optionally within the last N minutes. No model call.

## [1.23.3] - 2026-10-30

### Changed

- whosaid.app shows in the Dock and ⌘Tab while one of its windows (library, Live
  panel, Settings) is open or minimized, and goes back to menu bar only when the
  last one closes. A window buried behind other apps is one ⌘Tab away.

## [1.23.2] - 2026-10-30

### Fixed

- `whosaid live`: the "me" lane no longer fills with made-up lines ("you",
  "the", one phrase looped dozens of times). Only your speech islands are sent
  to Whisper, not 20 s of mostly masked silence; a chunk with under 0.8 s of
  your voice is skipped; segments Whisper itself rates as guesses (average
  log-prob under -1.0, compression ratio over 2.4), known silence phrases and
  repeats of a recent line are dropped.
- `live insights`: a next step needs an action, so "that's what I'll say" or
  "let's do this" no longer count as agreed; sentences cut off mid-thought and
  duplicates are left out. Filler words (guys, whoa, soon, forgot, ...) no
  longer name topics, and a topic without a repeated phrase is named by its
  two strongest terms.
- App wordgraph lane: shows the top 6 topics, lays islands out to the lane's
  shape (a narrow lane gets one column, not a tiny grid) and refits when new
  nodes arrive, so bubbles are no longer cut off at the edge. The full-window
  wordgraph still shows everything.

## [1.23.1] - 2026-10-29

### Fixed

- `whosaid app-state` (and the app's menu bar) no longer warns while the
  watcher sits between runs. It runs on demand (WatchPaths + StartInterval), so
  loaded, not running and exit 0 is normal; only a failed last run warns.

## [1.23.0] - 2026-10-29

### Added

- `whosaid topics build [WS]`: topics across meetings into `<WS>/_topics.json`
  (the live insights clustering per meeting, merged by label or terms, stable ids,
  ranked by meetings then recency, a wordgraph per topic, no speaker names), and
  the macOS app's Topics page: the ranked list, a topic's wordgraph and the
  meetings it came up in, each opening at the transcript. The wordgraph's fit
  keeps less padding in short views.
- `whosaid autorecord install|run|setup --live`: also run `whosaid live` during
  each capture, started when the capture starts and stopped when it is saved
  (off by default; `status --json` reports it). `autorecord setup` now points at
  the macOS app. The app's Recorder page shows autorecord's state, setup, last
  saved call or error and recent log, with stop, pause and resume, and the command
  that turns the live transcript on.
- The macOS app's Live now workspace: five lanes over the newest `whosaid live`
  session (me / them transcript, topic buckets that mark a revisit, the jibbr
  balloon wordgraph, 5-minute summaries, agreed and suggested next steps), a
  full-window wordgraph, and a ⌘L floating panel with "Ask about this call". The
  wordgraph is a bundled offline page in a WKWebView that may load only its own
  files. `test/app_network_gate_test.py` checks that the app has no network API
  and the page no fetch; a Swift test checks a navigation to http is cancelled.
  `app/g7a.sh` replays a synthetic call into a throwaway folder and reports
  latency, topics, fps and CPU.
- `whosaid live insights build|follow` and `whosaid live ask`: a local engine
  for topic buckets, wordgraph nodes, summaries and next steps from a live
  transcript (`<start>.insights.json`), and questions about the call (a search,
  or Claude only with `[summarizer] engine = "claude"`).
- The macOS app's My commitments and Action items pages: the ranked worklist
  with P1 to P3 chips and why strings, every action item with its owner, an
  Open / Done / All filter, links that open the source meeting scrolled to the
  line, and suggested completions with an explicit Mark done that records the
  quote as evidence (`whosaid_set_status`). The app fixture now carries
  commitments and a later "I sent ..." chat line, so the self-test runs the
  Mark done round trip (fixture workspaces only).
- The macOS app's speaker cards: name a voice with a role from a meeting's
  Speakers tab (`whosaid_relabel`, then `whosaid index`), see the closest
  near-miss registry voice for an unnamed one, and play a short clip of it
  (`whosaid_samples`) on click only. `test/app_audio_gate_test.py` checks that
  every audio API sits in one player that only a button calls. The self-test
  names a fixture voice end to end and refuses to unless `WHOSAID_SPEAKER_DB`
  names a throwaway registry.
- The macOS app's library: Meetings and Chats lists (speakers, unnamed voices
  and action items per row), a meeting view with the transcript colored per
  speaker, a talk-time bar and Summary / Action items / Speakers tabs, ⌘K search
  across meetings with the surrounding minute, and People / Unnamed voices pages
  with each person's owned and requested items. `app/fixture.sh` now builds a
  workspace shaped like a real one (dated folders, `transcript.*` files, silent
  audio, a scraped chat, a rolled-up action-item corpus).
- The macOS app's menu bar item: one glyph per `app-state` state (idle,
  waiting, transcribing, done, warn, agent not loaded, recording with the
  elapsed time, recording with the live transcript, saving, paused) and a
  panel with Stop and save now, Pause autorecord for 1 hour / Resume, the last
  three meetings, Name N unknown voices, Search, Open whosaid and Settings. The
  SwiftBar plugin stays as the lightweight option.
- `test/app_state_fixtures.py` builds one real `app-state` snapshot per menu
  bar state from fixture logs and state files; the Swift tests read them.
- `whosaid app build|install|open|uninstall|test`: a native macOS app (SwiftUI,
  Command Line Tools only) with a menu bar item and a library window whose
  vertical sidebar holds Library, People and Recorder. It reads `whosaid mcp`
  and `whosaid app-state --watch`, shows a clear empty state with no workspace,
  an empty one or a missing one, and never records. `app/selftest.sh` and
  `app/fixture.sh` take screenshots of its own window from a made-up workspace.
- `whosaid autorecord status --json` prints one object: installed, loaded,
  running, state (waiting, recording, saving, paused, error, stopped, not-installed),
  since, elapsed, paused_until, last_saved, last_error and whether the pause and
  stop controls are available. The run loop now keeps
  `<into>/.autorecord/state.json` current; an older loop is read from its log.
- `whosaid autorecord pause [--minutes N]` / `resume` skip new calls for a while
  (the call being recorded keeps going), and `whosaid autorecord stop-now` ends
  the call being recorded and saves it while the loop keeps waiting for the
  next one. The loop polls control files and sends the capture one SIGINT.
- `whosaid record` keeps ignoring Ctrl-C after the capture ends until the
  recording is written, so a late Ctrl-C (or a stop-now) no longer kills the
  mux and leaves the audio in a temp folder.
- `whosaid live status [--json]`: the newest live session without its text.
- `whosaid tracking set-status <ws> <AI-NNN|CM-NNN> <status> --note TEXT`
  appends one curated status event (`done` is stored as `delivered`; unknown ids
  are refused), and the `whosaid_set_status` MCP tool does the same.
- `whosaid app-state [--json | --watch SECONDS]`: one read-only snapshot of
  autorecord, the watcher, the live session, the newest meeting and unnamed
  voices, with a composed `menubar.state`, for the macOS app's menu bar.

### Fixed

- `autorecord`: a Ctrl-C or stop-now that was not delivered no longer blocks
  the next one for the same capture.
- `whosaid app build` fails with a message, not a traceback, when swift cannot
  report its build folder.

## [1.22.0] - 2026-10-29

### Added

- `whosaid autorecord setup --app A | --browser --workspace WS [--owner NAME]`
  sets up unattended call recording on a new Mac in one command (#127): `whosaid
  setup --yes` (dependencies, models; `--skip-deps` skips it), `autorecord
  install`, then `watch install --into WS --source <into> --no-fda`. It stops at
  the first failed step and lists what is left to do by hand. `--owner` starts
  `WS/whosaid.toml` with `[workspace] owner` and never edits an existing one;
  `--dry-run` changes nothing.
- `whosaid live` keeps a live transcript of the call being recorded (#131). It
  follows the `record --app` / `autorecord` capture, transcribes new audio about
  every 20 s with MLX Whisper (turbo, loaded once), and writes `me:` / `them:`
  lines with times to `<into>/.live/<start>.md` and `.jsonl`. Both legs are cut
  at one shared quiet point; mic frames not clearly louder than the app (speaker
  bleed) are silenced with the post-call stereo rule; silent chunks and
  no-speech segments are skipped. It only reads the capture. `whosaid live show
  [--minutes N]` and the read-only `whosaid_live` MCP tool return the newest one.

### Fixed

- The macOS app no longer passes a `WHOSAID_*` variable set to "" on to
  whosaid (an empty `WHOSAID_SPEAKER_DB` would have made the current folder the
  voice registry), and the Unnamed voices page says so when every voice has a name.
- `whosaid autorecord run`: a stop (SIGTERM from `launchctl`, or Ctrl-C) that
  landed between starting a `record` child and registering it left that child
  waiting for calls on its own after the loop exited. The loop now interrupts it.

- `record --app --wait-for-call` retries a permission error at capture start up
  to 4 times, 2 s apart, instead of exiting at once (#129). macOS refused one
  capture the moment a call started with the grant on, and `autorecord` then
  lost the first 30 s. The log names the error domain and code; a grant that is
  really missing still exits 5.

## [1.21.0] - 2026-10-29

### Added

- `whosaid autorecord run | install | uninstall | status` records every call
  unattended (#122). `run` loops `record --app A --wait-for-call
  --until-call-ends` or `record --browser --wait-for-call` into a folder
  (default `~/Recordings`), one copy per folder, and never gives up: an app that
  is not running or a browser that is not open yet is a waiting state, a missing
  recording permission logs the fix, other failures back off to 5 minutes.
  `install` starts it at login: `--app` builds a small generic launcher app
  (`~/Applications/Whosaid Call Recorder.app`) that holds the Screen & System
  Audio Recording grant and is rebuilt only when its source changes, so a
  reconfigure keeps the grant; `--browser` needs no grant and runs from a plain
  LaunchAgent. `uninstall` saves a call in progress first.
- `record --app --wait-for-call` looks the app up again once the call starts, so
  an app that restarted during the wait (an update) does not fail the capture.
- `whosaid record --browser --wait-for-call` waits until a meeting tab (Teams
  web, Meet, Zoom web, or `--target` with `--leave-selector`) shows its in-call
  element, then records it and stops when the call ends (#121). A browser that
  is not running yet counts as waiting; Ctrl-C while waiting exits 0 with no
  file. Like `--app`, the file is named and `creation_time`-tagged for when the
  call started. It needs no Screen Recording grant, so it suits Macs without
  admin rights.

## [1.20.0] - 2026-10-29

### Added

- `whosaid record --watch-call --app A [--interval S]` streams the app's mic and speaker
  state as JSON lines (one first, then one per change), so a recorder can offer to start
  when a call begins. Given a bundle ID it needs no Screen Recording and works while the
  app is closed. It never records audio.

## [1.19.1] - 2026-10-29

### Fixed

- `whosaid record` (mic, `--app` and `--browser`) now stops cleanly on SIGTERM.
  Before, SIGTERM from a parent process killed the script and left the capture
  child running until its 3h cap, so no file was ever saved. The script now
  forwards SIGTERM to the child and finishes as it does on Ctrl-C. The `--app`
  helper also treats a second signal within 1 s as the same stop, so a parent
  that signals both the script and its process group still gets the file.

## [1.19.0] - 2026-10-29

### Added

- `whosaid record --app` can run unattended next to `whosaid watch` (#119).
  `--wait-for-call` waits until the app opens the mic, `--until-call-ends` stops
  once none of its processes (the bundle ID and its `<bundle>.*` helpers) has
  used the mic for `--idle-seconds` (default 30), and `--into DIR` saves the file
  for the watcher instead of transcribing. Detection reads Core Audio's
  per-process objects, so it needs no extra permission. For the native Microsoft
  Teams app, `--app com.microsoft.teams2` captures the call even though Teams
  plays and records through a separate helper process.
- `record --app` files carry a `creation_time` tag set to the capture start, so
  the ingested meeting folder is dated when the call began, not when it ended.
- `ingest` accepts `--me` and, when neither it nor `$WHOSAID_OWNER` is set,
  uses the workspace's `[workspace] owner`, so a watched stereo recording names
  the mic-only voice after the owner.

## [1.18.0] - 2026-10-28

### Added

- `whosaid record --browser` records a web meeting tab (Teams web, Google Meet,
  Zoom web, or any page via `--leave-selector`) over the Chrome DevTools protocol
  and stops on its own when the call ends (#118). It records inside the page, so
  it needs no Screen Recording or audio-capture permission. The file is stereo
  (left = your mic, right = the other participants) and tagged like
  `record --app`; `--no-mic` records the tab audio alone. `--into DIR` drops the
  file into a watched folder instead of transcribing. A closed tab keeps what was
  recorded. The MCP server gains `whosaid_record_browser` (start / stop / status).

## [1.17.1] - 2026-10-27

### Fixed

- Outlook scraper exports now keep their `msg_id`, so two messages with the same
  author, clock and text but different source IDs are no longer merged into one
  (#110, #117). Re-importing an export attaches the ID to an older ID-less row
  when exactly one message matches; ambiguous rows are kept for review. Image
  references and uncertain quote attribution survive the upgrade and later imports.

## [1.17.0] - 2026-10-27

### Added

- Evidence-backed status history, corrections, compound milestones, and temporal
  priority assertions in a local ledger preserved across reindexing (#107, #108,
  #111). CLI import and read views share the same reconciliation rules.
- Structured obligation kinds, attributed acceptance, pending versus accepted
  handoffs, and historical item/person/worklist filters (#109, #111).
- Stable imported-message identity, preserved provenance, conservative conversation
  aliases, and quote-aware attribution for search and recurrence scoring (#110).
- Read-only MCP priority and completion-evidence suggestion tools. Suggestions
  require review and have bounded scans and deadlines (#107, #111).

### Fixed

- **Default MCP pages stay bounded (#112).** Omitting `limit` now caps every page at 50,
  including calls with `offset`, for items, meetings, PRs, speakers and worklists.
  Explicit limits retain their requested size. Worklist replies also include
  `next_offset` when more items remain.
- Compact rows include status, kind, acceptance, and evidence availability (#112).
- Corpus, ledger, config, transcript, and diarization sidecar changes invalidate
  cached worklists and paging snapshots, including source additions and removals
  and edits that preserve file timestamps. Older caches without source
  fingerprints are marked stale.
  Historical requests cannot fall back to current cached state (#113).
- Worklist views retain delivered/partial evidence, exclude standing directives
  and inferred tasks from overdue ranking, and include accepted handoff recipients
  in all-owner queries. Summary source pointers preserve obligation origin.
- A sentence that mentions a date no longer loses its text as a quoted reply header
  ("I will send the guide on 1/2/2030 9:30 AM"). The header needs a display name of
  two or more words before the date (#110).
- Refusals with a word between "will" and "not" ("I will definitely not send it",
  "I'll surely never do that") are no longer counted as acceptance (#109).
- `snapshot_token` is never null. A failed read returns a fresh token each time, so
  pagers restart instead of mistaking it for a stable snapshot (#113).
- Items views tolerate a corpus row whose `occurrences` is a count instead of a list,
  and workspace aliases with no workspace owner no longer attach to unowned items
  (#107).

## [1.16.0] - 2026-10-25

### Added

- **`whosaid_items` pages (#112).** New `limit`, `offset` and `compact` parameters; a call
  with neither returns the first 50 items. Replies carry `total`, `omitted` and
  `next_offset`, plus `index_mtime` so a caller can detect a rebuilt index between pages
  and restart at offset 0. Items sort by numeric id (AI-1000 after AI-999). Compact rows
  hold `id`, `text`, `type`, `owner`, `status` and `occurrences`; full detail stays in
  `whosaid_item`. `whosaid_meetings`, `whosaid_prs` and `whosaid_speakers` replies also
  gain `next_offset`.
- **Cached worklist fallback (#113).** The roll-up writes `_WORKLIST-<Owner>.json` (the
  `worklist --json` payload plus the corpora's sha256 and `generated_at`) next to
  `_WORKLIST-<Owner>.md`. On a timeout, `whosaid_worklist` attaches it as `cached` with
  `stale` and `generated_at`; `use_cache=true` returns it without running the ranking.
  `workspace.py worklist` prints `phase: <name>` lines to stderr.

### Fixed

- **`whosaid_worklist` errors say what actually failed (#113).** It no longer answers every
  failure with "run whosaid roll-up ... (builds the corpora)". Errors carry a `category`:
  `missing_corpora` (checked before anything runs; the only case that recommends a
  roll-up), `timeout` (with `elapsed_s`, the `phase` the worklist was in, and a
  phase-specific hint), `exit_<code>`, `bad_json`, `no_cache`.
- **The worklist ranking loop is much faster, with byte-identical output (#113).** Synthetic
  400 commitments x 2000 action items: 19 s to 1.6 s difflib-only, 36 s to 15 s with live
  embeddings.

## [1.15.2] - 2026-10-24

### Fixed

- **`roll-up` refolds only the commitment folders that changed (#98).** A Teams delta that
  touched one `teams.speakers.txt` used to refold the whole commitments corpus (minutes of
  CPU on a few hundred folders, `already folded (skipping)` printed 0 times). Now only
  folders whose fingerprint changed, or that are new or gone, are refolded: their
  occurrences are stripped from every item (merged ones too), the fold re-reads just them,
  and items that lost or gained evidence get their cue, negation, boss priority, requester
  and speaker re-derived from what is left before hand edits are re-applied, so a strong cue
  from a removed line no longer sticks. Ids never move, new items mint from the persisted
  `next_id`, and an uncurated item left with no occurrence is dropped. On a synthetic
  300-folder workspace a one-line delta went from 24.3 s to 5.8 s with the same corpus.
  A `[commitments]` change in `whosaid.toml` (now tracked as `cues_fingerprint`), or a
  corpus written before this release, still takes the full refresh once.
- **Roll-up shows progress and logs final ids.** A long commitments fold prints
  `folded N/M commitment folder(s)` every 25 folders, an incremental run names its changed
  folders, and the full refresh prints its `+`/`=` item lines after ids are restored, so the
  log never names a provisional `CM-` id that does not exist (an item that got its old id
  back logs as kept).
- **Possible-duplicate pairs survive incremental runs.** Fold-time near misses are kept in
  `_commitments.json` (`near_misses`, per meeting), so a pair only the fold sees stays in
  the review section when other folders refold. The all-pairs review pass also skips
  pairs that cannot reach the band before running the full difflib ratio (same output).

## [1.15.1] - 2026-10-23

### Fixed

- **`whosaid record --help` (and `-h`) prints help.** It used to fall through and start a
  mic recording.
- **The `record --app` helper can no longer outlive its run.** Every exit skips the atexit
  handlers that can block with a ScreenCaptureKit connection open, startup gives up after
  20 s with no answer (`$WHOSAID_RECORD_APP_STARTUP_TIMEOUT`), stopping a dead stream waits
  at most 3 s, a signal before capture starts exits at once, and a second Ctrl-C/SIGTERM
  exits hard. A stuck helper (seen once as a `--list` that never exited) held its
  connection, and macOS then cut off every later capture from the same binary at 0.0 s
  with "application connection being interrupted".
- **Stopping runs once.** A second stop request no longer logs "stopped after ..." again or
  races another exit.
- **`--max-seconds` is exact.** The helper used to notice the limit only on its 10 s
  progress tick.

## [1.15.0] - 2026-10-21

### Added

- **Teams chat images.** The Teams chat scraper now copies inline pasted images (through a
  canvas, as PNG data URLs), and `whosaid teams ingest` saves them to
  `<folder>/teams-images/<epoch>-<n>.png` with an `[image: ...]` reference in
  `teams.speakers.txt` and an `images` list on the sidecar segment. Image-only messages are
  kept. `window.__TS_IMAGES = false` keeps the old text-only scrape.

- **`whosaid ab`: compare two settings on your own recording.** Runs the same audio twice
  (`--a "<flags>" --b "<flags>"`, plus `--env-a`/`--env-b K=V`) and writes `ab-report.md` with
  each arm's speaker count, talk shares, word agreement, the largest disagreement spans and
  name agreement. `--same-asr` reuses A's transcript for a diarization-only A/B. Each arm works on a copy
  of the voice registry, so a trial never enrolls anything. `whosaid ab --compare DIR_A DIR_B`
  diffs two finished runs. See `docs/ab-compare.md`.

- **`--fast`** transcribes with whisper large-v3-turbo (the old default).

- **Listed in the MCP registry** as `io.github.sblattj/whosaid` (`server.json`): the PyPI package
  runs as `uvx --from whosaid==<version> whosaid-mcp`. Bump `server.json`'s two versions with each
  release, then `mcp-publisher publish`.

### Changed

- **Transcription defaults to whisper large-v3** (`mlx-community/whisper-large-v3-mlx`). On the
  hard eval set turbo scored 2.4 to 4.2% WER and swung run to run (one meeting 2.6% to 17.2%);
  large-v3 held 1.4%. It is slower and the download is about 3 GB. Roll back per run with
  `--fast`, or everywhere with `WHOSAID_MODEL=mlx-community/whisper-large-v3-turbo`.
  `--accurate` still works and now equals the default. The MCP `accuracy` argument gains
  `"default"` (follows the CLI default) and `"fast"`.

### Fixed

- **A two-person recording no longer splits into 8 to 18 speakers**
  ([#104](https://github.com/sblattj/whosaid/issues/104)). When the auto count saturated at its
  cap, the plateau ladder never settled and diarization ran with k=20. It now applies a
  substantive voice count: clusters with real talk time stay, and minor clusters (under 5% of
  talk) that sound like a larger one fold into it. Stray short turns get uncounted guest labels.
  On the realistic eval meetings DER drops from 27.4% to 18.4%; meetings that never saturate
  are unchanged. Roll back with `WHOSAID_COUNT_RECOVERY=legacy`.

- **Realistic eval meetings no longer report a bogus ~72% WER.** Their turns voice fragments of
  rendered lines but were labelled with the whole line's text, so every ASR model scored the
  same wrong number. `truth.json` now carries `"text_exact": false`, and `score.py` reports no
  WER for it (or for older truth tagged `realistic`).

## [1.14.0] - 2026-10-16

### Added

- **Calibrated realistic eval meetings** ([#80](https://github.com/sblattj/whosaid/issues/80)).
  `test/diarize_eval/realistic.py` now aims turn-taking at what the diarizer outputs (overlap
  4.7% vs real 4%, gap median 0.76 s vs 0.74, 5.74 switches/min vs 5.7), fixes an overlap
  overcount, and adds remote device switches (`--device-switch`, `--enroll-switch`) so a
  speaker's voiceprint drifts like a real one. On a 4-meeting, 3.5 h roster batch calibrate.py
  misses 1 of 20 metrics (was 7 of 21); the over-split cosine is still low (0.65 vs 0.70-0.83).
  The same batch reproduces the #77 failure: v1.12.1 refuses three truth-verified splits that
  v1.13.4 names. Roster similarity is saved in `voice_sim_realistic.json`. Batches cited by
  seed before this change no longer rebuild byte for byte.

- **`runner_up_placed_alone` on placed-voice absorbs** ([#77](https://github.com/sblattj/whosaid/issues/77)).
  `registry_matches` absorb records now also carry `runner_up_placed_alone` / `_name` / `_share` /
  `_sim`: the best already-placed cluster competing on its own, with no voiceprints, so an
  enrolled voice absent from the meeting can no longer win the runner-up and hide the blend
  partner. Recorded only: names are unchanged (44 synthetic blend runs identical to v1.13.4).
  The candidate gate (placed winner sim < 0.0, share >= 0.05) fired on no row, so it did not ship.

- **Outlook mail as a source-tagged corpus.** `contrib/outlook-scraper/outlook-mail-scraper.js`
  walks the Outlook on the web message list (a folder or a search) the way the Teams scraper
  walks the chat rail and emits the same raw_by_chat shape, keyed by thread subject with the
  conversation id as `chat_id`. `whosaid outlook ingest` folds it in as one folder per thread
  per local day: `outlook.speakers.txt` plus a sidecar with `source.kind = "outlook-mail"`.
  `lib/teams_chat.py` gained a `Source` profile (`--source teams|outlook`); `[outlook.names]` /
  `[outlook.roles]` layer over the shared `[teams.*]` map, and the roll-up audit treats both
  text-only kinds alike.

- **Teams meeting transcripts (`whosaid teams ingest-transcript`).** New
  `contrib/teams-scraper/teams-transcript-scraper.js` reads the Recap page's Transcript panel.
  That panel is a virtualized Fluent UI list that mounts only about 30 rows at a time, so the
  scraper reads the list's full `items` array from its React props (no scrolling, no UI change)
  and falls back to stepping the scroller and harvesting every frame, keyed by
  `aria-posinset`, then restoring `scrollTop`. It checks the count against `aria-setsize`.
  `lib/teams_transcript.py` ingests the result into one folder per meeting, with
  offset-stamped `teams.speakers.txt` and a `teams-transcript` sidecar; re-ingest is idempotent
  on `transcript_id`. The roll-up audit treats the new kind as text-only, like `teams-chat`.
  Checked live on a 476-entry transcript (476 of 476 read, 474 turns).

- **`whosaid record --app A`: record one app's audio, plus the mic (#92).** ScreenCaptureKit
  captures only the chosen app (Zoom, a browser tab's audio, ...) with no virtual audio device,
  and the mic rides the same stream (macOS 15+), so both legs share one clock. Saved as stereo
  m4a, left = mic, right = the app, then transcribed as usual. `--no-mic` records the app alone;
  `--list-apps` lists what `--app` accepts. The Swift helper (`lib/record_app.swift`) is built on
  first use and cached by source hash. Each leg is placed by timestamp: gaps are filled with
  silence and a leg running ahead of its timestamps is trimmed, so the two can't drift apart.
  When macOS kills the capture (a display change, sleep) it restarts in place, up to 5 times;
  after that, or on a revoked permission, the audio recorded so far is kept.
- **You vs. them on `record --app` files.** The diarizer reads the two channels: the app channel
  never carries your voice, so speech heard only on the mic is yours. Mic-only runs that the mono
  diarizer merged into an app-side voice are split back out, and with `--me NAME` (or
  `$WHOSAID_OWNER`) the one mic-side cluster is named NAME and no app-side cluster can be. The
  sidecar records each cluster's side under `channels`. Untagged or mono files are unchanged.

### Changed

- **`whosaid_meetings`, `whosaid_prs` and `whosaid_speakers` page their results (#90).** A
  ~300-entry workspace made `whosaid_meetings` return ~82k characters, past the MCP client's
  tool-result limit. All three now take `limit` / `offset` and, with neither, return the first
  50 rows plus `total` / `omitted` and a paging hint (the `whosaid_worklist` pattern from #30).
  `whosaid_meetings` also sorts newest first and adds `since` / `until` (YYYY-MM-DD, inclusive),
  `query` (substring of folder or source name) and `compact` (folder, kind, created, minutes).
  On that workspace the default call drops to ~14k characters (~7k compact).

### Fixed

- **Teams chat scraper no longer harvests a stale client snapshot (#86).** A freshly opened
  Teams tab can still show the previous session's cached rail and threads, so the newest
  messages were silently missed. The scraper now waits for the rail order to stay unchanged
  for 20 s (and for the tab to be at least 30 s old) before it counts or lists chats, and after
  each thread it compares the newest harvested message with the rail row's time: a lagging
  thread is re-harvested once, and one that still lags is listed in `__ts.stale` with a
  `progress.warning`. Pill counts are taken after the readiness wait. All timings are
  `__TS_*` knobs. The README recommends a cutoff of at least the run interval plus 36 h so
  every message is covered by two delta runs (ingest dedups on epoch). The rail row's
  timestamp selector has not been checked against live Teams yet; rows without it skip the
  per-thread check.

## [1.13.4] - 2026-10-07

### Added

- **Placed-cluster runner-up on placed-voice absorb records (#77).** `runner_up_placed` /
  `runner_up_placed_name` / `runner_up_placed_share` / `runner_up_placed_sim` repeat
  `runner_up` with the clusters already named for other voices competing by their in-meeting
  centroids (the candidate's own clusters excluded), so an enrolled blend partner whose
  voiceprint misses the blend turns can still show. Recorded only: on the 26 re-run synthetic
  runs the winner was a true blend partner in 9 of 13 blend rows, but the 4 wrongly named
  blends held shares of 0.00 to 0.17 while true splits reached 0.31.

## [1.13.3] - 2026-10-07

### Added

- **Anonymous-cluster runner-up on placed-voice absorb records (#77).** `runner_up_any` /
  `runner_up_any_share` repeat `runner_up` with the meeting's other anonymous clusters also
  competing for each turn, so an unenrolled blend partner can show; `runner_up_any_sim` is the
  winning cluster's centroid similarity to the candidate. Recorded only: re-run on the 22 blend
  meetings and the earlier split runs, true splits reached a share of 1.00, because the
  anonymous winner was usually another fragment of the candidate (50 of 58 rows). Requiring the
  winner to sit far from the candidate (similarity < 0.40, share >= 0.30) caught 1 of the 3
  wrongly named blends and no split, but that rule was chosen after seeing this data.

### Fixed

- `realistic.py --blends`: the `blend<k>` tag counts the crosstalk episodes actually inserted,
  not the number asked for (an episode that would overrun the meeting end is skipped).

## [1.13.2] - 2026-10-07

### Added

- **Runner-up evidence on placed-voice absorb records (#77).** Each `absorb` record into an
  already-placed voice now carries `runner_up` / `runner_up_share` (the other known voice
  winning the most of the cluster's talk time) and `second_placed` / `second_placed_sim` (the
  next placed voice by centroid), so a blend guard can be tuned offline on real by-ear labels.
  Recorded only: on 22 synthetic blend meetings neither separated blends from real splits.
  A centroid-margin cutoff would have dropped correct names (true splits down to +0.110, one of
  92 s) and caught none of the 3 blends the current gates miss. A runner-up cutoff would have
  caught 1 of the 3, on a 0.03 gap. See docs/diarize-eval.md "Blends (v1.13.2)".
  Known: on the reporter's real replay of v1.13.1, one of 2 blends is still named (it contains
  the candidate's own voice, so the sibling margin passes it), and the sibling margin refuses a
  59 s true split.
- `test/diarize_eval/realistic.py --blends K` inserts K crosstalk episodes per meeting (two
  speakers trading short, overlapping turns), the synthetic blend case #77 asked for.
  `--blends 0` (the default) plans byte-identical meetings.

## [1.13.1] - 2026-10-07

### Fixed

- **An unenrolled speaker no longer takes the name of an enrolled speaker who sounds like them.**
  v1.13.0's talk-share gate (#77) cannot see this case: when the real speaker is not enrolled,
  every turn's best known voice is the lookalike, so the share reads near 1.0. Across the
  held-out val sets and alternate enrolled halves, v1.13.0 named 5 such strangers (9-75 s each)
  after an enrolled lookalike, and val-mixed refs-subset named-wrong time rose from 2.6% to 7.6%.
  The placed-voice absorb now also needs a sibling margin: the cluster must sit at least 0.035
  closer to the candidate's own cluster in this recording than to the candidate's voiceprint
  (`sibling_sim` on the record). A real split of one person on one channel cleared it by
  +0.042 to +0.114 on 11 of 14 true splits; all 5 strangers sat at -0.017 to +0.028. The 3 true
  splits it gives up are short cameo fragments (4-21 s), which now stay anonymous.

### Added

- `test/diarize_eval/run.py --subset-salt S` re-picks which half of each roster is enrolled in
  `refs-subset`, to harvest more unenrolled-lookalike cases from the same meetings.

## [1.13.0] - 2026-10-07

### Fixed

- **A known speaker's second cluster is no longer refused as a blend when its short turns score low (#77).**
  The placed-voice absorb gate counted turns scoring >= 0.70 individually, but per-turn TitaNet
  scores on short turns have a median near 0.5, so on 20 synthetic meetings it refused all 12
  true single-speaker splits (purity 0.00-0.30). The gate now asks whether most of the talk time
  is closest to the candidate: the seconds-weighted share of turns whose best known voice is the
  candidate and score >= 0.40 must be >= 0.60 (`talk_share`; 0.75-1.00 on those 12 splits, about
  0.5 for a half-and-half blend). Short turns are discounted by weighting, not dropped, because
  one true split (48 turns, 76 s) had no turn over 3 s. `purity` is still recorded for comparison.

### Added

- **`registry_matches` absorb records now carry the per-turn inputs behind `purity` (#77).**
  Each placed-voice absorb decision (matched or refused) gains `turns`: one entry per turn in
  time order with `start`, `end`, `score` against the candidate, the turn's `best` known voice
  and its `best_score`, so the purity gate can be tuned offline. No naming decision or
  threshold changes. Records from `relabel --auto` have no per-turn prints and so no `turns`;
  matched records carried forward by a relabel keep theirs.
- **A realistic-meeting profile for the synthetic diarization eval (#80).** `test/diarize_eval/`
  gains a channel model (a near-mic local voice plus remote voices through a codec and playback
  path, mixed to one AAC channel), a planner for 6-10 speaker, 30-120 minute meetings with real
  turn-length, overlap and switch-rate statistics, within-speaker drift, a same-accent roster
  voice pool, and `calibrate.py`, which checks a synthetic set against real-corpus statistics
  (within 20%). It reproduced the #77 refusal on synthetic audio. See `docs/diarize-eval.md`.

Meetings transcribed before this release keep their old naming; re-run
`whosaid reprocess <ws> MEETING` (or `--all`) to apply the new absorb gate.

## [1.12.1] - 2026-10-07

### Fixed

- **A nameless cluster with under 1 s of talk in total no longer counts as a speaker (#69).**
  A named speaker's very short turns ("yeah", clipped one-second turns) used to survive as
  extra `SPEAKER_NN` clusters and inflate the headcount. Their voiceprints are too noisy to
  match anyone (0.09-0.47 against the true speaker), so instead of guessing a name, such a
  cluster is now left out of `num_speakers`, the speaker cards, and the meeting's attendees.
  Its turns keep the `SPEAKER_NN` label in the transcript and RTTM, so the words stay visible
  and a hand `relabel SPEAKER_NN=Name` still works. The transcript header lists them under
  `# Uncounted`, and the sidecar records them in `uncounted`. Named clusters are always
  counted. On 32 synthetic test meetings the exact headcount went from 20 to 23 (refs) and
  20 to 22 (refs-subset), with no change in wrong names. One real guest stopped being
  counted: only 0.6 s of their 2.3 s ended up in a cluster of their own.

### Upgrading

Run `whosaid relabel <base> --auto` on an existing meeting to apply this without
re-diarizing.

## [1.12.0] - 2026-10-07

### Added

- **`whosaid relabel <base> --auto --forget NAME`** (repeatable) clears a name from the
  meeting first, then re-runs naming, so a voice wrongly named can be dropped without
  re-transcribing. The MCP `whosaid_relabel` tool takes the same `forget` list (#77).
- **`--placed-absorb-threshold`** (env `WHOSAID_PLACED_ABSORB_THRESHOLD`, default 0.70): a
  still-unnamed cluster folds into a voice that already has a cluster in this meeting at this
  lower bar, but only when more than half of its turns individually agree (the purity gate,
  recorded as `purity` in `registry_matches`). Voices not in the meeting keep
  `--absorb-threshold` (0.85). It needs per-turn voiceprints, so it applies on the chunked
  transcribe path; `relabel --auto` and `--no-chunk` fall back to 0.85 (#77).

### Fixed

- **An enrolled voice that is not in the meeting can no longer take a cluster that belongs to
  someone already named (#77).** In the registry and `--ref` passes, a claim is refused when
  the cluster scores at least as high against a voice already placed in the meeting; the
  refusal is recorded with `blocked_by` in `registry_matches`.
- **`relabel --auto` now matches against the `voices/` clips** (`enroll --from FILE`), as
  transcribe does, and drops a name it had assigned from the registry or the absorb pass
  when that voiceprint no longer exists. Names you set by hand, and names a `--ref` clip
  matched, are kept (#77).

### Upgrading

Meetings named before 1.12.0 keep their current names. To re-evaluate one with the new rules,
run `whosaid relabel <base> --auto` (add `--forget NAME` for a name you know is wrong), or
re-diarize it with `whosaid reprocess <ws> MEETING` (or `--all`) to get the purity-gated
absorb as well; `reprocess` without names or `--all` only touches meetings its detectors flag.

## [1.11.2] - 2026-10-06

### Fixed

- **A `--max-speakers` bound (or `max_speakers` in `whosaid.toml`) now caps the automatic
  speaker count instead of setting it (#75).** Before, when the primary clustering saturated
  under a bound, recovery was skipped, so every multi-person meeting came out with exactly
  the bound (e.g. 8 speakers for a 4-person call). Now, if the plateau recovery abstains, the
  substantive voice count is applied (`count_estimate.fallback.method` is
  `bounded-substantive-count`), and k stays at the bound only when that many substantive
  voices really are present. `whosaid reprocess --dry-run` flags affected meetings with a new
  `bound-pinned` finding, including sidecars written by v1.11.0 and v1.11.1, so
  `whosaid reprocess <ws>` re-diarizes them.
- **A `reprocess`/`roll-up --refold` no longer burns commitment ids (#74).** A commitments
  refresh re-mints every item before restoring old ids, and items that got no old id kept
  their high fold-time ids, so `next_id` jumped by hundreds (one 590-item corpus went from
  CM-16061 to CM-16573 for 28 new items). New items are now numbered compactly from the
  previous `next_id`, near-miss pairs follow the rename, and an item whose text changed only
  slightly after re-diarization (same meeting, similarity at least 0.85) keeps its old id.
  Ids already burned are not reclaimed.

## [1.11.1] - 2026-10-06

### Fixed

- MCP `whosaid_reprocess` now accepts `expected_speakers` and `match_threshold`, matching `whosaid reprocess --expected-speakers/--match-threshold`.

## [1.11.0] - 2026-10-06

### Added

- **`whosaid reprocess <ws>` re-diarizes older meetings without re-running ASR.** Meetings
  processed before v1.10 keep the old speaker split, and their action items and commitments
  inherit it. `reprocess` reuses each meeting's `<base>.json` (via `transcribe --reuse-asr`),
  carries `relabel --no-save` labels over to the new clusters by segment overlap, reuses the
  speaker-count flags the meeting was first diarized with unless you pass one, never re-applies
  a role for a name the registry already knows, regenerates the per-meeting action items and
  commitments (`--no-items` skips that), then folds the corpora with `roll-up --refold`. Every
  touched file is backed up to `<ws>/_reprocess-backups/<meeting>/<UTC timestamp>/` and restored
  if a step fails. On 8 synthetic meetings first processed by v1.8.2, total absolute
  speaker-count error went from 11 to 6, the two `--speakers N` meetings went from 2 found (of 5
  and of 3) to exact, ASR was skipped in 8 of 8, and a `--no-save` label, a hand-set `done`, and
  a hand retitle all survived with ids unchanged. See the README's "Upgrading to 1.11".
- **`whosaid reprocess --dry-run` is a detector for meetings an older diarizer got wrong.** It
  reads each meeting's `<base>.diarization.json` and reports `likely` (`brief-fragments`,
  `short-hinted-undercount`) or `possible` (`short-whole-file`, `ref-order`) findings, and
  changes nothing. With no `MEETING` and no `--all`, `reprocess` takes only the flagged meetings.
- **`whosaid transcribe ... --reuse-asr`** skips Whisper when `<out>/<base>.json` exists and goes
  straight to diarization. It errors if the json is missing and cannot combine with
  `--no-diarize`.
- **`whosaid roll-up --refold MEETING[=OLD_MD]`** (repeatable; implies `--action-items`, cannot
  combine with `--rebuild`) re-reads one already-folded meeting's regenerated `action-items.md`.
  Corpus ids, hand-set status, and hand titles survive while owners follow the new speaker
  labels; `OLD_MD`, the meeting's previous file, lets a hand-retitled item re-match.
- **MCP tool `whosaid_reprocess`** wraps `whosaid reprocess`; `dry_run` defaults to true.
- **The sidecar `<base>.diarization.json` records `whosaid_version`.** Sidecars from v1.11.0 on
  are current and are never flagged by the reprocess detector.

### Fixed

- **A commitments refresh no longer skips CM ids.** When a meeting's commitment sources or roles
  changed, roll-up re-folded every meeting with fresh ids and mapped them back to the stable ones,
  but left `next_id` past the throwaway ids, so each refresh burned as many ids as the corpus
  held. `next_id` now rewinds to just past the highest live id, never below its old value.

## [1.10.0] - 2026-10-06

### Added

- **Synthetic-meeting diarization eval (`test/diarize_eval/`).** It renders a pool of TTS voices
  (ElevenLabs and OpenAI; only the scripted lines in `lines.json` go to the vendors), assembles
  seeded multi-speaker meetings with exact ground truth, and runs whosaid in four modes: blind,
  `--speakers N`, all voices enrolled, and half enrolled. Each run is isolated from the real
  registry, and the scores cover headcount, DER, per-turn and short-turn accuracy, named
  right/wrong, and WER. `voice_sim.py` measures how separable the pool's voices are to whosaid's
  own embedder, and the report splits meetings with a near-identical voice pair from the rest.
  See `docs/diarize-eval.md`. Tests: `test/diarize_eval_test.py`,
  `test/diarize_count_fixtures_test.py` (real TTS-voice embeddings, counted offline).

### Fixed

- **Short fragments no longer inflate the automatic speaker count (#59).** The count estimator
  now folds each cluster made only of turns under 2 s into the nearest substantive voice when
  they score at least 0.40 cosine. A brief guest with a distinct voice is kept. The #38 fallback
  and the `--max-speakers` hint act on the folded count, and the fallback's threshold ladder
  runs only when the duration filter actually excluded turns. On 32 synthetic meetings, exact
  counts went from 9 to 17 with no hint, and from 6 to 21 with half the voices enrolled.
- **`--speakers N` and the count estimator now work on recordings under 15 minutes (#68).**
  Short audio used sherpa's whole-file clustering, which returned fewer than N speakers on 4
  of 12 meetings and never ran whosaid's estimator. It now takes the per-turn path as one
  window. `--no-chunk` keeps the old path.
- **`--ref` voices are matched best pair first, not in argument order.** A clip given earlier
  could claim a cluster that was a far better match for a later clip, so the later speaker's
  talk went out under the wrong name. #60 had already fixed this for the registry pass. On 33
  held-out synthetic meetings with every voice passed as `--ref`, the other two fixes alone
  raised wrongly named talk time from 7.9% to 19.6%. With this fix it is 4.6%.

## [1.9.0] - 2026-10-06

### Added

- **Saturated auto counts suggest `--max-speakers N` (#59).** When the automatic speaker count
  hits its cap and the duration-filtered fallback abstains, the warning now ends with
  "Try `--max-speakers N`", and the sidecar, the CLI JSON output and both MCP results
  (`whosaid_transcribe`, `whosaid_relabel`) carry `suggested_max_speakers`. N counts voices with
  at least 3 turns of 1 s or longer at the normal cut; no suggestion when the user set a max, the
  fallback fired, or N would reach the cap. Reporting only: the count itself is unchanged.
  Test: `test/max_speakers_hint_test.py`.

- **Speaker-count eval harness (#59).** `WHOSAID_DUMP_EMBEDDINGS=<dir>` saves what the count
  estimator saw (`<base>.npz`), and `test/count_eval/run_count_eval.py` replays the dumps
  against labeled headcounts (error, saturation rate, `--threshold`). The data stays local
  (`test/count_eval/data/` is gitignored). See `docs/count-eval.md`. Test: `test/count_eval_test.py`.

- **Workspace-wide speaker hints: `[diarize]` in `whosaid.toml` (#59).** `[watch]`'s
  `speakers` / `min_speakers` / `max_speakers` / `expected_speakers` only reached watcher
  ingests, so a manual `whosaid ingest` or MCP `whosaid_transcribe` into the same workspace
  still auto-counted (and saturated at 20). `[diarize]` takes the same keys and applies to
  every `ingest --into` the workspace and to MCP transcribe whose output folder is inside it.
  It is a default: any speaker flag on the command (the watcher's `[watch]` hints included)
  replaces it. A bad value makes ingest, MCP transcribe and `watch run`/`install` refuse.
  Tests: `test/workspace_speaker_args_test.py`, `test/mcp_workspace_tools_test.py`.

### Fixed

- **Teams scraper no longer skips chats hidden by a filter pill (#57).** The left-rail pills
  (Unread / Channels / Chats / Meeting chats) are sticky per client, and with "Chats" pressed
  every meeting chat was silently missing from the run. The scraper now clears any pressed pill
  before listing chats, records it on `__ts.pills`, warns in `__ts.progress.warning` when the
  chat count changes, and presses the pill again when it ends. Test:
  `test/teams_scraper_pills_test.js`.

- **MCP `whosaid_transcribe` reports a diarization failure (#56).** A diarizer crash used to
  come back as `ok: true`, `num_speakers: null` and "All speakers named." It now returns
  `ok: false`, `partial: true`, a `diarization_error` with the CLI's reason, and the plain
  transcript path. A run that left only an earlier run's speaker files on disk is caught too
  (listed in `stale_speaker_files`), so an agent never relabels last week's sidecar.
  Test: `test/mcp_workspace_tools_test.py`.

- **Roll-up dedupe no longer drops to difflib on big workspaces.** Every commitment was embedded in
  one `/api/embed` call with a 20 s timeout; around 2,000 texts that call timed out and the whole
  run silently fell back to difflib. Texts now go in batches of 128 (`EMBED_BATCH`), each with
  its own timeout; any failed batch still falls back as before. Test: `test/embed_batch_test.py`.

- **`--max-speakers` / `--min-speakers` now hold on calls under 15 minutes (#59).** Short audio
  took the whole-file path, whose clustering only takes an exact count, so a range was ignored
  with a warning (a 15 min call came back with 42 speakers under `--max-speakers 8`). A range
  now takes the chunked path whenever the audio is long enough to split; it applies to
  `[watch] max_speakers` too. Test: `test/chunk_path_test.py`.

- **A long single turn no longer kills diarization (#56).** The speaker-embedding model crashed
  (ONNX broadcast error) on a ~145 s monologue, and the worker exception dropped every speaker
  label for the meeting. Inputs longer than 30 s (`EMBED_MAX_SECONDS`) are now embedded in equal
  pieces and averaged. Test: `test/long_turn_embed_test.py`.

### Added

- **`relabel --blend` strengthens a voiceprint instead of replacing it.** A relabel used to
  overwrite the saved print with one cluster, so a print taken from a 1:1 call could match the same
  person worse in group meetings. `--blend` averages the cluster into the existing print
  (renormalized) and records `+blend:<base>` in the entry's `added` field; the role tag and the
  similarity guard are kept. Also on `transcribe --save-speaker` and as `blend` on the
  `whosaid_relabel` MCP tool. Tests: `test/local_labels_test.py`, `test/relabel_no_save_test.sh`.

### Fixed

- **Registry naming no longer depends on registry order (#60).** The first naming pass let each
  saved voice take its best free cluster in file order, so a voice listed earlier could claim a
  cluster that matched a later voice far better (0.55 vs 0.85 on a real meeting), pushing the true
  owner onto a weak leftover. Pairs are now ranked by cosine across all voices and assigned
  highest-first (same 0.50 gate, still one cluster per voice; `relabel --auto` included).
  Test: `test/registry_matches_test.py`.

## [1.8.2] - 2026-09-30

### Fixed

- **The workspace `whosaid.toml` is no longer silently ignored under Python older than 3.11**
  (#52). On a stock macOS `python3` (3.9), the launcher dropped the owner, aliases, summarizer
  model and groups. `whosaid` now resolves one interpreter: `$WHOSAID_PYTHON`, otherwise the first
  of `python3`/`python3.13`/`python3.12`/`python3.11` that is 3.11+, otherwise
  `uv python find '>=3.11'`. Every helper runs with it, and if none is found the Python-backed
  commands exit 1 with the fix. `whosaid doctor` prints the chosen interpreter.
  `lib/wsconfig.py` falls back to `tomli`, and its warning now says what is lost. The watcher's
  interpreter probe also skips Pythons older than 3.11. Tests: `test/python_resolve_test.sh`.
- **The "Possible duplicates (review)" renderers can no longer crash a roll-up** (#53). The
  reported `KeyError: 'CM-095'` was fixed at the source in 1.7.1, because `possible_duplicates`
  only emits pairs of live ids. Both `render_commitments_md` and `render_action_items_md` now
  also fall back to the other item's text, or `(text unavailable)`, instead of subscripting,
  so a stale pair can never throw away a finished fold. Regression checks in
  `test/rollup_dupes_test.py`.

### Changed

- `requires-python` is now `>=3.11` (it was `>=3.10`, but `tomllib` needs 3.11), and the 3.10
  classifier is removed.

## [1.8.1] - 2026-09-30

### Fixed

- **`whosaid teams ingest` now reads what the Teams scraper actually emits.** v1.8.0 required
  `epoch_ms`, but the scraper writes `{chat, author, ts, epoch, text}` records grouped into a
  `raw_by_chat` map, so every real export was rejected with `epoch_ms must be an integer`.
  Ingest now accepts `epoch` or `epoch_ms` (falling back to `ts`/`timestamp_iso`), the
  `raw_by_chat` map, a flat list, `{"messages": [...]}`, and NDJSON.

### Added

- `contrib/teams-scraper/teams-chat-scraper.js`, the browser scraper itself (plain page JS,
  driven over CDP or pasted into the console), with run instructions in its README.

## [1.8.0] - 2026-09-30

### Added

- **`whosaid teams ingest <export.json> --into WS [--tz ZONE] [--self NAME] [--dry-run]`**
  adds a Microsoft Teams chat export to the workspace as a source-tagged corpus (#49). It
  writes one dated folder per chat per local day, holding `teams.speakers.txt` and a
  `teams.diarization.json` sidecar with `source.kind = "teams-chat"` and each message's
  `epoch_ms`. Display names map through `[teams.names]` in `whosaid.toml`. Roles come from
  `[teams.roles]` or the voice registry, and `--self` overrides both. Re-ingesting is
  idempotent: messages are deduplicated on `epoch_ms` and merged into the existing folder.
  Tests: `test/teams_chat_test.py`.
- **A `kind` for every meeting** (`teams-chat`, `audio`, or none) in `_workspace.json`, the
  graph's `meeting` table, `whosaid graph <ws> meetings` (with a new `--kind K` filter), the
  wiki's coverage table, and the `whosaid_meetings` MCP tool (new `kind` argument).
- `contrib/teams-scraper/README.md` documents the Teams web DOM contract and the JSON hand-off
  format.

### Changed

- **Audio-less meeting folders take their `created` time from the sidecar's
  `source.creation_time`**, falling back to the folder name. It is no longer the folder name
  alone. The roll-up audit marks a Teams chat folder as text-only instead of flagging a missing
  transcript. Tests: `test/teams_manifest_test.py`.

## [1.7.1] - 2026-09-26

### Fixed

- **`whosaid roll-up` no longer crashes with `KeyError: 'CM-NNN'` after a commitment refresh.**
  When commitment sources or speaker roles change (for example after a relabel), the roll-up
  re-folds every meeting under fresh ids and then restores the stable ids. Near misses recorded
  during that fold kept the fresh ids, so rendering the "Possible duplicates (review)" section
  of `_COMMITMENTS.md` looked up an id that no longer existed. The near misses are now rewritten
  to the restored ids, and `possible_duplicates` drops any pair naming an id the corpus does not
  hold. Regression test: `test/rollup_dupes_test.py`.

## [1.7.0] - 2026-09-24

### Added

- **`whosaid doctor` reports the summarizer engine (issue #46).** Doctor now shows the
  configured `[summarizer] engine`, and for `claude` probes that the binary resolves and a
  `claude -p` call answers under the watcher's launchd-like environment. `whosaid watch install`
  names the engine in its summary and warns that transcripts go to Anthropic when it is "claude".
- **Team directives from leadership.** When leadership sets a priority, deadline, process or
  expectation for the whole team ("from now on every PR needs a linked ticket", "all of you
  finish the security training by the end of the month"), the summarizer now drafts it as the
  owner's item even when the owner is not named and never speaks. Such items get their own
  section, "Team directives from leadership (<names>)", placed before the owner's commitments.
  The roll-up type still contains "leadership", so the worklist ranks them like boss asks.
  Leadership is the `[groups]` entry named `leadership` plus every speaker the transcript
  role-tags `boss`. A team directive on a sentence that opens by naming one other participant
  ("Sam, can you ...") is that person's task and is dropped by the script, whatever the model
  says. Without leadership, the prompts and the layout are unchanged.
- **Opt-in claude engine** (`[summarizer] engine = "claude"` or `--engine claude`, issue #44).
  One isolated `claude -p` call per meeting (Opus by default, `claude_model` to change it) reads
  the whole transcript and returns the items as JSON; whosaid still decides the sections, applies
  the team-directive guards, takes each item's speaker and time from the turn holding its quote,
  flags quotes no turn holds, and appends the verified evidence. **This sends the transcript's
  text to Anthropic**, so it is never the default and `auto` never picks it; the note under the
  banner says so. The call runs with no tools, MCP servers, settings or saved session, in an
  empty temp dir, with `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`, the model-alias overrides
  and a parent Claude Code session's variables scrubbed. On failure it falls
  back to the local Ollama engine (`fallback = "ollama"`, the default, when Ollama is up) or the
  skeleton. New keys `claude_model`, `claude_timeout`, `claude_bin`, `fallback`, and the
  `WHOSAID_CLAUDE_BIN` variable. On the eval fixtures it scores F1 0.960 (precision 0.980, recall 0.941)
  in 6 calls, against 0.899 for Opus through the per-turn pipeline in 84 calls.
- A `claude-engine` eval backend and a committed `claude-engine-opus` recording, replayed by
  `test/eval_test.py` like the others.
- A `team-directives` eval fixture (owner nearly silent, two leadership speakers, 6 directives plus 1 optional)
  and a `directive` gold kind.

### Changed

- The committed eval runs (`claude-cli-opus`, `ollama-qwen3-14b`, `ollama-qwen2.5-14b`) are
  re-recorded on the #42 prompts over six fixtures: F1 0.899, 0.895 and 0.785. The
  `ollama-qwen3-14b-think` and `ollama-qwen3.8-27b` recordings were not re-recorded and are
  removed; their numbers stay in [docs/eval.md](docs/eval.md) as history.

- **`[summarizer] think`, globally or per model.** A bool applies to every model; a table is
  looked up by exact model name, then the name without its tag, then its `default` key:
  `think = { "qwen3:14b" = true, default = false }`. Drafting stats carry the resolved value.
- The eval harness takes `--think on|off`; an `on` run records as `<run>-think`.
- Three recorded eval runs for thinking models (see [docs/eval.md](docs/eval.md)):
  `qwen3:14b` with thinking off (F1 0.821, 159.8 s), the same model with thinking on (F1 0.868,
  1907.8 s), and `qwen3.8:27b` with thinking off (F1 0.844, 355.6 s).

### Fixed

- **`watch install` provisioning checks the codesign return code (issue #31).** A failing
  codesign used to be silently ignored, leaving a half-provisioned agent tree whose unsigned
  interpreter the Full Disk Access grant cannot key to. The return code is now checked, the
  captured stderr is surfaced, the half-provisioned agent tree is removed, and install aborts
  with remediation steps.
- **The Full Disk Access deep-link uses the current macOS form (issue #34).** The pane path now
  lives in one named constant,
  `x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_AllFiles`,
  shared between `watch install` and the SwiftBar plugin — mirrored, with a sync comment and a
  drift-guard test so the two copies cannot drift.
- Test hygiene: `search_test.sh` scrubs `WHOSAID_WORKSPACE` from the environment of the check
  that must not inherit it, and the `commitments` / `roles` / `workspace` test scripts carry the
  executable bit again.
- A model reply bullet written without its leading `- ` (`**Title.** context "quote"`), which
  `qwen3:14b` often does, is now parsed instead of silently dropped.
- **Thinking models no longer reason before every summarizer call.** The Ollama request now
  always carries `think` (default `false`). Before this, `qwen3` and newer reasoned on each of
  the roughly 70 small calls per meeting, about 12 times slower on the eval, and a long enough
  thought could hit `num_predict` and leave no answer. Reasoning a model leaves inline in its
  reply is stripped before parsing, and a model with no thinking mode gets an actionable hint
  when `think` is on.
- The eval's claude-cli backend no longer inherits `ANTHROPIC_MODEL` /
  `ANTHROPIC_DEFAULT_*_MODEL` or a parent Claude Code session's `CLAUDECODE` / `CLAUDE_CODE_*`
  variables, which could silently remap `--model opus`; the OAuth token and provider selectors
  still pass through. It also reads the resolved model from `modelUsage`, so every cassette
  records `resolved_model` again.

### Documentation

- **The "No admin rights?" section covers the menu bar glyph (issue #32).** SwiftBar holds its
  own admin-gated Full Disk Access grant; without it the plugin degrades gracefully and only the
  🔴 REC indicator goes missing, and plain-folder (`--no-fda`) watchers never show REC
  regardless.

## [1.6.0] - 2026-09-23

### Added

- **An offline eval for the action-item summarizer** (`test/eval/`, documented in
  [docs/eval.md](docs/eval.md)). Five synthetic meetings with hand-labeled action items and
  distractor turns are drafted by the real pipeline and scored for precision, recall, F1 and
  flag rate. Runs are recorded as cassettes and replay offline with a strict score check. The
  recorded baseline is F1 0.717 for the default `qwen2.5:14b` (precision 0.603, recall 0.884)
  against 0.921 for a Claude `opus` reference run (precision 0.891, recall 0.954); most of the
  local model's false positives are asks aimed at someone other than the owner. The reference
  backend drives the Claude Code CLI on a subscription, is opt-in developer tooling that lives
  only under `test/eval/`, and refuses to read anything but the committed synthetic fixtures.
  whosaid itself still never calls a cloud model.
- `action_items.prepare()` and `draft()` take an optional keyword-only `client=`, a model client
  with a `chat(system, user, temperature)` method. With no client, lib builds its own Ollama
  client exactly as before.
- **`[watch]` speaker hints.** `speakers`, `min_speakers`, `max_speakers` and
  `expected_speakers` in a workspace's `whosaid.toml` are passed to every recording the watcher
  ingests, so a workspace that only records 1:1 calls can pin `speakers = 2` instead of relying
  on blind auto-detect. They are re-read on every pass, echoed by `install --dry-run`, and an
  invalid value makes `run` and `install` refuse, naming the key.
- `[summarizer] num_predict` (default 2048): the maximum number of tokens in one Ollama reply.

### Fixed

- **A runaway local-model generation no longer stalls the summarizer for the full timeout.**
  Ollama requests carried no token cap, so a sampled pass that fell into a repetition loop kept
  generating until the 900-second timeout and failed the whole draft. Replies are now capped by
  `[summarizer] num_predict`, and a failure in the inferred-next-steps pass keeps the drafted
  bullets and notes the skip in the draft instead of discarding them.
- **The watcher refuses a workspace that overlaps its recordings folder.** `watch install` and
  `watch run` now stop, before writing anything, when the recordings source and the meeting
  workspace are the same folder or one is inside the other, so transcripts, corpora and
  `_search.db` can no longer land among the recordings being watched.
- **Speaker-count saturation now repairs anonymous phantom clusters (GitHub issue #38).**
  Suspicious auto counts retry stable clustering cuts on substantive turns before accepting
  the cap, then assign short turns using those voices. A distinct short-only voice prevents
  that fallback; unresolved counts remain explicitly uncertain. Exact counts and bounds
  remain enforced.
  Fresh problematic auto diarization records the physical speaker-cluster counts before and after
  the anonymous-phantom check as
  `count_before_fold` / `count_after_fold`, plus `fold_note`, in its sidecar and MCP result.
  Cached auto diarization can receive the same conservative repair with
  `whosaid relabel <base> --auto --fold-unknown`; the flag requires `--auto` and does not assign
  identities to anonymous clusters.
  Original similarity evidence is retained so repeated repairs cannot gradually merge
  distinct voices through averaged centroids. Named and transcript-only labels are protected.

## [1.5.0] - 2026-09-22

### Added

- Speaker registry export/import commands for private backups and migration, with
  validation, explicit conflict handling, atomic writes, and private file permissions.
- An explicit workspace backfill for historical speaker names and roles, including
  a dry run, conflict reporting, regenerated commitments, and refreshed workspace views.

### Fixed

- Watcher setup on Macs using Apple Command Line Tools Python: plain-folder
  `--no-fda` installs reuse the interpreter, and FDA installs select a Python that
  supports a copied interpreter.
- LaunchAgent environment handling for user-local tools and managed-network
  certificate settings, including retention across reinstalls and TLS diagnostics.
- Dependency setup accepts installed tools without requiring Homebrew and checks
  for both `ffmpeg` and `ffprobe`. It can install checksum-verified user-local uv
  and build FFmpeg from a pinned, verified source release. Homebrew installations
  require a bottle and do not silently fall back to compilation.
- SwiftBar probes the configured source, recognizes active watcher processes, and
  preserves multiword launchd states. Plugin installation handles the first-run
  directory preference and reports setup failures accurately.
- Commitment roll-up detects changed source and role inputs in previously folded
  meetings while preserving curated corpus fields.

## [1.4.0] - 2026-09-21

### Added

- **`whosaid_worklist` pagination: `tier`/`limit`/`offset`/`compact` (GitHub issue #30).**
  The full ranked worklist ran ~61k chars in one observed workspace and exceeded the MCP
  tool-result token limit. `tier="P1|P2|P3"` (case-insensitive) filters the ranked list
  first, then `offset` skips, then `limit` caps (the shell's order is preserved);
  `compact=true` keeps only id, source, text, tier, score and why per item; a call with
  neither `limit` nor `offset` now returns the top 50 items (`_WORKLIST_DEFAULT_LIMIT`)
  with `total`, `omitted` and a paging `hint` instead of the whole list.

- **`whosaid watch install --no-fda`: the no-administrator flow.** The macOS Full Disk Access
  toggle is admin-gated (it demands an administrator's password; MDM-managed Macs may hide the
  pane entirely), yet folder mode never needed FDA. `--no-fda` makes that the default: with no
  `--source` and no `[watch] source` it watches `~/Recordings` (created on install, pinned in
  the plist), and it exits 1 if the resolved source still sits under `~/Library`, suggesting
  `~/Recordings` or a Dropbox folder. Non-admin installs print a notice before the FDA
  instructions (record into a plain folder and run `--no-fda` instead), the dry run warns
  non-admins, the watcher's TCC error path points them at `--no-fda`, and the no-FDA success
  path lists the three capture options: an iPhone "Record Audio -> Save File" Shortcut into a
  Dropbox folder (Dropbox because iCloud Drive lives under `~/Library`), QuickTime Player saved
  into `~/Recordings`, or drag-and-drop out of Voice Memos. See the new README
  section "No admin rights?".

- **Action-items hooks receive `WHOSAID_ROLES` (GitHub issue #27).** The external `--hook` /
  `WHOSAID_ACTION_ITEMS_HOOK` engine now runs with `WHOSAID_ROLES` in its environment (compact
  JSON `{name: role}` from the transcript's `# Role:` headers, `{}` when none), matching the
  commitments hook. A bring-your-own summarizer can tell leadership from peers without
  re-deriving roles. Additive and backward compatible: hooks that ignore the variable are
  unchanged.

- **PyPI packaging (`pyproject.toml`).** The `lib/` modules ship as the importable `whosaid`
  package (PEP 621, hatchling; wheel renames `lib/` → `whosaid/`), with a `whosaid-mcp` console
  script that runs the MCP server (`whosaid.mcp_server:main`) and the MCP registry marker
  (`io.github.sblattj/whosaid`) in the README. Runtime deps are declared (`mlx-whisper`,
  `mcp>=2,<3`, `numpy`, `sherpa-onnx`); ffmpeg/uv remain system prerequisites from
  `whosaid setup`, and the full CLI is still installed from a checkout via `whosaid install`.
  The version is single-sourced from `lib/__init__.py` (`__version__`): `pyproject.toml` reads it
  via hatch's dynamic version and `lib/mcp_server.py` imports it, so a release bumps exactly two
  places — `lib/__init__.py` and `WHOSAID_VERSION` in the `whosaid` script (`test/version_test.sh`
  fails if they disagree). `[tool.uv] managed = false` keeps `uv run` in the checkout working
  exactly as before (ephemeral `--with` environments; no project sync, no `.venv`, no lockfile).

## [1.3.0] - 2026-09-18

### Added
- **SwiftBar menu bar plugin: `whosaid watch menubar install` (GitHub issue #24).** Ships
  `contrib/swiftbar/whosaid.10s.py` (stdlib only) and symlinks it into SwiftBar's plugin
  directory (`menubar uninstall` / `menubar status` manage it; `whosaid doctor` reports it).
  The glyph tracks the watcher — 🎙 idle, 🔴 REC, ⏳ syncing, ⚙️ ingesting, ✅ done (30 min),
  ⚠️ needs a look, 🎙✗ agent not loaded — driven ONLY by the watcher's own timestamped stage
  lines (subprocess noise like the diarizer's `chunk 8/8 done:` can never match), reading the
  last ~2 MB of `.watch.log`. The dropdown shows the stage tail, agent line, store-read probes
  (SwiftBar needs its own Full Disk Access; pane link + relaunch action on failure), and opens
  the workspace, the latest dated meeting folder (greatest `YYYY-MM-DD-HHMM` name), each
  `_WORKLIST-<Owner>.md`, and `_WIKI.md`; actions follow the log, kickstart the watcher, and
  relaunch/refresh SwiftBar. macOS notifications fire once per transition. Offline tests:
  `test/menubar_test.py` (72 assertions).

- **One commitment model (GitHub issue #23).** The CM corpus (`_commitments.json`) is now the
  single id authority for commitments from BOTH sources: the roll-up folds self-owned
  action-items bullets (`- **Owner** [Requester HH:MM:SS] text`) into the same CM-NNN items as
  the spoken clauses via the existing TextMatcher — a bullet and its matching clause become one
  item with two occurrences (each carrying `source` `transcript`|`action-items`, meeting, line,
  `t_sec`, owner, requester, requester_role, text, cue, negative) and the bullet's timestamp
  back to the audio. `whosaid graph <ws> commitments [--owner X] [--source S] [--status S]
  [--json]` inspects the merged table; the worklist ranks action-items sightings of self-owned
  items with no input change.

### Changed

- **`graph build` loads the CM corpus instead of re-parsing action-items bullets (GitHub issue
  #23).** The `commitment` table in `_search.db` is now the corpus flattened — one row per
  occurrence with the CM id, source, and status — so `graph person` (one line per item with
  occurrence count and sources), `graph item`, and `_WIKI.md` cite the same rows the worklist
  ranks (`CM-001 meeting@12:01`), and the same spoken promise no longer lives twice with no
  link between the two. Legacy/old-shape corpora load defensively; a missing corpus yields an
  empty table with one log line.

- **`_COMMITMENTS.md` / `_commitments.json` corpus.** Roll-up folds each meeting's
  `commitments.json` into stable `CM-NNN` ids (never renumbered) with the same 0.82 difflib
  dedupe and 0.10 near-miss review band as action items, grouped by status then speaker,
  `**[boss]**` marking boss-requested items; hand edits in the rendered `_COMMITMENTS.md`
  reconcile back on the next run. `_workspace.json` points at the corpus via
  `commitments_corpus` and `_INDEX.md` gains a conditional one-line open/total count.
- **Ranked personal worklist (closes the gap left by GitHub issue #13).** Roll-up writes
  `_WORKLIST-<Owner>.md` whenever a commitments corpus or owner-attributed action items exist:
  the union of the owner's `CM-NNN` commitments and the `AI-NNN` action items assigned to them
  (owner matched case-insensitively, `_`/space interchangeable, plus `[workspace] aliases`;
  an action item restating a commitment folds into it as `(also AI-NNN)`), ranked
  deterministically into `## P1` / `## P2` / `## P3` with a score and a short why per line
  (`P1 · boss · due=tomorrow · 3 meetings`), resolved items under `## Done / history`. P1 =
  boss-requested, a blocking/urgency cue, a deadline cue, or 3+ meetings; P2 = 2 meetings,
  requested by anyone, or a strong cue in the latest meeting; P3 = the rest; negated
  commitments are never P1. The owner is `--owner NAME`, else the `self`-roled speaker, else
  `[workspace] owner`; `--all-owners` writes one file per participant. The file is a
  regenerated view (never reconciled); ids never renumber; the JSON corpora stay the source of
  truth. Cue lists, boss names, weights, and the embedding threshold are overridable under
  `[commitments]` in `whosaid.toml`.
- **`whosaid commitments <ws> [--owner NAME|me] [--all-owners] [--json] [-o FILE]`.** Prints
  the worklist on demand from the corpora without a roll-up (`whosaid worklist` is an alias);
  `--json` emits `{owner, generated_from, items: [{id, source, text, status, tier, score, why,
  first_seen, last_seen, occurrences, requested_by, negative}]}`.
- **Semantic dedupe.** Commitment folding, action-item folding, and the worklist union share
  one matcher: difflib at the corpus threshold OR embedding cosine at `[commitments]
  embed_threshold` (default 0.90) when Ollama answers on a loopback `[search] ollama` URL with
  `[search] embed` on (`[search] embed_model`, one batched `/api/embed` call per run). No
  Ollama, `embed = false`, a non-loopback URL, or any failure falls back to difflib only with
  one log line; `WHOSAID_EMBED_FAKE=1` swaps in a deterministic bag-of-words embedder for tests.
- **Watcher extracts commitments.** `whosaid watch` now runs `ingest --action-items
  --commitments`, so the corpus and the worklist stay current hands-free.
- **Transcript-only labels: `whosaid relabel <base> SPEAKER_04=Alice --no-save [--note TEXT]`
  (GitHub issue #19).** Renames the cluster in the sidecar and re-renders the transcript + speaker
  cards without ever writing to the registry — for the speaker the conversation makes obvious but
  whose cluster is a poor voiceprint (a 176-turn mixed cluster would have degraded Alice's enrolled
  print). The label is stored in the sidecar under `local_labels`, so it survives `relabel --auto`
  and `whosaid samples`, and each card is marked `Alice_Example  (transcript-only label; registry
  untouched)`. The `whosaid_relabel` MCP tool gains `no_save`, `force`, and `note` parameters.

### Changed

- **MCP `whosaid_worklist(workspace, owner="me")`.** A read-only tool returning the
  `whosaid commitments --json` payload, plus worklist guidance in the server instructions and
  the `whosaid://guide` resource.
- **`_commitments.json` items carry `cue`, `negative`, and `requested_by_role`** (additive;
  older corpora load with defaults) so the worklist can rank without re-reading transcripts.
  Folding adopts the first requester and the strongest cue seen for a repeated commitment.
- **`whosaid roll-up` accepts `--owner NAME|me` and `--all-owners`** to pick the worklist owner.

- **MCP server surfaces for roles and commitments.** A `roles` param on `whosaid_relabel`,
  role reporting in `whosaid_list_speakers`, a `roles` key in the `whosaid_transcribe` result,
  and roles + dev-commitments guidance in the server instructions and the `whosaid://guide`
  resource.

### Fixed

- **Commitment fragments ("I'll do that", "I'll check") no longer become items (GitHub issue
  #22).** The heuristic extractor accepted any non-empty cue clause, so a 15-meeting workspace
  carried ten CM items of four words or fewer with nothing to act on, sitting in the worklist as
  P2/P3 rows that could never dedupe against the real item. A clause now needs at least
  `[commitments] min_words` content words (default 2; the tokens left after a stoplist of
  pronouns, determiners, particles, prepositions, auxiliaries and fillers, so "I'll bump the
  version" passes and "I'll see what I can do" does not); one is enough when the item has a
  `requested_by` or names a deadline or urgency, since the request supplies the object. Clause
  hygiene runs first: a stuttered cue collapses ("I'll I'll start investigating that while I"
  becomes "I'll start investigating that"), clauses end at subordinators (`while`, `because`,
  `if`, `when`, `unless`, `until`, `which`, `whereas`, `although`, `though`) as well as
  `and/but/or/so/then`, and a dangling trailing pronoun or preposition is trimmed. Dropped
  clauses are reported like near-miss merges: a _Dropped fragments (review)_ section in each
  meeting's `commitments.md` (mirrored as `dropped` in `commitments.json`) plus a one-line
  `dropped N fragment(s)` log. Roll-up applies the same rule at fold time, so older
  `commitments.json` files never seed fragments into the corpus (skips land under _Dropped
  fragments (review)_ in `_COMMITMENTS.md` and persist as `dropped` in `_commitments.json`);
  existing `CM` items stay untouched. `whosaid ingest --min-words N` and the `commitments`
  subcommand's `--min-words N` / `--ws DIR` override the toml; `min_words = 0` disables the
  filter.

- **Worklist cues ignored negation, treated bare nouns as blocking, and never expired
  relative deadlines (GitHub issue #21).** `cue_hit` now skips a cue that has a negator within
  the three words before it in the same clause, so "send non-urgent questions to the channel",
  "not blocking", "no rush", and "isn't critical" no longer rank P1 with `blocking=urgent`
  ("not done yet, this is urgent" still does: the comma starts a new clause); the negator list
  is the new `[commitments] negators` key. The bare nouns `prod`, `production`, `release`,
  `ship`, `customer`, `customers` leave the default blocking list in favour of phrases (`prod
  issue`, `production is down`, `release blocker`, `blocking the release`, `before we ship`,
  `customer escalation`, `customer is waiting`, …); a workspace can add the nouns back through
  `[commitments] blocking_cues`. Relative deadline cues (`today`, `tomorrow`, `this week`,
  `next week`, `by friday`, `the 14th`, `sept 3`, `9/3`, ISO dates) now resolve against the
  date of the meeting the item was last seen in (`resolve_deadline`); once that date is behind
  today the why column says `overdue=YYYY-MM-DD` instead of `due=today`, the new `overdue`
  weight (default 1) replaces `deadline`, and the item is no longer P1 on the deadline alone.
  Cues with no calendar meaning (`this sprint`, `before the demo`) still never expire.
  `WHOSAID_TODAY=YYYY-MM-DD` pins today for tests and replays.

- **Silent registry replacement on relabel (GitHub issue #19).** Relabeling or `--save-speaker`-ing
  a cluster onto a name already in the registry used to overwrite that person's saved voiceprint
  without warning, even when the new cluster barely resembled it. Replacing an existing print whose
  similarity to the new cluster is below the match threshold (default 0.50) now refuses with
  `matches 'X' current print at 0.NN; replacing it. Pass --force or use --no-save.` unless `--force`
  is passed.

- **MCP `serverInfo.version` was empty (GitHub issue #14).** `whosaid mcp` now passes
  `__version__` to the SDK 2.x server so clients see `1.2.0` instead of an empty string; SDK 1.x,
  which has no such parameter, keeps working unchanged.

- **`whosaid transcribe FILE` was parsed as two input files (GitHub issue #18).** `transcribe` is
  now an explicit alias for the default action: `whosaid transcribe meeting.m4a --speakers 7`
  behaves exactly like `whosaid meeting.m4a --speakers 7`. This also fixes the misleading errors
  `--name only works with a single input` and `file not found: transcribe`. Help and README
  document the alias.

## [1.2.0] - 2026-09-17

### Added

- **Workspace search: `whosaid index` and `whosaid search` (GitHub issue #14).** `whosaid index <ws>`
  builds `<ws>/_search.db` (`lib/search.py`): an SQLite FTS5 table over every turn of every
  `*.speakers.txt` in the workspace, plus, when Ollama answers on `127.0.0.1:11434`, a
  `nomic-embed-text` embedding per turn. `whosaid search <ws> "<query>"` returns turn-level hits
  (`[meeting @ HH:MM:SS] Speaker: snippet`) in `--mode exact` (FTS5 words, phrases, `AND`/`OR`/`NOT`,
  `prefix*`), `--mode meaning` (nearest embeddings), or `--mode hybrid` (fused ranking), with
  `--speaker`, `--meeting`, `-k`, and `--json`. `--no-embed` and a missing Ollama both degrade to
  exact search with a note; nothing fails and nothing leaves the machine. The workspace argument
  falls back to `WHOSAID_WORKSPACE`, then to a current directory that holds `_workspace.json` or
  `whosaid.toml` (`lib/wsconfig.py`).
- **`whosaid context <ws> <meeting> <HH:MM:SS>` (GitHub issue #14).** The verbatim turns around a
  moment (`--before`/`--after` seconds, `--json`), so a search hit can be read in place without
  opening the transcript.
- **Entity graph and `whosaid graph` views (GitHub issues #14 and #13).** `lib/graph.py` derives
  people, meetings, action items, timestamped commitments, and PR mentions into tables in
  `_search.db` from the manifest, the action-item corpus, per-meeting `action-items.md`, and the
  transcripts. Views: `items` (`--owner`, `--requester`, `--status`, `--type`), `item AI-NNN`
  (occurrences and the commitments behind it), `person [Name]` (the per-person view issue #13
  asked for: talk share, commitments made, asks received), `prs`, `meetings`, and `speakers`,
  each with `--json`.
- **Generated wiki: `whosaid wiki` and `<ws>/_WIKI.md` (GitHub issue #14).** A rollup of people,
  meetings, open items, and PR mentions, each item citing the `[meeting @ time]` it was committed
  at. Regenerated by `whosaid index` and `whosaid wiki [--stdout] [-o FILE]`; never hand-edited.
- **Built-in local action-item summarizer, `--engine ollama` (GitHub issue #14).**
  `lib/action_items.py` drafts a meeting's `action-items.md` with a local Ollama model
  (`qwen2.5:14b` by default, `WHOSAID_SUMMARIZER_MODEL` or `[summarizer] model` to change it) and
  no hook script. Candidate turns are picked deterministically (the workspace owner's turns, turns
  naming the owner, one pass over the rest for unnamed asks), the model reads one turn at a time
  and must quote it verbatim, and every quote is verified against its turn before it is kept. The
  file carries a DRAFT banner, sections driven by `[workspace] owner` and `[groups]`, and an
  evidence block of the exact turns. `--engine auto` (the default) uses the hook when one is
  configured, else Ollama when it is up, else the skeleton; `hook` and `none` select the old
  behaviours explicitly, and `ollama` warns and writes the skeleton when Ollama is unreachable.
- **Per-workspace `whosaid.toml` (GitHub issue #14).** Optional settings next to the transcripts:
  `[workspace] owner`/`aliases`/`tz`, ordered `[groups]`, `[summarizer] engine`/`model`/`timeout`,
  `[search] ollama`/`embed_model`/`embed`, and `[watch] source`/`stable_seconds`/
  `max_wait_seconds`/`interval_seconds`, read by `lib/wsconfig.py`; `WHOSAID_WORKSPACE`,
  `WHOSAID_OWNER`, `WHOSAID_OLLAMA`, and `WHOSAID_SUMMARIZER_MODEL` override per run.
- **Hands-free ingest: `whosaid watch` (GitHub issue #14).** `lib/watch.py` is a launchd
  LaunchAgent (`WatchPaths` on the source folder plus a `StartInterval` safety net) that ingests
  every new recording in the macOS Voice Memos store, or any `--source` folder, once its mtime has
  been stable for `stable_seconds`, then runs `roll-up --action-items` and `index`. Recordings are
  copied out of the TCC-protected store into `<ws>/.watch_staging/` first, so only one dedicated,
  ad-hoc-signed interpreter (`~/.local/opt/whosaid-watch/bin/whosaid-watch`, provisioned by
  `install`, reusable with `--interpreter`) ever needs Full Disk Access; whosaid, `uv`, `ffmpeg`,
  and the models read ordinary files. `install --seed` marks an existing library as done,
  `--dry-run` prints every step, `--offline` bakes the Hugging Face offline variables into the
  agent, `status [--json]` reports the agent, `uninstall --purge` removes interpreter, state, lock,
  and staging (never the log). State: `<ws>/.watch_state.json`, `.watch.lock`, `.watch.log`.
- **Voice Memos helpers: `whosaid memos` (GitHub issue #14).** `list` and `pull [--latest|--title]`
  read a copy of the app's database and copy recordings out; `delete "<title>"` runs the Voice
  Memos "Delete Recordings" App Intent through a one-action Shortcut ("Delete Voice Memo"), which
  is what tapping Delete in the app does, so iCloud stays consistent and the memo lands in Recently
  Deleted. `shortcut-recipe` builds that Shortcut, or prints the by-hand recipe when signing is not
  available. The app's database and `.m4a` files are never modified directly.
- **MCP workspace tools and resources (GitHub issue #14).** Nine read-only tools on `whosaid mcp`:
  `whosaid_search`, `whosaid_context`, `whosaid_items`, `whosaid_item`, `whosaid_person`,
  `whosaid_meetings`, `whosaid_prs`, `whosaid_speakers`, and `whosaid_workspace_status`, each
  taking an optional `workspace` and otherwise using `WHOSAID_WORKSPACE`; resources
  `whosaid://workspace/wiki`, `whosaid://workspace/action-items`, `whosaid://workspace/index`,
  and `whosaid://workspace/meeting/{folder}/transcript` and `.../action-items`. None of them
  rebuilds the index. The README shows the `claude mcp add ... -e WHOSAID_WORKSPACE=...` and Kiro
  `mcp.json` registrations.
- **Offline tests for all of the above (GitHub issue #14).** `test/cli_test.sh` (launcher wiring,
  help paths, and an end-to-end index/search/context/graph/wiki/roll-up/doctor/ingest run on a
  synthetic two-meeting workspace), `test/search_test.sh`, `test/graph_test.py`,
  `test/action_items_test.py` (fake model), `test/watch_test.sh` (stub `whosaid`, temp source), and
  `test/mcp_workspace_tools_test.py`; none contacts Ollama or downloads a model.
- **`--expected-speakers` registry-anchored diarization (GitHub issue #1, part 4).** `whosaid <audio> --expected-speakers Alice,Bob` (comma-separated and repeatable; also on `ingest`, and as `expected_speakers` on the `whosaid_transcribe` MCP tool) anchors clustering to the enrolled voiceprints of the people you expect: each turn at cosine ≥ `--anchor-threshold` (default `0.70`, env `WHOSAID_ANCHOR_THRESHOLD`) is pinned to that person, the residual turns are clustered into new speakers as before, and a listed person who never speaks is dropped — so a recurring team with varying attendance no longer needs an exact `--speakers N` (which merges distinct people when fewer show up) or bare auto-detect (which over-segments and then leaves a major speaker unmatched). Unknown names are fatal and list the known voices. Per-anchor results land in the sidecar under `anchors` and in `registry_matches` with `pass: "anchor"`; the flag forces the chunked diarization path at any length, since anchoring needs per-turn voiceprints.
- **Temp-directory install guard (GitHub issue #10).** `whosaid install` (and `./bootstrap.sh`,
  before its multi-minute model downloads) now refuse to install from a checkout under `/tmp`,
  `/private/tmp`, `/var/tmp`, or `$TMPDIR` unless `--force` is given, since the installed symlink
  would silently dangle once the OS cleans that directory up. `whosaid doctor` now also reports a
  dangling or foreign install symlink so a "command not found" regression points at the real cause.
- **Match-threshold flag, machine-readable match confidence, source metadata (GitHub issue #1, parts 2–3).**
  `whosaid <audio> --match-threshold F` (alias `--ref-threshold`, env `WHOSAID_MATCH_THRESHOLD`) now
  passes the registry/reference match gate through from the CLI, `ingest`, `relabel` and the MCP
  tools, and its default is raised from `0.40` to `0.50` — real meeting audio produced wrong
  assertions in the 0.40–0.53 band, while genuine matches score far higher. Clusters below the
  threshold keep their `SPEAKER_NN` label. Every naming decision, near-misses included, is written
  to `<base>.diarization.json` as `registry_matches`
  (`{cluster, name, similarity, threshold, matched, pass}`), refreshed by `relabel --auto`, and the
  sidecar also gains `source` (`path`, `duration_seconds`, `creation_time`) so no separate `ffprobe`
  is needed for per-meeting timestamps.
- **`whosaid enroll --from FILE` (GitHub issue #1, part 1).** Enroll a voice from an existing
  recording instead of the mic: `--ss`/`--t`/`--to` cut a time window (seconds or `M:SS`/`H:MM:SS`),
  the same ≥15s/non-silent check runs on the extracted clip, and `--force` allows overwriting an
  existing `voices/<Name>.wav`.
- **`whosaid samples <base>` and the identity-mechanisms doc (GitHub issue #1, nice-to-haves).**
  `whosaid samples <base> [-o DIR] [--audio FILE] [--per-speaker N] [--seconds S] [--json]` exports
  one short representative WAV per speaker cluster (the longest segment, clamped to `--seconds`,
  default 8) so you can listen and confirm an identity before trusting an auto-label or enrolling —
  replacing the by-hand `ffmpeg` cut issue #1 described. Also exposed as the `whosaid_samples` MCP
  tool. A new README section, "Speaker identity: enrollment clips vs. the registry", documents how
  `WHOSAID_VOICE_REFS` enrollment clips and the speaker registry (`relabel`/`--save-speaker`) differ
  and which one names a given cluster.
- **Meeting workspaces (GitHub issue #2).** A dated, auditable home for a recurring meeting
  series (`lib/workspace.py`): every recording transcribed into a `YYYY-MM-DD-HHMM` folder,
  per-meeting action items, and one roll-up across the whole workspace — still entirely offline.
- **`whosaid ingest` — dated, idempotent batch folders.** Transcribes a batch into folders named
  from each recording's container `creation_time` (rendered in `--tz`, mtime fallback),
  idempotent by source sha256, passing through all transcribe flags.
- **Pluggable action-items hook.** `--hook CMD` (or the `WHOSAID_ACTION_ITEMS_HOOK` environment
  variable) receives the speaker-labeled transcript on stdin plus `WHOSAID_SPEAKERS` /
  `WHOSAID_TRANSCRIPT_PATH`, and its stdout becomes the meeting's `action-items.md`. With no hook
  a skeleton is written instead, so the default stays fully offline.
- **`whosaid roll-up` — coverage index with a nothing-missing audit.** `_INDEX.md` lists one row
  per meeting (created, duration, transcribed/diarized/action-items) and flags orphan directories
  and stale manifest entries, alongside a recurring-topics section.
- **Living, deduplicated action-item corpus.** With `--action-items`, `_ACTION-ITEMS.md` folds
  every meeting's items into stable `AI-001` ids (never renumbered) with `first_seen` /
  `last_seen`, occurrence lists, and open/ongoing/resolved statuses that survive re-runs.
  Incremental and append-only by default (`--rebuild` to reset); state is plain JSON
  (`_workspace.json`, `_action-items.json`) that is safe to hand-edit.
- **`whosaid version` / `--version` (GitHub issue #9).** Prints the installed version (and, from
  a git checkout, a `git describe` suffix); also exposed as the `version` field in the MCP
  `whosaid_doctor` tool's report.

### Changed

- **`whosaid ingest` gains `--engine E` and `--index` (GitHub issue #14).** `--engine
  auto|ollama|hook|none` selects the action-item summarizer and implies `--action-items`;
  `--index` runs `whosaid roll-up <ws> --action-items` and `whosaid index <ws>` after the batch, so
  freshly ingested meetings are searchable from one command. Without `--index`, ingest now ends
  with a `next:` hint naming that roll-up command.
- **`whosaid roll-up` gains `--index` (GitHub issue #14)**, rebuilding `_search.db` and `_WIKI.md`
  right after the roll-up.
- **`whosaid doctor` reports Ollama and the workspace index (GitHub issue #14).** Two new lines:
  whether Ollama answers at `WHOSAID_OLLAMA` (default `http://127.0.0.1:11434`), with the exact-only
  fallback spelled out when it does not, and, when `WHOSAID_WORKSPACE` is set, the workspace's
  index status or the `whosaid index <ws>` fix.
- **`whosaid mcp` reads `WHOSAID_WORKSPACE` (GitHub issue #14)** as the default workspace for the
  new read-only tools and the `whosaid://workspace/...` resources. The existing tools are
  unchanged.
- **`whosaid help`** has new `WORKSPACE SEARCH` and `WATCH` sections, and `-h`/`--help` on any of
  the new subcommands prints just its section.

### Fixed

- **Phantom speaker clusters on long recordings (GitHub issues #5, #6, #7, #8, #11).** Four
  related fixes so a long meeting no longer fragments into duplicate/unidentified speakers:
  - **Absorb pass.** After the registry one-best and `--ref` passes, every still-unnamed cluster
    whose centroid cosine to a known voice (registry entry or `--ref` clip) is `>=`
    `--absorb-threshold` (default `0.85`, env `WHOSAID_ABSORB_THRESHOLD`) is folded into that
    person. A person split across several clusters is named on all of them, and the speaker cards
    now render **one card per name** with the combined turns/talk time (was one card per cluster).
  - **`--ref` no longer double-names.** The `--ref` pass only considers still-unnamed clusters and
    skips any name the registry already assigned, so an enrolled voice plus a registry entry for the
    same person can't produce two cards for them.
  - **Auto speaker-count cap guard.** When farthest-first speaker-count estimation saturates at the
    cap (20), it is re-estimated with progressively lower merge thresholds until the count drops
    below the cap, instead of handing k-means a `k` of 20 that shatters real voices. The
    over-segmentation WARN also now fires when the final count equals the cap.
- **Auto speaker-count estimation (GitHub issue #5, #6 header warning, #1 min/max speakers).**
  Replaces the farthest-first count (and its cap-retry ladder) with average-linkage
  agglomerative clustering of the per-turn voiceprints, cut at cosine `0.58` (env
  `WHOSAID_COUNT_THRESHOLD`), using the nearest-neighbour-chain algorithm so it stays O(n^2):
  1000 turns estimate in **0.009 s**. The cap of 20 is now a **bound, not a target** — an
  estimate that lands on it is treated as a failed estimate rather than a result.
  - Measured on a purpose-built 17.8-minute, 6-voice, 48-turn synthetic meeting (macOS `say`,
    65-72 embedded turns): raw `say` audio is too clean to fragment, so base and new both
    return the true 6. Re-mixed with 8 per-turn channel profiles (band-limiting, room/headset
    EQ, level offsets, pink noise) the true count is still 6, base returns 6, and the new
    estimator returns **6** — an earlier cut of 0.45, calibrated from the 0.6-0.8 same-speaker
    figure quoted in the issue, returned 5 and was corrected by measurement.
  - Real per-turn TitaNet-small similarity through this pipeline is **intra-speaker ~0.90,
    inter-speaker ~0.25** (not the 0.6-0.8 the issue assumes, which is the low tail); k=6 holds
    for any cut in `[0.55, 0.61]` on the channel-varied fixture and `[0.44, 0.61]` on the clean
    one, and 0.58 is the midpoint of the intersection.
  - **`--min-speakers N` / `--max-speakers N`** (issue #1) clamp the auto estimate on
    `whosaid`, the diarizer and the `whosaid_transcribe` MCP tool; `--max-speakers` also lowers
    the cap, `--speakers N` still forces an exact count, and `min > max` is rejected. The
    whole-file (<15 min) path passes an exact count only when `min == max` and otherwise
    reports the range as unenforced, since sherpa's `FastClustering` has no notion of a range.
  - **The count warning now reaches the artifact (issue #6).** When the count is untrustworthy
    a `# WARNING: ...` line is written into `<base>.speaker-cards.txt` directly under the count
    line, and `count_warning` plus a `count_estimate` record (`method`, `threshold`, `k`,
    `raw_k`, `cap`, `min`, `max`, `saturated`) appear in `<base>.diarization.json`, in the JSON
    on stdout, and in the MCP result.
- **Registry entries computed with a different embedding model no longer mis-match** in
  `relabel --auto`: candidate voiceprints are filtered to the sidecar's own embedding model.

### Added

- **`whosaid relabel <base> --auto` — re-apply naming with no re-diarization.** Reloads the cached
  `<base>.diarization.json`, re-runs registry matching + the absorb pass, and rewrites
  `<base>.speakers.txt` / `<base>.speaker-cards.txt` (and the sidecar's names). Picks up voices you
  enrolled after the transcript was made and merges phantom splits. Accepts the meeting-workspace
  layout (`base` = `transcript`). The MCP `whosaid_relabel` tool gains an `auto` parameter.
- **`--absorb-threshold F` transcribe/relabel flag** (env `WHOSAID_ABSORB_THRESHOLD`, default
  `0.85`) controlling the absorb pass above.

## [1.1.0] — 2026-08-18

### Added

- **MCP server (`whosaid mcp`).** A stdio MCP (Model Context Protocol) server (`lib/mcp_server.py`,
  built on the official `mcp` Python SDK's `FastMCP`) exposes whosaid to AI agents. Launched via
  `whosaid mcp`, using the same ephemeral `uv run --with "mcp[cli]"` pattern as the rest of
  whosaid — no persistent install.
- **Five tools**, all prefixed `whosaid_`: `whosaid_transcribe`, `whosaid_relabel`,
  `whosaid_list_speakers` (read-only), `whosaid_doctor` (read-only), and
  `whosaid_enroll_from_file`. Every tool shells the existing `whosaid` CLI rather than
  reimplementing the pipeline, so the MCP surface and the CLI can't drift apart.
- **`whosaid://guide` resource** — an on-demand deep reference (full flag/env list, the long-audio
  parallel path, the cosine-match threshold) an agent can read without it bloating every tool's
  always-loaded description.

## [1.0.2] — 2026-08-18

Documentation-accuracy patch — no change to the transcription or diarization
pipeline. A `/cbm-atlas` architecture audit confirmed the README already matches
the code; the drift was in the design doc, now corrected.

### Documentation

- **`docs/design.md` refreshed to the current architecture.** The default
  speaker-embedding model is corrected to NeMo TitaNet-small (it had named
  3D-Speaker ERes2Net, now listed only as an opt-in `DIARIZE_EMB_NAME`
  alternative). Added the `install` and `relabel` subcommands, the persistent
  local speaker registry, `.speaker-cards.txt`, the `.diarization.json` sidecar,
  and the parallel long-audio path so the doc matches the implementation.
- **README wording tightened.** The long-audio parallel path now says it
  "recovers the same speakers" as a single-pass run rather than "the same
  result" — the whole-file and chunked paths use different clustering algorithms,
  so bit-identical output isn't guaranteed.

### Build

- `bootstrap.sh`'s disk-space check now distinguishes the ~4 GB recommended free
  space from the ~1.5 GB Whisper model download.
- The generated `/cbm-atlas` output directory (`.cbm-atlas/`) is now gitignored.

## [1.0.1] — 2026-08-17

Documentation and test-coverage patch — no change to the transcription or
diarization pipeline.

### Documentation

- **README tuned for discoverability.** An above-the-fold summary now names the
  terms people search for — speaker diarization, Whisper speech-to-text, "who
  spoke when," offline/on-device, voice-based speaker recognition — and the
  comparison section opens with a "MacWhisper / whisperX / aTrain alternative"
  framing.

### Tests

- **Expanded `test/e2e.sh` coverage.** Asserts every output format and artifact
  from a run (`.srt`, `.vtt`, `.tsv`, `.json`, `.rttm`, the speaker-cards file,
  and the `.diarization.json` sidecar); smoke-tests `whosaid doctor`'s
  embedding-model and registry report; and exercises `--no-diarize` (plain
  transcript, no `.speakers.txt`).

## [1.0.0] — 2026-08-17

First stable release. whosaid is local, speaker-attributed transcription for
Apple Silicon: it turns an audio file into a transcript where every turn is
attributed to a person — who said what — with nothing ever leaving your Mac.

### Highlights

- **Speaker-attributed transcripts.** MLX Whisper (Metal GPU) transcribes,
  sherpa-onnx diarization (CPU) finds who spoke when, and the two are merged into
  `<base>.speakers.txt` with per-turn speaker labels and timestamps.
- **Names speakers by voice.** A one-time ~45s enrollment (`whosaid enroll`)
  teaches whosaid your voice, so your turns read as your name instead of
  `SPEAKER_00`.
- **Persistent cross-meeting speaker identity.** Identify someone once with
  `whosaid relabel` and their voiceprint is saved to a private local registry
  (`~/.config/whosaid/speakers.json`), so they are auto-named in every future
  transcript — a persistence most transcription tools don't offer.
- **Tells you how many people spoke.** The distinct-speaker count is
  auto-detected and reported up front, with per-speaker turn counts and talk
  time.
- **Speaker cards.** For each speaker, `<base>.speaker-cards.txt` holds a few
  representative snippets so you can read a couple of lines and know who each
  cluster is — then name them with `whosaid relabel`.
- **Fast on long recordings.** Audio over ~15 min is diarized in parallel time
  windows and stitched back into consistent speakers by a single global
  clustering pass over every turn's voiceprint — several times faster, with the
  same result as a single-pass run.
- **No accounts, no API keys, no Hugging Face token.** Every model comes from an
  open, ungated source and is cached locally after the first download.

### Commands

- `whosaid <audio>…` — transcribe + diarize + label one or more files.
- `whosaid enroll [Name]` — record ~45s and name a voice.
- `whosaid record [--label L]` — capture from the mic, then transcribe.
- `whosaid relabel <base> SPEAKER_02=Name …` — name clusters from the speaker
  cards and persist their voiceprints (no re-transcription).
- `whosaid doctor` — read-only environment / model / registry report.
- `whosaid setup` (`./bootstrap.sh`) — dependency check + model pre-download.
- `whosaid install` — install/update the `~/.local/bin/whosaid` command symlink.

### Notes

- **The default speaker-embedding model is NeMo TitaNet-small** (English-native,
  ~2.5× faster than ERes2Net in sherpa's benchmark). Set `DIARIZE_EMB_NAME` to
  select the ERes2Net (en) or `…zh-cn…` (Mandarin) models from the same release.
- **`--chunk-seconds` is honored at any length.** Auto-chunking still only engages
  past ~15 min, but passing an explicit `--chunk-seconds` forces the parallel
  path on shorter audio too.
- **`whosaid doctor`** reports the active speaker-embedding model and the local
  registry path + voiceprint count.
- **Hallucination-hardened transcription.** whosaid calls the `mlx-whisper`
  library directly with the temperature-fallback ladder enabled,
  `condition_on_previous_text` off, and a hallucination-silence threshold — the
  configuration that avoids Whisper's repetition-collapse on long audio.
- **Output files:** `.txt`, `.srt`, `.vtt`, `.tsv`, `.json` (transcription), plus
  `.rttm`, `.speakers.txt`, `.speaker-cards.txt`, and a `.diarization.json`
  sidecar that makes `relabel` instant.
- **Privacy by construction:** audio, text, and voice embeddings never leave the
  machine; the speaker registry lives outside the repo and is never uploaded.

### Requirements

- An Apple Silicon Mac (MLX runs on the GPU via Metal); macOS.
- `ffmpeg` and `uv` (Homebrew). Python is used only through ephemeral `uv`
  environments — no persistent install is left behind.

[Unreleased]: https://github.com/sblattj/whosaid/compare/v1.9.0...HEAD
[1.9.0]: https://github.com/sblattj/whosaid/releases/tag/v1.9.0
[1.8.2]: https://github.com/sblattj/whosaid/releases/tag/v1.8.2
[1.8.1]: https://github.com/sblattj/whosaid/releases/tag/v1.8.1
[1.8.0]: https://github.com/sblattj/whosaid/releases/tag/v1.8.0
[1.7.1]: https://github.com/sblattj/whosaid/releases/tag/v1.7.1
[1.7.0]: https://github.com/sblattj/whosaid/releases/tag/v1.7.0
[1.6.0]: https://github.com/sblattj/whosaid/releases/tag/v1.6.0
[1.5.0]: https://github.com/sblattj/whosaid/releases/tag/v1.5.0
[1.4.0]: https://github.com/sblattj/whosaid/releases/tag/v1.4.0
[1.3.0]: https://github.com/sblattj/whosaid/releases/tag/v1.3.0
[1.2.0]: https://github.com/sblattj/whosaid/releases/tag/v1.2.0
[1.1.0]: https://github.com/sblattj/whosaid/releases/tag/v1.1.0
[1.0.2]: https://github.com/sblattj/whosaid/releases/tag/v1.0.2
[1.0.1]: https://github.com/sblattj/whosaid/releases/tag/v1.0.1
[1.0.0]: https://github.com/sblattj/whosaid/releases/tag/v1.0.0
