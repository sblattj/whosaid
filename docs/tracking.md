# Tracking promises and their outcomes

The tracking ledger records what happened after an item was extracted. It is
stored in the workspace as `_tracking.json`, separately from the generated
corpora and search index. Roll-up and reindex cannot erase these events.
Queries are local and read-only. No model, network request, or transcript
upload is used.

An event records an item ID, an occurrence date or timezone-qualified timestamp,
a type, and evidence. Statuses are `open`, `in_progress`, `delivered`, `accepted`,
`blocked`, `superseded`, and `unknown`. Lack of evidence never means
`not_started`. A generated parent can also be `partial` when some milestones
remain unfinished.

## Record reviewed evidence

Create a JSON file outside your repository containing an events array:

```json
[
  {
    "item_id": "AI-001",
    "type": "status",
    "status": "delivered",
    "occurred_at": "2020-01-02T14:00:00Z",
    "evidence": {
      "kind": "transcript",
      "ref": "meeting-b/transcript.speakers.txt:12",
      "meeting": "meeting-b",
      "line": 12,
      "t_sec": 90,
      "quote": "I sent the setup guide."
    }
  }
]
```

From the repository:

```sh
python3 lib/tracking.py import ./meetings ./reviewed-events.json
```

Evidence kinds are `transcript`, `external`, and `manual`. Each needs a nonempty
`ref`; transcript evidence also needs a meeting and a line or timestamp. An
optional confidence is between 0 and 1. External references can point to a pull
request, ticket, deployment receipt, or other artifact. Importing a reference
means the caller has checked it; the ledger does not fetch or verify URLs.
For a pull request, `artifact_type: "pull_request"`, `state`, and `merged_at`
can be recorded. A closed request without a merge date is rejected as delivery
proof. A merge still does not establish deployment.

Imports are atomic, serialized between writers, and content-idempotent. Each
stored event gets a stable ID and a recorded timestamp. An existing ID cannot
be rewritten. Append a correction with a `supersedes` array of previous event
IDs. Conflicting status events at the same time remain visible and yield
`unknown` until corrected. Curated/imported status evidence takes precedence
over automatic status extraction.

To mark one item without writing an events file, `set-status` appends a single
curated status event with manual evidence (`done` is stored as `delivered`). Ids
missing from `_action-items.json` and `_commitments.json` are refused. The macOS
app's Mark done and the `whosaid_set_status` MCP tool use the same call.

```sh
whosaid tracking set-status ./meetings CM-012 done --note "marked done in the whosaid app"
```

## Split a compound promise

Use a `milestones` event with the parent item ID and a nonempty milestones
array. Each child needs a stable `id`, `text`, and `acceptance` criterion:

```json
{
  "item_id": "AI-001",
  "type": "milestones",
  "occurred_at": "2020-01-01",
  "evidence": {"kind": "manual", "ref": "confirmed milestone split"},
  "milestones": [
    {"id": "AI-001:submit", "text": "Open the change request", "acceptance": "Request URL exists"},
    {"id": "AI-001:deploy", "text": "Deploy the release", "acceptance": "Deployment receipt exists"}
  ]
}
```

Record separate status events against each child ID. Opening the request only
completes submission. The parent remains partial until deployment is evidenced.
A completed parent event cannot hide unfinished children: the view reports the
conflict. To revise a split, append a newer milestones event and reuse unchanged
child IDs so their evidence stays attached.
Conflicting definitions at the same time remain unresolved with `unknown`
status and all candidate definitions until a correction selects the criteria.

## Ownership and priorities

A `handoff_requested` event names the proposed `owner`; it does not transfer
responsibility. A later `handoff_accepted` event must name that same owner and
reference the request in `request_event_id`. An explicit `owner` event records
an ownership decision. Views expose pending handoffs and the next action owner.
An assignment alone does not establish acceptance. Accepting a requested handoff
makes a proposed request actionable; standing directives remain standing and
never become overdue tasks. Completed items are also ineligible for overdue
scoring even when their owner originally accepted them.

A `priority` event needs `item_id`, `speaker`, `scope`, and `text`. Optional fields
include `rank` (positive integer), `explicit` (default true), and `state` (active,
deferred, or withdrawn). A newer assertion by the same speaker about the same
item and scope replaces their prior assertion in the current view. Other
speakers' conflicting assertions stay visible; there is no fabricated consensus.
Configured workspace groups can filter whose direction is included.

```sh
python3 lib/tracking.py priorities ./meetings --group leadership
python3 lib/tracking.py priorities ./meetings --group leadership --as-of 2020-01-01
python3 lib/tracking.py show ./meetings ./items.json --as-of 2020-01-01
```

Date-only `--as-of` includes the entire UTC day. Timestamp values must carry a
timezone. These queries use event occurrence time, not what the system knew at
a past moment. A current corpus status cannot prove historical state: when no
prior event establishes it, historical status is unknown. Items first seen
after the cutoff are excluded. Future-dated ledger events do not affect the
current view.

## Find completion candidates

```sh
python3 lib/tracking.py suggest ./items.json ./turns.json --limit 100
```

Items use the corpus shape (`id`, `text`, `owner` or `speaker`, `first_seen`).
Turns need `speaker`, `text`, `meeting`, a line or `t_sec`, and either an ISO
`occurred_at`/`date` or a date-prefixed meeting name. This conservative lexical
pass finds later statements from the owner with shared subject words and a
past-tense cue for the promised action. It rejects negated, future, and explicitly
quoted statements, and deduplicates repeated copies of the same candidate.

Suggestions are proposals requiring review. They do not change any status and
cannot be imported until `approved: true` is added to each reviewed candidate.
An incomplete compound promise produces an in-progress suggestion rather than
claiming the whole promise was delivered. The matcher intentionally misses
paraphrases, delegated completion, and references with insufficient subject
words. Add verified external or manual evidence for those cases.
Action cues use verb context, retaining subject nouns such as "release" in
"send release checklist". The standalone suggestion command has a 60-second
deadline, configurable with `--timeout`.

## Python integration

- `load_ledger(workspace)` reads and validates events, raising `TrackingError`
  for corrupt data instead of discarding it.
- `record_event(workspace, event)` and `record_events(workspace, events)` append
  reviewed evidence.
- `add_milestones(workspace, item_id, milestones, evidence, occurred_at)` records
  a split.
- `reconcile_items(workspace, items, as_of=None, ledger=None)` preserves caller
  fields and overlays status, evidence, conflicts, milestones, and ownership.
  Equivalent IDs supplied through `also`, `ai_refs`, or `tracking_aliases`
  share evidence in both directions.
- `priority_view(workspace, group=None, as_of=None, ledger=None)` returns current
  assertions, history, conflicts, and the selected group membership.
- `suggest_evidence(items, turns, limit=100, deadline=None)` returns unapproved
  candidate events. An optional absolute monotonic deadline raises `TimeoutError`
  from within the scan when reached.

The supplied `ledger` argument lets a caller load once for several read views.

## Cache freshness

Cached worklists retain their generation time and report whether their inputs
have changed. The fingerprint covers corpus files, the ledger, configuration,
and the transcript and matching diarization sidecar files used for attribution.
It detects content edits even when file timestamps are preserved, and detects
added or removed sources. Older caches without source fingerprints are readable
but marked stale. Read queries do not rewrite them.

Freshness describes input changes, not a frozen historical snapshot or an
expiration policy. Date-sensitive ranking can age without a file change.
Historical queries require live reconciliation instead of a current cache.
