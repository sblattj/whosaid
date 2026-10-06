#!/usr/bin/env python3
"""`roll-up --refold MEETING`: a regenerated action-items.md is re-read into the
corpus WITHOUT --rebuild, so ids, statuses and hand edits survive. Offline;
run: uv run --quiet python test/rollup_refold_test.py"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
WS_PY = REPO_DIR / "lib" / "workspace.py"

CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    CHECKS += 1


M1, M2 = "2026-09-01-0900", "2026-09-08-0900"
X_TXT = "Draft the quarterly budget proposal for finance"
Y_TXT = "Migrate the billing database to the new cluster"
Z_TXT = "Schedule the vendor security audit interview"
W_TXT = "Publish the onboarding handbook revision to the wiki"


def meeting(ws: Path, name: str, bullets: list[str]) -> None:
    d = ws / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "action-items.md").write_text("# Action items\n\n" + "\n".join(f"- {b}" for b in bullets) + "\n")


def rollup(ws: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(WS_PY), "rollup", str(ws), *extra],
                          capture_output=True, text=True, timeout=120)


def corpus(ws: Path) -> dict:
    return json.loads((ws / "_action-items.json").read_text())


def by_text(data: dict, needle: str) -> dict:
    hits = [i for i in data["items"] if needle in i["text"]]
    check(len(hits) == 1, f"exactly one item containing {needle!r}, got {len(hits)}")
    return hits[0]


def fresh(tmp: str, tag: str) -> Path:
    ws = Path(tmp) / tag
    ws.mkdir()
    return ws


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # --- main scenario: hand edits survive, ids stable, Z minted above max.
        ws = fresh(tmp, "main")
        meeting(ws, M1, [f"**Alice:** {X_TXT}", f"**Bob:** {Y_TXT}"])
        r = rollup(ws, "--action-items")
        check(r.returncode == 0, f"initial roll-up rc={r.returncode}: {r.stderr[-300:]}")
        data = corpus(ws)
        x_id, y_id = by_text(data, X_TXT)["id"], by_text(data, Y_TXT)["id"]
        check({x_id, y_id} == {"AI-001", "AI-002"}, "initial ids")

        md = ws / "_ACTION-ITEMS.md"
        text = md.read_text()
        text = text.replace(f"**{x_id}** [open]", f"**{x_id}** [done]")
        text = text.replace(Y_TXT, Y_TXT + " (renamed by hand)")
        md.write_text(text)

        meeting(ws, M1, [f"**Carol:** {X_TXT}", f"**Alice:** {Z_TXT}"])
        r = rollup(ws, "--refold", M1)
        check(r.returncode == 0, f"refold rc={r.returncode}: {r.stderr[-300:]}")
        data = corpus(ws)
        x = by_text(data, X_TXT)
        check(x["id"] == x_id and x["status"] == "done", "X keeps id and done status")
        check([o["meeting"] for o in x["occurrences"]] == [M1], "X re-folded once")
        y = by_text(data, "renamed by hand")
        check(y["id"] == y_id and y["occurrences"] == [], "hand-edited Y kept with zero occurrences")
        z = by_text(data, Z_TXT)
        check(int(z["id"].split("-")[1]) > 2, f"Z minted above previous max ({z['id']})")
        check(data["folded_meetings"] == [M1], "folded_meetings contains M exactly once")
        check(sorted(i["id"] for i in data["items"]) == sorted([x_id, y_id, z["id"]]), "no renumbering")

        # --- plain incremental run still skips a folded meeting (regression).
        meeting(ws, M1, [f"**Alice:** {W_TXT}"])
        r = rollup(ws, "--action-items")
        check(r.returncode == 0 and "already folded" in r.stderr + r.stdout, "plain run skips folded meeting")
        check(not any(W_TXT in i["text"] for i in corpus(ws)["items"]), "plain run did not fold new bullets")

        # --- untouched open item losing its only occurrence is dropped.
        ws = fresh(tmp, "drop")
        meeting(ws, M1, [f"**Alice:** {X_TXT}", f"**Bob:** {Y_TXT}"])
        check(rollup(ws, "--action-items").returncode == 0, "drop: initial")
        meeting(ws, M1, [f"**Alice:** {X_TXT}"])
        r = rollup(ws, "--refold", M1)
        check(r.returncode == 0, f"drop: refold rc={r.returncode}")
        data = corpus(ws)
        check([i["text"] for i in data["items"]] == [X_TXT], "untouched Y dropped, X kept")
        check(data["items"][0]["id"] == "AI-001", "unchanged bullet keeps its id")
        check(data["next_id"] == 3, "next_id never rewinds")

        # --- item spanning two meetings keeps the other meeting; spans recompute.
        ws = fresh(tmp, "span")
        meeting(ws, M1, [f"**Alice:** {X_TXT}"])
        meeting(ws, M2, [f"**Alice:** {X_TXT}"])
        check(rollup(ws, "--action-items").returncode == 0, "span: initial")
        x = corpus(ws)["items"][0]
        check((x["first_seen"], x["last_seen"], len(x["occurrences"])) == (M1, M2, 2), "span: initial span")
        meeting(ws, M1, [f"**Bob:** {Y_TXT}"])
        check(rollup(ws, "--refold", M1).returncode == 0, "span: refold")
        data = corpus(ws)
        x = by_text(data, X_TXT)
        check([o["meeting"] for o in x["occurrences"]] == [M2], "span: only M2 occurrence left")
        check((x["first_seen"], x["last_seen"]) == (M2, M2), "span: first/last recomputed")
        check(x["id"] == "AI-001", "span: id stable")
        check(set(data["folded_meetings"]) == {M1, M2}, "span: both meetings folded")

        # --- missing action-items.md: occurrences stripped, still folded.
        (ws / M1 / "action-items.md").unlink()
        r = rollup(ws, "--refold", M1)
        check(r.returncode == 0, "missing md: rc 0")
        check(not any(o["meeting"] == M1 for i in corpus(ws)["items"] for o in i["occurrences"]),
              "missing md: M1 occurrences stripped")

        # --- error paths.
        r = rollup(ws, "--refold", "2020-01-01-0000")
        check(r.returncode == 2, f"unknown folder exits 2 (got {r.returncode})")
        r = rollup(ws, "--refold", M1, "--rebuild")
        check(r.returncode == 2, f"--refold with --rebuild exits 2 (got {r.returncode})")

    print(f"OK: {CHECKS} checks")


if __name__ == "__main__":
    main()
