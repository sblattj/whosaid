#!/usr/bin/env python3
"""
teams_calendar.py: read your Microsoft Teams calendar from the desktop app's local cache.

The new Teams desktop app keeps the calendar weeks it has synced in the same Chromium
IndexedDB store as chats (database Teams:calendar, object store calendar). Reading a copy
of it gives your meetings with no network call, no Graph permission and no browser. It is a
window, not the whole calendar: the weeks the app has loaded (open the Calendar tab and
page to a week to pull it in), so every result says what range the cache covers.

  python3 lib/teams_calendar.py events [--from today|2d|ISO] [--to ISO] [--days N] [--q TEXT]
                                       [--cancelled] [--free] [--ws WS] [--json]
  python3 lib/teams_calendar.py status [--json]

events lists meetings starting in the window (default: today through 7 days on), oldest
first, in local time. --q keeps events whose subject, organizer, location or attendee names
contain TEXT (case-insensitive). Cancelled events and ones shown as Free are left out unless
--cancelled / --free. Recurring meetings come as one row per occurrence; occurrences that the
cache stores without attendees or body inherit them from their series (attendees_from =
"series"). With a workspace, each event lists the recorded meeting folders (YYYY-MM-DD-HHMM,
local) that started between 10 minutes before it and its end.

Times in the store are UTC; eventTimeZone is the organizer's zone and is not used for math.
Cache location and override (WHOSAID_TEAMS_CACHE) are the ones teams_cache.py uses; the
decoded result is kept in ~/.cache/whosaid/teams-calendar.json until Teams writes again.
Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_LIB = Path(__file__).resolve().parent
sys.path.insert(0, str(_LIB))

import teams_cache as tc  # noqa: E402
from teams_cache import CacheError  # noqa: E402

CACHE_VERSION = 1
DB, STORE, INTERNAL = "calendar", "calendar", "calendar-internal-data"
RECORD_LEAD_MIN = 10
DATE_DIR_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})(?:-\d+)?$")
TEXT_SIDECARS = ("teams.diarization.json", "outlook.diarization.json")


# ---- reading -----------------------------------------------------------------------------

def read_calendar(path=None) -> dict:
    """Raw calendar rows and sync state from a copy of the store."""
    def reader(dbs, rows):
        if DB not in dbs:
            raise CacheError("the Teams cache has no calendar database; open the Calendar tab in the "
                             "Teams desktop app once so it syncs")
        internal = dict(rows(DB, INTERNAL, keys=True))
        return {"events": rows(DB, STORE), "internal": internal}
    return tc.read_dbs(reader, path)


def _utc_ms(s) -> int | None:
    """'2026-09-10T12:30:00' (UTC, no zone) or with Z/offset -> epoch ms."""
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()[:19] + "Z"


def _local(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="minutes")


def _attendees(raw) -> list[dict]:
    out = []
    for a in raw or []:
        if not isinstance(a, dict):
            continue
        st = a.get("status") if isinstance(a.get("status"), dict) else {}
        out.append({"name": a.get("name") or a.get("address") or "", "address": a.get("address"),
                    "type": a.get("type") or "Required", "response": st.get("response")})
    return out


def _body(raw: dict) -> str:
    content = raw.get("bodyContent")
    if isinstance(content, str) and content.strip():
        kind = "Text" if str(raw.get("bodyContentType", "")).lower() == "text" else "RichText/Html"
        return tc.html_to_text(content, kind)
    return (raw.get("bodyPreview") or "").strip()


def decode(raw: dict) -> dict:
    """Raw rows -> {events: [...], synced_at, sync_state}, recurring masters dropped."""
    rows = [r for r in raw.get("events", []) if isinstance(r, dict) and r.get("objectId")]
    by_id = {r["objectId"]: r for r in rows}
    series: dict[str, dict] = {}  # series id -> a row that has attendees/body, master first
    for r in sorted(rows, key=lambda r: r.get("eventType") != "RecurringMaster"):
        sid = r["objectId"] if r.get("eventType") == "RecurringMaster" else r.get("seriesMasterId")
        if sid and r.get("attendees") is not None and sid not in series:
            series[sid] = r
    events = []
    for r in rows:
        if r.get("eventType") == "RecurringMaster":
            continue
        start, end = _utc_ms(r.get("startTime")), _utc_ms(r.get("endTime"))
        if start is None:
            continue
        end = end if end is not None and end >= start else start
        sid = r.get("seriesMasterId")
        src, inherited = r, False
        if r.get("attendees") is None and sid:
            src = series.get(sid) or by_id.get(sid) or r
            inherited = src is not r
        ev = {
            "id": r["objectId"],
            "subject": (r.get("subject") or "").strip(),
            "start": _iso(start), "end": _iso(end), "start_ms": start, "end_ms": end,
            "start_local": _local(start), "end_local": _local(end),
            "minutes": round((end - start) / 60000),
            "organizer": r.get("organizerName") or src.get("organizerName") or "",
            "organizer_address": r.get("organizerAddress") or src.get("organizerAddress"),
            "location": (r.get("location") or "").strip(),
            "online": bool(r.get("isOnlineMeeting")),
            "join_url": r.get("skypeTeamsMeetingUrl") or r.get("joinOnlineMeetingUrl") or None,
            "response": r.get("myResponseType"),
            "show_as": r.get("showAs"),
            "cancelled": bool(r.get("isCancelled")),
            "kind": {"Occurrence": "occurrence", "Exception": "exception"}.get(r.get("eventType"), "single"),
            "series_id": sid,
            "attendees": _attendees(src.get("attendees")),
            "body": _body(src if inherited else r),
            "event_time_zone": r.get("eventTimeZone"),
            "last_modified": r.get("lastModifiedTime"),
        }
        if inherited:
            ev["attendees_from"] = "series"
        events.append(ev)
    events.sort(key=lambda e: (e["start_ms"], e["subject"]))
    internal = raw.get("internal") or {}
    sync = internal.get("lastSuccesfulSyncTimestamp")  # sic, the store's own spelling
    sync_ms = sync.get("value") if isinstance(sync, dict) else sync
    state = internal.get("syncState")
    state = state.get("value") if isinstance(state, dict) else state
    return {"events": events,
            "synced_at": _iso(int(sync_ms)) if isinstance(sync_ms, (int, float)) and sync_ms > 0 else None,
            "sync_state": state if isinstance(state, str) else None}


def _cache_file() -> Path:
    return tc._cache_file().with_name("teams-calendar.json")


def load(path=None, use_cache: bool = True) -> dict:
    """decode(read_calendar()) with a cache keyed on the store's fingerprint."""
    d = tc.cache_dir(path)
    if not tc._leveldb(d).is_dir():
        raise CacheError(f"no Teams cache at {tc._leveldb(d)} (set WHOSAID_TEAMS_CACHE)")
    fp = f"{CACHE_VERSION}:{tc.fingerprint(d)}"
    cf = _cache_file()
    if use_cache:
        try:
            got = json.loads(cf.read_text())
            if got.get("fingerprint") == fp:
                got["data"]["cached"] = True
                return got["data"]
        except (OSError, ValueError, KeyError):
            pass
    t0 = time.time()
    data = decode(read_calendar(d))
    data.update(read_seconds=round(time.time() - t0, 1), read_at=datetime.now(timezone.utc).isoformat()[:19] + "Z",
                cache=str(d), cached=False)
    if use_cache:
        try:
            cf.parent.mkdir(parents=True, exist_ok=True)
            tmp = cf.with_suffix(".tmp")
            tmp.write_text(json.dumps({"fingerprint": fp, "data": data}, ensure_ascii=False))
            os.chmod(tmp, 0o600)
            tmp.replace(cf)
        except OSError:
            pass
    return data


# ---- recorded meetings in a workspace ------------------------------------------------------

def recorded_folders(ws: Path) -> list[tuple[int, str]]:
    """(local start epoch ms, folder) for dated meeting folders, chat/mail day folders left out."""
    out = []
    try:
        entries = list(ws.iterdir())
    except OSError:
        return out
    for p in entries:
        m = DATE_DIR_RE.match(p.name)
        if not m or not p.is_dir() or any((p / s).exists() for s in TEXT_SIDECARS):
            continue
        y, mo, d, hh, mm = (int(x) for x in m.groups())
        try:
            out.append((int(datetime(y, mo, d, hh, mm).astimezone().timestamp() * 1000), p.name))
        except ValueError:
            continue
    return sorted(out)


def _match(ev: dict, folders: list[tuple[int, str]]) -> list[str]:
    lo = ev["start_ms"] - RECORD_LEAD_MIN * 60000
    hi = max(ev["end_ms"], ev["start_ms"] + 60000)
    return [f for ms, f in folders if lo <= ms < hi]


# ---- the window query ------------------------------------------------------------------------

def parse_when(s: str | None, default_ms: int, future: bool = False) -> int:
    """'today', 'tomorrow', 'yesterday', '2d' (ago, or ahead with future), 'YYYY-MM-DD' or ISO -> epoch ms."""
    if not s:
        return default_ms
    s = s.strip()
    low = s.lower()
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    words = {"today": 0, "tomorrow": 1, "yesterday": -1}
    if low in words:
        return int((midnight + timedelta(days=words[low])).timestamp() * 1000)
    if low == "now":
        return int(time.time() * 1000)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([mhdw])", low)
    if m and future:
        mult = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
        return int((time.time() + float(m.group(1)) * mult) * 1000)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return int(datetime.fromisoformat(s).astimezone().timestamp() * 1000)
    try:
        return tc.parse_since(s)
    except CacheError:
        raise CacheError(f"{s!r}: use today, tomorrow, yesterday, now, 3d, 2026-11-09 or an ISO time") from None


def events(start_ms: int | None = None, end_ms: int | None = None, query: str | None = None,
           include_cancelled: bool = False, include_free: bool = False, ws: Path | None = None,
           limit: int | None = 200, path=None) -> dict:
    """Events starting in [start_ms, end_ms), oldest first, plus what range the cache covers."""
    data = load(path)
    if start_ms is None:
        start_ms = parse_when("today", 0)
    if end_ms is None:
        end_ms = start_ms + 7 * 86400000
    want = (query or "").strip().lower()
    folders = recorded_folders(ws) if ws else None
    rows, skipped = [], {"cancelled": 0, "free": 0}
    for ev in data["events"]:
        if not (start_ms <= ev["start_ms"] < end_ms or ev["start_ms"] < start_ms < ev["end_ms"]):
            continue
        if want:
            hay = " ".join([ev["subject"], ev["organizer"], ev["location"],
                            *(a["name"] for a in ev["attendees"])]).lower()
            if want not in hay:
                continue
        if ev["cancelled"] and not include_cancelled:
            skipped["cancelled"] += 1
            continue
        if ev["show_as"] == "Free" and not include_free and not ev["cancelled"]:
            skipped["free"] += 1
            continue
        row = dict(ev)
        if folders is not None:
            row["recorded"] = _match(ev, folders)
        rows.append(row)
    total = len(rows)
    if limit:
        rows = rows[:int(limit)]
    starts = [e["start_ms"] for e in data["events"]]
    return {"ok": True, **{k: data.get(k) for k in ("cache", "read_at", "read_seconds", "cached")},
            "synced_at": data.get("synced_at"), "sync_state": data.get("sync_state"),
            "covers": {"first_start": _iso(min(starts)) if starts else None,
                       "last_start": _iso(max(starts)) if starts else None, "events": len(starts)},
            "window": {"from": _iso(start_ms), "to": _iso(end_ms)},
            "skipped": skipped, "total": total, "count": len(rows), "events": rows}


def status(path=None) -> dict:
    data = load(path)
    starts = [e["start_ms"] for e in data["events"]]
    return {"ok": True, "cache": data.get("cache"), "synced_at": data.get("synced_at"),
            "sync_state": data.get("sync_state"), "events": len(starts),
            "first_start": _iso(min(starts)) if starts else None,
            "last_start": _iso(max(starts)) if starts else None}


# ---- CLI ---------------------------------------------------------------------------------

def _print_events(res: dict) -> None:
    age = "cached" if res.get("cached") else f"read in {res.get('read_seconds') or 0}s"
    cov = res["covers"]
    print(f"# Teams calendar cache: synced {res.get('synced_at')}, {age}; holds {cov['events']} event(s) "
          f"starting {(cov['first_start'] or '?')[:10]} .. {(cov['last_start'] or '?')[:10]}")
    skipped = ", ".join(f"{n} {k}" for k, n in res["skipped"].items() if n)
    print(f"# {res['count']} of {res['total']} event(s)" + (f"; hidden: {skipped}" if skipped else ""))
    day = None
    for e in res["events"]:
        s = datetime.fromtimestamp(e["start_ms"] / 1000)
        t = datetime.fromtimestamp(e["end_ms"] / 1000)
        if s.date() != day:
            day = s.date()
            print(f"\n{s:%a %Y-%m-%d}")
        flags = []
        if e["cancelled"]:
            flags.append("CANCELLED")
        if e["response"] and e["response"] != "Accepted":
            flags.append({"NotResponded": "not answered", "Organizer": "organizer"}.get(e["response"], e["response"].lower()))
        if e.get("recorded"):
            flags.append("recorded: " + ", ".join(e["recorded"]))
        who = f"  ({e['organizer']})" if e["organizer"] else ""
        tail = f"  [{'; '.join(flags)}]" if flags else ""
        print(f"  {s:%H:%M}-{t:%H:%M}  {e['subject'] or '(no subject)'}{who}{tail}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="whosaid teams calendar", description=__doc__.split("\n\n")[1].strip())
    sub = ap.add_subparsers(dest="cmd")
    pe = sub.add_parser("events", help="meetings in a window (default today + 7 days)")
    pe.add_argument("--from", dest="frm", default=None, help="today (default), tomorrow, yesterday, now, 2d (ago), "
                    "2026-11-09 or ISO")
    pe.add_argument("--to", default=None, help="end of the window (same forms; 3d means 3 days ahead)")
    pe.add_argument("--days", type=float, default=None, help="window length in days (default 7)")
    pe.add_argument("--q", default=None, help="subject / organizer / location / attendee substring")
    pe.add_argument("--cancelled", action="store_true", help="include cancelled events")
    pe.add_argument("--free", action="store_true", help="include events shown as Free")
    pe.add_argument("--limit", type=int, default=200)
    pe.add_argument("--ws", default=os.environ.get("WHOSAID_WORKSPACE"),
                    help="workspace whose recorded meeting folders to match")
    sub.add_parser("status", help="what the calendar cache holds and when Teams last synced it")
    for p in sub.choices.values():
        p.add_argument("--json", action="store_true")
        p.add_argument("--cache", default=None, help="IndexedDB dir (default: WHOSAID_TEAMS_CACHE or the Teams app's)")
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in sub.choices and argv[0] not in ("-h", "--help"):
        argv.insert(0, "events")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            res = status(a.cache)
            if a.json:
                print(json.dumps(res, indent=2))
            else:
                print(f"{res['cache']}\n  {res['events']} event(s) starting {res['first_start']} .. {res['last_start']}; "
                      f"synced {res['synced_at']} ({res['sync_state']})")
            return 0
        start = parse_when(a.frm, parse_when("today", 0))
        if a.to:
            end = parse_when(a.to, 0, future=True)
        else:
            end = start + int((a.days if a.days is not None else 7) * 86400000)
        if end <= start:
            raise CacheError("--to must be after --from")
        ws = Path(a.ws).expanduser() if a.ws else None
        res = events(start, end, a.q, a.cancelled, a.free, ws if ws and ws.is_dir() else None, a.limit, a.cache)
        if a.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            _print_events(res)
        return 0
    except CacheError as e:
        print(f"whosaid teams calendar: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
