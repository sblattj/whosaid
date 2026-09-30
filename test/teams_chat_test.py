#!/usr/bin/env python3
"""
Offline test for lib/teams_chat.py (GitHub issue #49: MS Teams chat as a
source-tagged corpus; the ingest half).

Builds a synthetic export with placeholder names (two chats over two local
days), ingests it into a temp workspace, and asserts: one folder per
(chat, local day) named per DATE_DIR_RE; teams.speakers.txt round-trips through
wsconfig.parse_turns (a display name holding ':' and a multi-line body
included); the sidecar contract (source.kind, creation_time, segments);
[teams.names] / [teams.roles] / --self and the '# Role:' header lines;
idempotent re-ingest and merge of an extra message; a -2 suffix when the
folder name is taken by an unrelated dated folder; and the real CLI
(`./whosaid teams ingest`, then index + exact search finding teams.speakers.txt).

Run:  python3 test/teams_chat_test.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"
sys.path.insert(0, str(LIB))

import teams_chat  # noqa: E402
import wsconfig  # noqa: E402

TZ = "America/Los_Angeles"
LA = ZoneInfo(TZ)
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{4}(?:-\d+)?$")
CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    assert cond, msg
    CHECKS += 1


def ms(y, mo, d, h, mi, s=0) -> int:
    """Epoch ms of a wall-clock time in America/Los_Angeles."""
    return int(datetime(y, mo, d, h, mi, s, tzinfo=LA).timestamp() * 1000)


DOE = "Doe, Jane (Vendor, consultant)"
ROE = "Roe: Richard"          # display name containing ':'
SELF = "Self_Example"
CHAT_A = "Project Widget"
CHAT_B = "Doe, Jane (Vendor, consultant)"


def rec(chat, author, t, text, **extra):
    r = {"chat": chat, "author": author, "epoch_ms": t, "text": text,
         "timestamp_iso": datetime.fromtimestamp(t / 1000, tz=timezone.utc).isoformat()}
    r.update(extra)
    return r


T_A1 = ms(2026, 9, 24, 9, 5, 6)
T_A2 = ms(2026, 9, 24, 9, 12, 39)
T_A3 = ms(2026, 9, 25, 15, 12, 30)
T_B1 = ms(2026, 9, 24, 15, 1, 0)
T_B2 = ms(2026, 9, 24, 15, 2, 30)

EXPORT = [
    rec(CHAT_A, ROE, T_A2, "opened PR 68 against the service"),
    rec(CHAT_A, ROE, T_A1, "i got the telemetry for the widget\nsecond line   here", chat_id="19:abc", url="https://example.invalid/c/abc"),
    rec(CHAT_A, SELF, T_A3, "zebrafish deploy is done"),
    rec(CHAT_A, ROE, T_A2, "opened PR 68 against the service"),            # duplicate
    rec(CHAT_A, ROE, ms(2026, 9, 24, 9, 30), "   \n  "),                    # empty: skipped
    rec(CHAT_B, DOE, T_B1, "can you send the list for the week"),
    rec(CHAT_B, SELF, T_B2, "sending it now"),
]


def write_export(d: Path, records, name="export.json") -> Path:
    p = d / name
    p.write_text(json.dumps(records), encoding="utf-8")
    return p


def mask(text: str) -> str:
    return re.sub(r'"scraped_at": "[^"]*"', '"scraped_at": "X"',
                  re.sub(r"scraped \d{4}-\d{2}-\d{2}", "scraped X", text))


def snapshot(ws: Path) -> dict[str, str]:
    return {str(p.relative_to(ws)): mask(p.read_text(encoding="utf-8"))
            for p in sorted(ws.rglob("teams.*")) if p.is_file()}


def folders(ws: Path) -> list[str]:
    return sorted(p.name for p in ws.iterdir() if p.is_dir() and (p / "teams.speakers.txt").exists())


def fresh(tmp: Path, name: str, toml: str = "") -> Path:
    ws = tmp / name
    ws.mkdir()
    (ws / "whosaid.toml").write_text(toml, encoding="utf-8")
    return ws


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="teams-chat-test-"))
    try:
        run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"PASS: teams_chat_test ({CHECKS} checks)")


def run(tmp: Path) -> None:
    export = write_export(tmp, EXPORT)
    env = dict(os.environ, WHOSAID_SPEAKER_DB=str(tmp / "no-registry.json"))
    os.environ["WHOSAID_SPEAKER_DB"] = env["WHOSAID_SPEAKER_DB"]

    # ---- (a) folders: two chats, two local days -> 3 chat-days
    ws = fresh(tmp, "ws1", '[teams.names]\n"Roe: Richard" = "Roe Richard"\n'
                           '"Doe, Jane (Vendor, consultant)" = "Doe Jane"\n'
                           '[teams.roles]\n"Doe Jane" = "vendor"\n')
    res = teams_chat.ingest(export, ws, tz_flag=TZ, self_name=SELF)
    fl = folders(ws)
    check(len(fl) == 3 and len(res) == 3, f"3 chat-days, got {fl}")
    check(all(DATE_DIR_RE.match(f) for f in fl), f"DATE_DIR_RE: {fl}")
    check(fl == ["2026-09-24-0905", "2026-09-24-1501", "2026-09-25-1512"], fl)
    a1 = next(r for r in res if r["folder"] == "2026-09-24-0905")
    check(a1["total"] == 2 and a1["new"] == 2, f"dedup + empty skip: {a1}")

    # ---- (b) speakers.txt parses
    txt = (ws / "2026-09-24-0905" / "teams.speakers.txt").read_text(encoding="utf-8")
    turns = wsconfig.parse_turns(txt)
    got = [(t.speaker, t.text, t.t_sec) for t in turns]
    want = [("Roe Richard", "i got the telemetry for the widget second line here", 9 * 3600 + 5 * 60 + 6),
            ("Roe Richard", "opened PR 68 against the service", 9 * 3600 + 12 * 60 + 39)]
    check(got == want, f"turns: {got}")
    check("\n\n[09:12:39]" in txt, "blank line between turns")
    # an UNMAPPED display name with ':' is sanitized to '-'
    ws_raw = fresh(tmp, "ws-raw")
    teams_chat.ingest(export, ws_raw, tz_flag=TZ)
    raw = (ws_raw / "2026-09-24-0905" / "teams.speakers.txt").read_text(encoding="utf-8")
    rt = wsconfig.parse_turns(raw)
    check([t.speaker for t in rt] == ["Roe- Richard"] * 2, [t.speaker for t in rt])
    check(rt[0].t_sec == want[0][2], "t_sec of sanitized-name turn")
    # the vendor chat's display name carries a comma and parens: still one speaker
    rt_b = wsconfig.parse_turns((ws_raw / "2026-09-24-1501" / "teams.speakers.txt").read_text(encoding="utf-8"))
    check([t.speaker for t in rt_b] == [DOE, SELF], [t.speaker for t in rt_b])

    # ---- (c) sidecar
    sc = json.loads((ws / "2026-09-24-0905" / "teams.diarization.json").read_text(encoding="utf-8"))
    src = sc["source"]
    check(sc["base"] == "teams", "base")
    check(src["kind"] == "teams-chat" and src["platform"] == "microsoft-teams" and src["surface"] == "web", src)
    check(src["chat_name"] == CHAT_A and src["chat_id"] == "19:abc" and src["url"] == "https://example.invalid/c/abc", src)
    check(src["day"] == "2026-09-24" and src["message_count"] == 2 and src["scraper"] == "teams-dom-scraper@1", src)
    check(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", src["scraped_at"]), src["scraped_at"])
    first_utc = datetime.fromtimestamp(T_A1 / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    check(src["creation_time"] == first_utc == "2026-09-24T16:05:06Z", src["creation_time"])
    check(sc["names"] == {"Roe: Richard": "Roe Richard"}, sc["names"])
    segs = sc["segments"]
    check([s["epoch_ms"] for s in segs] == [T_A1, T_A2], "segments sorted by epoch_ms")
    check(segs[0] == {"start": 32706, "end": 32706, "speaker": "Roe Richard", "epoch_ms": T_A1,
                      "mid": str(T_A1), "text": "i got the telemetry for the widget second line here"}, segs[0])
    sc_b = json.loads((ws / "2026-09-24-1501" / "teams.diarization.json").read_text(encoding="utf-8"))
    check(sc_b["source"]["chat_id"] is None and sc_b["source"]["url"] is None, "null chat_id/url")
    check(sorted(p.name for p in (ws / "2026-09-24-0905").iterdir()) == ["teams.diarization.json", "teams.speakers.txt"],
          "only the two files")

    # ---- (d) names / roles / --self header
    hdr_b = (ws / "2026-09-24-1501" / "teams.speakers.txt").read_text(encoding="utf-8").splitlines()[:6]
    check(hdr_b[0] == "# Speaker-labeled transcript: teams", hdr_b)
    check(hdr_b[1].startswith(f"# Source: ms-teams-chat ({CHAT_B}), scraped "), hdr_b)
    check(hdr_b[2] == f"# Speakers (2): Doe Jane, {SELF}", hdr_b)
    check(hdr_b[3:5] == ["# Role: Doe Jane = vendor", f"# Role: {SELF} = self"], hdr_b)
    check(hdr_b[5] == "", hdr_b)
    check(json.loads((ws / "2026-09-24-1501" / "teams.diarization.json").read_text())["roles"]
          == {"Doe Jane": "vendor", SELF: "self"}, "sidecar roles")
    hdr_a = txt.splitlines()[:4]
    check(hdr_a[2] == "# Speakers (1): Roe Richard" and hdr_a[3] == "", hdr_a)   # no role line for Roe
    # registry role is used when the toml has none; --self wins over both
    reg = tmp / "registry.json"
    reg.write_text(json.dumps({"speakers": [{"name": "Roe Richard", "model": "m.onnx", "embedding": [1.0],
                                              "added": "2026-01-01", "role": "boss"}]}))
    os.environ["WHOSAID_SPEAKER_DB"] = str(reg)
    ws_r = fresh(tmp, "ws-reg", '[teams.names]\n"Roe: Richard" = "Roe Richard"\n')
    teams_chat.ingest(export, ws_r, tz_flag=TZ)
    check("# Role: Roe Richard = boss" in (ws_r / "2026-09-24-0905" / "teams.speakers.txt").read_text(), "registry role")
    ws_r2 = fresh(tmp, "ws-reg2", '[teams.names]\n"Roe: Richard" = "Roe Richard"\n[teams.roles]\n"Roe Richard" = "peer"\n')
    teams_chat.ingest(export, ws_r2, tz_flag=TZ)
    check("# Role: Roe Richard = peer" in (ws_r2 / "2026-09-24-0905" / "teams.speakers.txt").read_text(), "toml beats registry")
    ws_r3 = fresh(tmp, "ws-reg3", '[teams.names]\n"Roe: Richard" = "Roe Richard"\n[teams.roles]\n"Roe Richard" = "peer"\n')
    teams_chat.ingest(export, ws_r3, tz_flag=TZ, self_name="Roe Richard")
    check("# Role: Roe Richard = self" in (ws_r3 / "2026-09-24-0905" / "teams.speakers.txt").read_text(), "--self wins")
    os.environ["WHOSAID_SPEAKER_DB"] = env["WHOSAID_SPEAKER_DB"]

    # ---- (e) idempotent re-ingest and merge
    before = snapshot(ws)
    res2 = teams_chat.ingest(export, ws, tz_flag=TZ, self_name=SELF)
    check(snapshot(ws) == before, "re-ingest changes nothing (scraped_at masked)")
    check(all(r["new"] == 0 for r in res2), res2)
    check(folders(ws) == fl, "no new folders")
    extra = EXPORT + [rec(CHAT_A, ROE, ms(2026, 9, 24, 11, 0), "one more thing", chat_id="19:abc")]
    res3 = teams_chat.ingest(write_export(tmp, extra, "export2.json"), ws, tz_flag=TZ, self_name=SELF)
    check(folders(ws) == fl, "extra message merges into the same folder")
    m = next(r for r in res3 if r["folder"] == "2026-09-24-0905")
    check(m["total"] == 3 and m["new"] == 1, m)
    sc2 = json.loads((ws / "2026-09-24-0905" / "teams.diarization.json").read_text())
    check(sc2["source"]["message_count"] == 3 and len(sc2["segments"]) == 3, "merged sidecar")
    check(len(wsconfig.parse_turns((ws / "2026-09-24-0905" / "teams.speakers.txt").read_text())) == 3, "merged txt")
    # dry run writes nothing
    ws_d = fresh(tmp, "ws-dry")
    res_d = teams_chat.ingest(export, ws_d, tz_flag=TZ, dry_run=True)
    check(len(res_d) == 3 and sorted(p.name for p in ws_d.iterdir()) == ["whosaid.toml"], "dry-run writes nothing")

    # ---- (f) name collision with an unrelated dated folder -> -2
    ws_c = fresh(tmp, "ws-coll")
    (ws_c / "2026-09-24-0905").mkdir()
    (ws_c / "2026-09-24-0905" / "transcript.speakers.txt").write_text("[00:00:01] A_Example: hi\n")
    teams_chat.ingest(export, ws_c, tz_flag=TZ)
    check((ws_c / "2026-09-24-0905-2" / "teams.speakers.txt").is_file(), sorted(p.name for p in ws_c.iterdir()))
    check(DATE_DIR_RE.match("2026-09-24-0905-2") is not None, "suffix matches DATE_DIR_RE")
    check(not (ws_c / "2026-09-24-0905" / "teams.speakers.txt").exists(), "unrelated folder untouched")
    teams_chat.ingest(export, ws_c, tz_flag=TZ)        # re-ingest finds the -2 folder, no -3
    check(not (ws_c / "2026-09-24-0905-3").exists(), "re-ingest reuses the -2 folder")

    # the tz flag decides the day: UTC puts 09-24 16:05 PDT on the same day but shifts the name
    ws_u = fresh(tmp, "ws-utc")
    teams_chat.ingest(export, ws_u, tz_flag="UTC")
    check("2026-09-24-1605" in folders(ws_u), folders(ws_u))

    # malformed / unreadable export -> TeamsError
    for bad in ("{not json", '{"x": 1}', '[{"chat": "c"}]'):
        (tmp / "bad.json").write_text(bad)
        try:
            teams_chat.ingest(tmp / "bad.json", ws_d, tz_flag=TZ)
        except teams_chat.TeamsError:
            check(True, "")
        else:
            check(False, f"expected TeamsError for {bad!r}")

    # ---- (g) the REAL CLI
    ws_cli = fresh(tmp, "ws-cli", "[search]\nembed = false\n")
    cli_env = dict(env)
    r = subprocess.run([str(REPO / "whosaid"), "teams", "ingest", str(export), "--into", str(ws_cli), "--tz", TZ],
                       capture_output=True, text=True, env=cli_env, cwd=REPO)
    check(r.returncode == 0, f"cli rc={r.returncode}: {r.stderr}")
    lines = r.stdout.strip().splitlines()
    check(len(lines) == 3 and lines[0].startswith("2026-09-24-0905  Project Widget  2 messages (2 new)"), lines)
    check(folders(ws_cli) == fl, folders(ws_cli))
    bad = subprocess.run([str(REPO / "whosaid"), "teams", "ingest", str(tmp / "missing.json"), "--into", str(ws_cli)],
                         capture_output=True, text=True, env=cli_env, cwd=REPO)
    check(bad.returncode == 1 and len(bad.stderr.strip().splitlines()) == 1, (bad.returncode, bad.stderr))
    ix = subprocess.run([str(REPO / "whosaid"), "index", str(ws_cli)], capture_output=True, text=True,
                        env=cli_env, cwd=REPO)
    check(ix.returncode == 0, f"index rc={ix.returncode}: {ix.stderr[-400:]}")
    se = subprocess.run([str(REPO / "whosaid"), "search", str(ws_cli), "zebrafish", "--mode", "exact", "--json"],
                        capture_output=True, text=True, env=cli_env, cwd=REPO)
    check(se.returncode == 0, f"search rc={se.returncode}: {se.stderr[-400:]}")
    hits = json.loads(se.stdout)
    check(hits and hits[0]["file"] == "teams.speakers.txt" and hits[0]["meeting"] == "2026-09-25-1512", hits[:1])
    # control: a word absent from every message finds nothing
    se0 = subprocess.run([str(REPO / "whosaid"), "search", str(ws_cli), "qqxxnothing", "--mode", "exact", "--json"],
                         capture_output=True, text=True, env=cli_env, cwd=REPO)
    check(json.loads(se0.stdout) == [], se0.stdout)


    # ---- (h) the real scraper's shapes: {chat, author, ts, epoch, text}
    def srec(chat, author, t, text):
        # exactly as teams-chat-scraper.js builds it: ts = new Date(epoch).toISOString()
        return {"chat": chat, "author": author,
                "ts": datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "epoch": t, "text": text}

    s_a = [srec(CHAT_A, DOE, T_A1, "first scraped line"), srec(CHAT_A, SELF, T_A2, "second scraped line")]
    s_b = [srec(CHAT_B, DOE, T_B1, "zebrafish from the other chat"), srec(CHAT_B, SELF, T_B2, "reply")]
    raw_by_chat = {c: {"chat": c, "harvested": len(m), "kept": len(m), "reachedCutoff": True, "messages": m}
                   for c, m in ((CHAT_A, s_a), (CHAT_B, s_b))}
    flat = sorted(s_a + s_b, key=lambda m: m["epoch"])
    (tmp / "raw_by_chat.json").write_text(json.dumps(raw_by_chat), encoding="utf-8")
    (tmp / "messages.json").write_text(json.dumps(flat), encoding="utf-8")
    (tmp / "messages.ndjson").write_text("\n".join(json.dumps(m) for m in flat) + "\n\n", encoding="utf-8")
    snaps = {}
    for name in ("raw_by_chat.json", "messages.json", "messages.ndjson"):
        w = fresh(tmp, "ws-" + name)
        res = teams_chat.ingest(tmp / name, w, tz_flag=TZ, self_name=SELF)
        snaps[name] = (folders(w), snapshot(w))
        check(len(folders(w)) == 2 and len(res) == 2, f"{name}: 2 chat-days, got {folders(w)}")
    check(snaps["raw_by_chat.json"] == snaps["messages.json"], "raw_by_chat == flat messages.json")
    check(snaps["messages.json"] == snaps["messages.ndjson"], "flat messages.json == NDJSON")
    txt = "".join(v for k, v in snaps["raw_by_chat.json"][1].items() if k.endswith("teams.speakers.txt"))
    check("second scraped line" in txt and "zebrafish from the other chat" in txt, "scraped text ingested")
    # a map record without its own chat takes the map key; epoch_ms beats epoch
    nochat = {"K chat": {"messages": [{"author": DOE, "epoch": T_A1, "text": "keyed"}]}}
    (tmp / "nochat.json").write_text(json.dumps(nochat), encoding="utf-8")
    check([r["chat"] for r in teams_chat.load_export(tmp / "nochat.json")] == ["K chat"], "map key is the chat")
    both = write_export(tmp, [{"chat": "C", "author": DOE, "epoch_ms": T_A1, "epoch": T_A2, "text": "x"}], "both.json")
    check(teams_chat.load_export(both)[0]["epoch_ms"] == T_A1, "epoch_ms wins over epoch")
    (tmp / "one.ndjson").write_text(json.dumps(flat[0]) + "\n", encoding="utf-8")
    check(len(teams_chat.load_export(tmp / "one.ndjson")) == 1, "single-record NDJSON loads as one record")
    # (d) ts only (no epoch), Z suffix and an offset; timestamp_iso beats ts; bool epoch rejected
    def ms_of(rec_):
        return teams_chat.load_export(write_export(tmp, [dict(chat="C", author=DOE, text="x", **rec_)], "one.json"))[0]["epoch_ms"]
    check(ms_of({"ts": "2026-09-24T16:05:06.000Z"}) == T_A1, "ts with Z")
    check(ms_of({"ts": "2026-09-24T09:05:06-07:00"}) == T_A1, "ts with offset")
    check(ms_of({"timestamp_iso": "2026-09-24T16:05:06Z", "ts": "2001-01-01T00:00:00Z"}) == T_A1, "timestamp_iso before ts")
    check(ms_of({"epoch": True, "ts": "2026-09-24T16:05:06Z"}) == T_A1, "bool epoch falls back to ts")
    # NDJSON error names the line; blank lines are skipped in the count
    (tmp / "badlines.ndjson").write_text(json.dumps(flat[0]) + "\n\n{oops\n", encoding="utf-8")
    try:
        teams_chat.load_export(tmp / "badlines.ndjson")
    except teams_chat.TeamsError as e:
        check("line 3" in str(e) and "\n" not in str(e), str(e))
    else:
        check(False, "expected TeamsError for a bad NDJSON line")
    # (e) none of the four keys: exit 1 through the real CLI, key names in stderr
    nokeys = write_export(tmp, [{"chat": "C", "author": DOE, "text": "no clock"}], "nokeys.json")
    ne = subprocess.run([str(REPO / "whosaid"), "teams", "ingest", str(nokeys), "--into", str(ws_cli), "--tz", TZ],
                        capture_output=True, text=True, env=cli_env, cwd=REPO)
    check(ne.returncode == 1 and len(ne.stderr.strip().splitlines()) == 1, (ne.returncode, ne.stderr))
    check(all(k in ne.stderr for k in ("epoch_ms", "epoch", "timestamp_iso", "ts")), ne.stderr)
    # CLI accepts the scraper's raw_by_chat map
    ws_raw = fresh(tmp, "ws-cli-raw")
    rr = subprocess.run([str(REPO / "whosaid"), "teams", "ingest", str(tmp / "raw_by_chat.json"), "--into", str(ws_raw), "--tz", TZ],
                        capture_output=True, text=True, env=cli_env, cwd=REPO)
    check(rr.returncode == 0 and len(rr.stdout.strip().splitlines()) == 2, (rr.returncode, rr.stderr, rr.stdout))


if __name__ == "__main__":
    main()
