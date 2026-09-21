# Changelog

All notable changes to whosaid are documented here. This project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) and the format of
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **`whosaid watch install --no-fda`: the no-administrator flow.** The macOS Full Disk Access
  toggle is admin-gated (it demands an administrator's password; MDM-managed Macs may hide the
  pane entirely), yet folder mode never needed FDA. `--no-fda` makes that the default: with no
  `--source` and no `[watch] source` it watches `~/Recordings` (created on install, pinned in
  the plist), and it exits 1 if the resolved source still sits under `~/Library`, suggesting
  `~/Recordings` or a Dropbox folder. Non-admin installs print a notice before the FDA
  instructions (record into a plain folder and run `--no-fda` instead), the dry run warns
  non-admins, the watcher's TCC error path points them at `--no-fda`, and the no-FDA success
  path lists the three capture options: Just Press Record for Mac into `~/Recordings`, an
  iPhone "Record Audio -> Save File" Shortcut into a Dropbox folder (Dropbox because iCloud
  Drive lives under `~/Library`), or drag-and-drop out of Voice Memos. See the new README
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

[Unreleased]: https://github.com/sblattj/whosaid/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/sblattj/whosaid/releases/tag/v1.2.0
[1.1.0]: https://github.com/sblattj/whosaid/releases/tag/v1.1.0
[1.0.2]: https://github.com/sblattj/whosaid/releases/tag/v1.0.2
[1.0.1]: https://github.com/sblattj/whosaid/releases/tag/v1.0.1
[1.0.0]: https://github.com/sblattj/whosaid/releases/tag/v1.0.0
