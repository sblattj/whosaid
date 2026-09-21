# whosaid — design

Local, GPU-accelerated, speaker-attributed transcription for Apple Silicon Macs. One command turns
an audio file (or a live mic recording) into a transcript where each turn is labeled with *who said
it* — and after a one-time voice enrollment, your own turns are labeled with your name. Nothing —
audio, text, or embeddings — ever leaves the machine. No API keys, no Hugging Face token, no
gated models.

## Why this exists

- **Whisper is local and excellent, but it doesn't know who spoke.** Diarization tooling that does
  (pyannote on torch) typically needs a HF token for gated models and a heavy Python environment.
- **whosaid composes two fully-open pieces**: MLX Whisper (Apple's MLX framework — transcription on
  the Mac GPU via Metal) and sherpa-onnx offline diarization (pyannote segmentation-3.0 ONNX + a
  NeMo TitaNet-small speaker embedding by default — ungated GitHub-release models, CPU; the
  embedding is configurable via `DIARIZE_EMB_NAME`, with 3D-Speaker ERes2Net (English) and a zh-cn
  model as opt-in alternatives). Ephemeral `uv` environments mean no persistent Python install.
- **Enrollment names the clusters.** Diarization alone yields `SPEAKER_00/01`. whosaid matches each
  cluster's voice embedding against reference clips in `voices/` by cosine similarity, so
  `Alice.wav` makes the transcript read `Alice: …`.

## Components

```
whosaid              # CLI dispatcher (bash): setup | install | enroll | record | relabel | samples | doctor | version
                     #   | ingest | roll-up | index | search | context | graph | wiki | watch | memos | mcp | <audio files>
bootstrap.sh         # capability check, dependency install, model pre-download
lib/transcribe_mlx.py  # MLX Whisper runner (hallucination-hardened)
lib/diarize_sherpa.py  # diarization + voice-ref cluster naming
lib/workspace.py     # meeting workspaces: ingest (dated, idempotent folders), action-items, dev-commitments, roll-up
lib/wsconfig.py      # workspace resolution ($WHOSAID_WORKSPACE, cwd) + whosaid.toml + env overrides
lib/search.py        # _search.db: FTS5 over every turn, optional Ollama embeddings; query/context/status
lib/graph.py         # entity tables in _search.db (people, meetings, items, commitments, PRs); views; _WIKI.md
lib/action_items.py  # built-in summarizer: candidate turns -> per-turn Ollama pass -> quote verification
lib/watch.py         # launchd watcher over Voice Memos (or any folder) + `memos` helpers
lib/mcp_server.py    # MCP stdio server: transcribe/relabel/list/doctor/enroll/samples + read-only workspace tools (whosaid mcp)
voices/              # enrollment clips: <Name>.wav (16 kHz mono; contents gitignored)
recordings/          # `whosaid record` output (gitignored)
test/e2e.sh          # offline end-to-end smoke test (synthesizes a 2-voice dialog with `say`)
test/cli_test.sh     # launcher wiring + an offline index/search/graph/wiki round trip on a synthetic workspace
```

### `bootstrap.sh` (= `whosaid setup`)

Checks, in order, loud on failure: macOS + `arm64` (hard fail — MLX requires Apple Silicon),
Homebrew, `ffmpeg` and `uv` (offers `brew install` for missing ones; `--yes` skips prompts),
~4 GB free disk. Then pre-downloads everything so first use has no surprise waits: warms the `uv`
environments, fetches the default Whisper model (`mlx-community/whisper-large-v3-turbo`, ~1.5 GB)
via `huggingface_hub`, and fetches the sherpa segmentation + embedding models (~30 MB) via
`lib/diarize_sherpa.py --ensure-models-only`. Idempotent — safe to re-run. Ends by pointing at
`./whosaid enroll`.

### `whosaid enroll [Name]`

Prints the first paragraph of the Rainbow Passage (public-domain, phonetically balanced — the
standard enrollment text in speech science), records up to 45 s from the mic via
ffmpeg/avfoundation (Ctrl-C stops early; 15 s minimum enforced), then verifies the capture is not
silent: macOS denies an ungranted microphone by feeding **silent zeros, not an error**, so a mean
volume ≤ −85 dB means the terminal lacks Microphone permission and enroll fails with System
Settings instructions. A good capture is converted to 16 kHz mono WAV at `voices/<Name>.wav`.
Repeatable for any number of people; every clip in `voices/` names its cluster in future runs.

### `whosaid record [--label L]`

Foreground mic capture to `recordings/<utc-ts>[-label].m4a` (Ctrl-C to stop — ffmpeg finalizes the
m4a on SIGINT; 3 h safety cap), the same silent-capture check, then the recording feeds directly
into the transcribe flow. Deliberately interactive-only: no daemon, no launchd. (The launchd agent
in `whosaid watch` never records; it only ingests files that Voice Memos or another app already
wrote. See [Workspace search and hands-free ingest](#workspace-search-and-hands-free-ingest).)

### `whosaid <audio>…` (default command)

The pipeline, per file:

1. **Transcribe** — `uv run --with mlx-whisper` invokes `lib/transcribe_mlx.py` (never the bare
   `mlx_whisper` CLI: the CLI's single-temperature default disables Whisper's temperature-fallback
   ladder and lets long recordings collapse into one repeated token). The helper keeps the fallback
   tuple, sets `condition_on_previous_text=False`, and uses `hallucination_silence_threshold` so
   dead air doesn't spawn repeated-token filler. Writes `.txt/.srt/.vtt/.tsv/.json` per `--format`.
2. **Diarize** — `uv run --with sherpa-onnx --with numpy` invokes `lib/diarize_sherpa.py`:
   pyannote segmentation-3.0 finds speech turns, a NeMo TitaNet-small embedding (the default —
   configurable via `DIARIZE_EMB_NAME`, with 3D-Speaker ERes2Net (English) or a zh-cn model as
   opt-in alternatives) embeds them, clustering groups them (`--speakers N` hints the count), then
   every `voices/*.wav` — plus every voiceprint in the persistent local speaker registry
   (`~/.config/whosaid/speakers.json`, overridable via `WHOSAID_SPEAKER_DB`; voiceprints are keyed
   by embedding model, so switching models re-enrolls) — is embedded and matched to clusters by
   cosine similarity (≥ `--match-threshold`, default 0.50, names the cluster; below it the cluster
   keeps its `SPEAKER_NN` label, and every decision including near-misses is recorded in the
   sidecar's `registry_matches`). Writes `<base>.rttm`, merges with the Whisper
   segments into `<base>.speakers.txt` (the speaker-labeled transcript), and emits a
   `<base>.speaker-cards.txt` (one card per speaker — turn count, talk time, representative
   snippets — to tell who each `SPEAKER_NN` is) plus a `<base>.diarization.json` sidecar (cached
   segments + per-cluster voiceprints).
3. Flags: `-o/--outdir`, `-m/--model`, `--accurate` (full large-v3 instead of turbo),
   `-l/--lang`, `-f/--format`, `-n/--name`, `--speakers N`, `--no-diarize`, and the long-audio
   controls `--no-chunk`, `-j/--jobs`, `--chunk-seconds`.
   Env: `WHOSAID_MODEL`, `WHOSAID_LANG`, `WHOSAID_VOICE_REFS`, `WHOSAID_SPEAKER_DB`,
   `DIARIZE_EMB_NAME`, `WHOSAID_REC_DEVICE`, `WHOSAID_INSTALL_DIR`; the model cache locations honor
   `HF_HOME` (Whisper) and `SHERPA_DIARIZE_CACHE` (diarization).

**Long recordings run in parallel.** Audio over ~15 min (900 s) auto-chunks: the file is split into
non-overlapping time windows that are segmented and embedded concurrently across a process pool,
then a single global clustering pass over every window's voiceprints recovers speakers that stay
consistent across window boundaries — in practice the same speakers as a single-pass run, finishing
several times faster. `--no-chunk` forces a single pass; `-j/--jobs` and `--chunk-seconds` tune the
worker count and window length.

**Relabel without re-diarizing.** `whosaid relabel <base> SPEAKER_02=Jane …` reads the
`<base>.diarization.json` sidecar, rewrites `<base>.speakers.txt` and `<base>.speaker-cards.txt`
with the new names, and saves each named voiceprint to the local registry — so that person is
auto-named in future transcripts. No re-transcription, no re-diarization.

**Role tags.** A registry entry may carry an optional lowercase `"role"` (conventional set
`self`, `boss`, `peer`, `report`, `external`; free-form allowed). Set at relabel time with
`--role NAME=ROLE` (repeatable) and preserved when the voiceprint is re-saved, a role renders
as `NAME  [role]` on the speaker card, `# Role: NAME = ROLE` headers in `.speakers.txt`, and a
top-level `"roles"` map in the sidecar. `self` marks the user's own voice; boss-roled
speakers' requests rank higher downstream (dev-commitments).

## Dev-commitments

`lib/workspace.py commitments --transcript T --json-out F` (and `whosaid ingest
--commitments`) extracts, per meeting, the first-person commitments the `self`-roled speaker
made. The extractor is a stdlib heuristic — no LLM, no network: a clause starting with a
first-person cue (`i'll`, `i will`, `i plan to`, `let me`, `i owe`, …) records the clause;
negations (`i won't`/`i can't`) are kept flagged `negative`, and question clauses are skipped.
Roles gate it: with roles present only the `self` speaker's cues count; without them every
speaker's do (legacy transcripts). When the preceding turn is a different speaker asking or
directing ("can you", "please", …), the item records `requested_by`, priority `high` when
that speaker's role is `boss`. A `--hook CMD` (env `WHOSAID_COMMITMENTS_HOOK`) can replace
the heuristic, mirroring the action-items hook contract (transcript on stdin, `WHOSAID_ROLES`
added).

Per-meeting `commitments.md`/`commitments.json` fold, at roll-up, into the corpus
`_COMMITMENTS.md`/`_commitments.json` — stable `CM-NNN` ids, the same 0.82 difflib dedupe and
0.10 near-miss band as action items, grouped by status then speaker with `**[boss]**` marking
boss-requested ones, hand edits in the rendered markdown reconciled back on the next run.
`_workspace.json` points at it via `commitments_corpus`; `_INDEX.md` gains a one-line
open/total count.

**Worklist.** `_WORKLIST-<Owner>.md` (written by roll-up whenever a commitments corpus or
owner-attributed action items exist; `whosaid commitments <ws>` prints it on demand, `--json`
for scripts and the MCP `whosaid_worklist` tool) is the ranked, per-owner union of `CM-NNN`
items the owner made and `AI-NNN` items assigned to them (`OwnerMatcher`: case-insensitive,
`_`/space interchangeable, plus `[workspace] aliases` via `compile_name_re`). `Ranker` is
deterministic and config-driven (`WORKLIST_DEFAULTS` overlaid with `[commitments]`): P1 for
boss-requested (priority `high`, requester role `boss`, `[commitments] boss`, or `[groups]
leadership` when no role is recorded), a blocking/urgency cue, a deadline cue (literal list plus
`DEADLINE_DATE_RE` for "by friday", "on sept 3", ISO dates), or 3+ meetings; P2 for 2 meetings,
any requester, or a strong cue in the latest meeting; P3 otherwise; negated items never P1.
`cue_hit` skips a cue with one of `[commitments] negators` in the three words before it inside
the same clause ("non-urgent", "not blocking"); the default blocking list carries phrases, not
bare nouns (`prod issue`, not `prod`). `resolve_deadline` turns a relative cue into a date
against the item's `last_seen` meeting day (`meeting_day_of`); when that date is before
`Ranker.today` (`worklist_today()`, `WHOSAID_TODAY` or the clock) the line says
`overdue=YYYY-MM-DD`, the `overdue` weight replaces `deadline`, and the deadline no longer earns
P1.
Score is the sum of the signal weights, ordering within a tier is score, last_seen, id, and each
line keeps the corpus prefix (`- **CM-002** [open] span (2×) P1 · boss · due=tomorrow: text`)
so `parse_commitments_md` still reads it. The file is a regenerated view, never reconciled;
`--all-owners` writes one per participant. Owner resolution: `--owner NAME`, else the `self`
role in the newest meeting (`commitments.json` roles, then `# Role:` headers), else
`[workspace] owner`. Dedupe across meetings and across the two sources goes through one
`TextMatcher`: difflib at the corpus threshold OR embedding cosine at `embed_threshold` (0.90)
when Ollama answers on a loopback URL with `[search] embed` on, one batched `/api/embed` call per
run; `WHOSAID_EMBED_FAKE=1` swaps in a bag-of-words hashing embedder so tests need no model, and
any failure falls back to difflib with one log line.

### `whosaid doctor`

Re-runs the capability checks read-only and reports: arch/OS, brew/ffmpeg/uv versions, Whisper and
sherpa model cache state, enrolled voices, whether Ollama answers at `WHOSAID_OLLAMA` (with the
exact-only search fallback spelled out when it does not), the index status of `WHOSAID_WORKSPACE`
when it is set (via `lib/search.py status`, with the `whosaid index <ws>` fix when there is none),
and the avfoundation audio device list. The Ollama probe is a 1.5 s `GET /api/tags` on localhost.

### MCP server (`whosaid mcp`)

`whosaid mcp` runs a stdio MCP (Model Context Protocol) server — `lib/mcp_server.py`, built on the
official `mcp` Python SDK's `FastMCP` — so AI agents can drive whosaid directly, launched the same
ephemeral way as everything else in whosaid: `uv run --with "mcp[cli]"`, no persistent install. The
server never reimplements the pipeline — every tool **shells the existing `whosaid` CLI** as a
subprocess, so the MCP surface and the CLI can't drift apart.

Six tools, all prefixed `whosaid_`:

- **`whosaid_transcribe`** — runs the transcribe + diarize pipeline on a file and returns the output
  paths, speaker cards, and a `next_step` pointing the agent at whichever speakers still need naming.
- **`whosaid_relabel`** — maps `SPEAKER_NN` clusters to names from the cached diarization sidecar (no
  re-transcription, no re-diarization) and persists them to the speaker registry.
- **`whosaid_list_speakers`** *(read-only)* — lists enrolled voice clips and registry-known speakers.
- **`whosaid_doctor`** *(read-only)* — the same environment/model-cache readiness report as
  `whosaid doctor`, for an agent to run before attempting a transcribe that might fail.
- **`whosaid_enroll_from_file`** — enrolls a named voice from an existing audio clip (no mic).
- **`whosaid_samples`** — exports one short representative WAV per speaker cluster (the longest
  diarized segment, clamped to `seconds`) so an agent (or the human it's helping) can listen and
  confirm an identity before trusting a label.

Cross-cutting behavior — the local-only guarantee, the output-file contract, the
transcribe-then-relabel workflow — lives once in the server's `instructions`, loaded up front rather
than repeated in every tool description; deeper reference (the full flag/env list, the long-audio
parallel path, the cosine-match threshold) is a `whosaid://guide` resource an agent reads on demand.
The interactive `enroll` and `record` commands are deliberately **not** exposed as tools — both need
a live terminal and microphone access, which an MCP client doesn't have. Same guarantee as the CLI:
audio, text, and voice embeddings never leave the machine.

**Workspace tools (issue #14).** Nine more tools, all read-only, put the search layer in front of
an agent so it can answer from turns instead of reading whole transcripts: `whosaid_search`,
`whosaid_context`, `whosaid_items`, `whosaid_item`, `whosaid_person`, `whosaid_meetings`,
`whosaid_prs`, `whosaid_speakers`, and `whosaid_workspace_status`. Each takes an optional
`workspace` and otherwise uses `WHOSAID_WORKSPACE` from the server's environment; there is no
current-directory fallback, because an MCP server's cwd is whatever the client chose. Resources
(`whosaid://workspace/wiki`, `.../action-items`, `.../index`, and
`.../meeting/{folder}/transcript` and `.../meeting/{folder}/action-items`) read
`WHOSAID_WORKSPACE` only. The server never builds or rebuilds `_search.db`: writes belong to
`whosaid index`, `ingest --index`, `roll-up --index`, and the watcher, so a tool call can never
race a rebuild or spend minutes embedding.

## Workspace search and hands-free ingest

Issue #14 adds a second layer on top of meeting workspaces (`lib/workspace.py`): once transcripts
exist, make them searchable, derive who committed to what, generate a wiki, draft action items
with a local model, and remove the manual ingest step. Every piece is stdlib Python run with plain
`python3`, keeps its data inside the workspace, and talks to nothing but Ollama on `127.0.0.1`.

**Workspace search data flow.** Each meeting's `transcript.speakers.txt` is parsed into turns
(meeting folder, offset seconds, speaker, text) and written to `_search.db` as the `seg` FTS5 table
plus an `emb` row per turn when embeddings are on; `lib/graph.py` then reads the manifest
(`_workspace.json`), the deduplicated corpus (`_action-items.json`), each meeting's
`action-items.md`, and the `seg` table to fill the graph tables (people, meetings, items,
occurrences, timestamped commitments, PR mentions) in the same database, and renders `_WIKI.md`
from those tables. The summarizer feeds the front of that chain: `ingest --action-items` writes
each meeting's `action-items.md`, `roll-up --action-items` folds those into the corpus, and the
next `index` turns the corpus into graph rows and wiki sections. Everything under `_search.db` and
`_WIKI.md` is derived and rebuildable; the hand-edited sources are the transcripts, the corpus, and
`whosaid.toml`.

### `lib/wsconfig.py`

Shared resolution used by every workspace command: an explicit path wins, then `WHOSAID_WORKSPACE`,
then the current directory if it holds `_workspace.json` or `whosaid.toml`. Parses the optional
`whosaid.toml` (`[workspace] owner`/`aliases`/`tz`, ordered `[groups]`, `[summarizer] engine`/`model`/`timeout`,
`[search] ollama`/`embed_model`/`embed`, `[watch] source`/`stable_seconds`/`max_wait_seconds`/
`interval_seconds`) with `tomllib`, and applies the per-run overrides `WHOSAID_OWNER`,
`WHOSAID_OLLAMA`, and `WHOSAID_SUMMARIZER_MODEL`. Every key has a default so the file is optional.

### `lib/search.py` (`whosaid index`, `search`, `context`)

`build` walks every subfolder with a `*.speakers.txt` (dated or hand-named), parses turns, and
writes them to an FTS5 table; with `[search] embed` on and Ollama reachable it also stores a
`nomic-embed-text` vector per turn, skipping turns already embedded so re-indexing is cheap
(`--rebuild` re-embeds everything, `--no-embed` skips the pass). `query` runs `exact` (FTS5 match:
words, phrases, boolean operators, prefixes), `meaning` (embed the query, cosine over the stored
vectors), or `hybrid` (both, reciprocal-rank fusion); `exact` is the default and the other two are
opt-in, and a `meaning` or `hybrid` request with no Ollama degrades to exact with a note on stderr. Filters `--speaker` and `--meeting` are applied in SQL; every hit is
`[meeting @ HH:MM:SS] Speaker: snippet` or, with `--json`, an object with `meeting`, `t_sec`,
`t_str`, `speaker`, `text`, `score`, `source`, `file`, `line`, `snippet`. `context` returns the
turns in a window around a timestamp. `status` reports segment and embedding counts and exits
non-zero with the `run: whosaid index <ws>` hint when there is no index, which is what `doctor`
and the MCP status tool surface.

### `lib/graph.py` (`whosaid graph`, `whosaid wiki`)

Builds the entity tables from reliable signals only (manifest, corpus, per-meeting notes,
transcripts), never from a model. Views: `items` with `--owner`/`--requester`/`--status`/`--type`,
`item AI-NNN` with occurrences and the timestamped commitments behind it, `person [Name]` (talk
share, commitments made, asks received; the view issue #13 asked for), `prs`, `meetings`, and
`speakers`, all with `--json`. `wiki` renders `_WIKI.md` (people, meetings, open items, PR
mentions, each item citing `[meeting @ time]`); `whosaid index` runs `search build`, `graph build`,
and `graph wiki` in that order and stops at the first failing step.

### `lib/action_items.py` (`ingest --engine ollama`)

The built-in summarizer, called in-process by `lib/workspace.py action-items` when the engine
resolves to `ollama` (`auto` picks it when no hook is configured and Ollama answers). The design keeps the model away
from anything it could fabricate: candidate turns are chosen deterministically (every substantive
turn by the workspace owner, every turn naming the owner, plus one model pass over the remainder
for unnamed asks); the model then sees one turn at a time (long turns in sentence groups) and must
answer `SKIP` or bullets that each carry a short verbatim quote; each quote is verified against its
turn before it is kept, and a mismatch is flagged in the output rather than silently dropped.
Sections come from `[workspace] owner` and `[groups]`; the file opens with a DRAFT banner and ends
with an evidence block of the exact turns. Any failure falls back to the skeleton with a WARN so
ingest never fails on the summarizer. Default model `qwen2.5:14b`; `qwen2.5:7b` is about twice as
fast and about twice as noisy.

### `lib/watch.py` (`whosaid watch`, `whosaid memos`)

A launchd LaunchAgent (`~/Library/LaunchAgents/com.whosaid.watch.<8 hex of the workspace path>.plist`,
`WatchPaths` on the source folder plus a `StartInterval`) that runs `watch run --into <ws>`. A pass
lists audio files in the source (the macOS Voice Memos store by default, any `--source` folder
otherwise), skips those recorded in `<ws>/.watch_state.json` (keyed name:size), waits until a
file's mtime has been stable for `stable_seconds` (bounded by `max_wait_seconds`, then rescans),
copies it into `<ws>/.watch_staging/`, and runs `whosaid ingest <copy> --into <ws> --folder-by
created --action-items --commitments`, then `roll-up --action-items` (which also refreshes the
worklist) and `index`. A `.watch.lock` prevents
overlapping passes; stdout/stderr go to `<ws>/.watch.log`. Exit codes: 0 ok, 1 error, 2
usage/verification, 3 cannot read the source (Full Disk Access missing).

The store is TCC-protected and macOS grants Full Disk Access per executable path, so `install`
provisions one dedicated interpreter (`python -m venv --copies`, binary renamed to
`~/.local/opt/whosaid-watch/bin/whosaid-watch`, ad-hoc codesigned so the grant survives) and points
the agent at it; because the watcher copies each recording out of the store before calling
`whosaid`, nothing else in the pipeline needs the grant. `--interpreter` reuses an already-granted
binary, `--seed` marks the existing library as done, `--offline` bakes `HF_HUB_OFFLINE` and friends
into the plist, `--env K=V` adds more, `uninstall --purge` removes the interpreter, state, lock, and
staging but keeps the log. `memos list`/`pull` read a copy of `CloudRecordings.db` read-only;
`memos delete` runs the app's own "Delete Recordings" App Intent through a one-action Shortcut
("Delete Voice Memo", built by `memos shortcut-recipe` or by hand when signing is unavailable) so
iCloud sync and the app's database stay consistent. The database and the `.m4a` files are never
modified directly.

Folder mode (a `--source` outside `~/Library`) needs no FDA at all. Because the FDA toggle is
admin-gated on macOS (it demands an administrator's password; MDM fleets may hide the pane
entirely), `install --no-fda` defaults an unset source to `~/Recordings` (created on install,
pinned in the plist) for non-admin clients and refuses a source still under `~/Library`;
non-admins deliver recordings via an iPhone Shortcut into a Dropbox folder, QuickTime Player
saved into the folder, or drag-and-drop out of Voice Memos instead of watching the
TCC-protected store (iCloud-syncing capture apps land files under ~/Library and so cannot
feed the no-FDA watcher).

## Error handling

- Diarization failure is **never fatal to the transcript** — warn and keep the Whisper output.
- Exit codes are **artifact-verified**: the transcribe flow exits non-zero unless the files a
  consumer would read actually exist and are non-empty (never trusts a step's own report).
- Every environment guard fails loud with the fix in the message (brew install line, System
  Settings path for mic permission, Apple-Silicon requirement).

## Testing

`test/e2e.sh` is fully offline and self-contained: it synthesizes a two-speaker dialog with two
macOS `say` voices, builds an enrollment clip for one of them ("Alice") into a temp voices dir
(`WHOSAID_VOICE_REFS` override — the repo's `voices/` is never touched), runs `./whosaid` on the
dialog with `--speakers 2`, and asserts the `.speakers.txt` exists, contains `Alice:` plus at least
one other `SPEAKER_NN` label, and has a plausible number of turns. Also runs `bash -n` /
`python3 -m py_compile` syntax checks over the sources.

The workspace layer is covered offline, with placeholder speakers and no Ollama: `test/cli_test.sh`
(launcher wiring, help paths, then roll-up, `index --no-embed`, search, context, every graph view,
wiki, `roll-up --index`, `doctor`, and a stub `ingest --index` on a synthetic two-meeting
workspace), `test/search_test.sh`, `test/graph_test.py`, `test/action_items_test.py` (fake model
server), `test/watch_test.sh` (temp source folder, stub `whosaid`), and
`test/mcp_workspace_tools_test.py`. `test/commitments_test.sh` covers the extractor, the corpus,
and the semantic dedupe (fake embedder versus difflib fallback); `test/worklist_test.sh` covers
the ranked worklist (tier rules, ordering, owner resolution, the CM + AI union, the regenerated
view, `--all-owners`, `--json`, and the `whosaid commitments` launcher command).

## Non-goals (v1)

- No PyPI/npm packaging (`git clone` + `./bootstrap.sh` is the install story; a `uvx` package is a
  possible later evolution).
- No daemonized/background recording. (`whosaid watch` ingests files another app already wrote;
  it never captures audio.)
- No cloud fallback of any kind: local-only is the point, not a default. Ollama is contacted on
  `127.0.0.1` only.
