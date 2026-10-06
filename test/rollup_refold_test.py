#!/usr/bin/env python3
"""`roll-up --refold MEETING`: a regenerated action-items.md is re-read into the
corpus WITHOUT --rebuild, so ids, statuses and hand edits survive. Offline;
run: uv run --quiet python test/rollup_refold_test.py"""
from __future__ import annotations

import difflib
import json
import re
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


def w_ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


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

        # --- owner follows the re-diarized labels of the item's earliest meeting.
        ws = fresh(tmp, "owner")
        meeting(ws, M1, [f"**SPEAKER_02:** {X_TXT}", f"**SPEAKER_00:** {Y_TXT}"])
        meeting(ws, M2, [f"**SPEAKER_01:** {X_TXT}"])
        check(rollup(ws, "--action-items").returncode == 0, "owner: initial")
        check(by_text(corpus(ws), X_TXT)["owner"] == "SPEAKER_02", "owner: initial from M1")
        meeting(ws, M2, [f"**SPEAKER_04:** {X_TXT}"])
        check(rollup(ws, "--refold", M2).returncode == 0, "owner: refold M2")
        check(by_text(corpus(ws), X_TXT)["owner"] == "SPEAKER_02", "owner: later meeting does not steal it")
        meeting(ws, M1, [f"**Zaphod:** {X_TXT}", f"**SPEAKER_03:** {X_TXT}",
                         f"**SPEAKER_01:** {Y_TXT}"])
        check(rollup(ws, "--refold", M1).returncode == 0, "owner: refold M1")
        data = corpus(ws)
        check(by_text(data, X_TXT)["owner"] == "Zaphod", "owner: earliest meeting's first bullet wins")
        check(by_text(data, Y_TXT)["owner"] == "SPEAKER_01", "owner: single-meeting item relabeled")

        # --- MEETING=OLD_MD: a hand-retitled item re-matches its regenerated bullet.
        ws = fresh(tmp, "alias")
        meeting(ws, M1, [f"**SPEAKER_02:** {X_TXT}", f"**SPEAKER_00:** {Y_TXT}"])
        check(rollup(ws, "--action-items").returncode == 0, "alias: initial")
        x_id = by_text(corpus(ws), X_TXT)["id"]
        md = ws / "_ACTION-ITEMS.md"
        md.write_text(md.read_text().replace(X_TXT, "Budget memo for finance"))
        prev = Path(tmp) / "alias-prev.md"
        prev.write_text((ws / M1 / "action-items.md").read_text())
        meeting(ws, M1, [f"**SPEAKER_05:** {X_TXT}", f"**SPEAKER_01:** {Y_TXT}"])
        r = rollup(ws, "--refold", f"{M1}={prev}")
        check(r.returncode == 0, f"alias: refold rc={r.returncode}: {r.stderr[-300:]}")
        data = corpus(ws)
        x = by_text(data, "Budget memo")
        check(x["id"] == x_id and [o["meeting"] for o in x["occurrences"]] == [M1],
              "alias: retitled item keeps its occurrence")
        check(x["owner"] == "SPEAKER_05", "alias: retitled item takes the new owner")
        check(not any(i["text"] == X_TXT for i in data["items"]), "alias: no duplicate minted")
        check(len(data["items"]) == 2, f"alias: still two items ({len(data['items'])})")
        # Control: without OLD_MD the retitled item cannot re-match and is re-minted.
        ws = fresh(tmp, "alias-control")
        meeting(ws, M1, [f"**SPEAKER_02:** {X_TXT}"])
        check(rollup(ws, "--action-items").returncode == 0, "alias control: initial")
        md = ws / "_ACTION-ITEMS.md"
        md.write_text(md.read_text().replace(X_TXT, "Budget memo for finance"))
        check(rollup(ws, "--refold", M1).returncode == 0, "alias control: refold")
        check(any(i["text"] == X_TXT for i in corpus(ws)["items"]), "alias control: re-minted")
        r = rollup(ws, "--refold", f"{M1}={Path(tmp) / 'missing.md'}")
        check(r.returncode == 2, f"missing OLD_MD exits 2 (got {r.returncode})")

        # --- a commitments refresh (new speaker labels) keeps CM ids and does
        # not burn ids: next_id stays put when nothing new appears.
        ws = fresh(tmp, "cm-refresh")
        (ws / M1).mkdir()

        def cm_json(speaker: str, texts: list[str]) -> None:
            items = [{"speaker": speaker, "speaker_role": None, "text": t, "time": "00:00:00",
                      "cue": "i will", "negative": False, "priority": "normal"} for t in texts]
            (ws / M1 / "commitments.json").write_text(json.dumps(
                {"source": "heuristic", "speakers": [speaker], "roles": {}, "min_words": 2,
                 "items": items, "dropped": []}))
        cm_json("SPEAKER_00", [f"I will {X_TXT.lower()}", f"I will {Y_TXT.lower()}"])
        check(rollup(ws).returncode == 0, "cm refresh: initial")
        before = json.loads((ws / "_commitments.json").read_text())
        check(before["next_id"] == 3, f"cm refresh: initial next_id ({before['next_id']})")
        cm_json("Zaphod", [f"I will {X_TXT.lower()}", f"I will {Y_TXT.lower()}"])
        r = rollup(ws)
        check(r.returncode == 0 and "replacing derived contributions" in r.stderr + r.stdout,
              "cm refresh: fingerprint refresh ran")
        after = json.loads((ws / "_commitments.json").read_text())
        check(sorted(i["id"] for i in after["items"]) == ["CM-001", "CM-002"], "cm refresh: ids kept")
        check({i["speaker"] for i in after["items"]} == {"Zaphod"}, "cm refresh: speakers follow")
        check(after["next_id"] == 3, f"cm refresh: next_id not burned ({after['next_id']})")
        cm_json("Zaphod", [f"I will {X_TXT.lower()}", f"I will {Y_TXT.lower()}", f"I will {Z_TXT.lower()}"])
        check(rollup(ws).returncode == 0, "cm refresh: third run")
        after = json.loads((ws / "_commitments.json").read_text())
        z = [i for i in after["items"] if Z_TXT.lower() in i["text"].lower()]
        check(len(z) == 1 and int(z[0]["id"].split("-")[1]) >= 3
              and after["next_id"] > int(z[0]["id"].split("-")[1]),
              f"cm refresh: new item minted above the old max ({after['next_id']}, {z})")

        # --- issue #74: a refresh must not burn CM ids. Two meetings fold in
        # order, so a genuinely new commitment in the EARLIER meeting is minted
        # below the later meeting's fresh ids; restore hands the old ids back
        # and the new item used to keep its high fold-time id.
        M1_CM = [f"I will {t}" for t in (
            "draft the quarterly budget proposal for finance",
            "migrate the billing database to the new cluster",
            "schedule the vendor security audit interview",
            "publish the onboarding handbook revision to the wiki")]
        M2_CM = [f"I will {t}" for t in (
            "rotate the production signing keys before friday",
            "interview two candidates for the analyst opening",
            "renegotiate the office lease renewal terms",
            "prepare the board slides on churn trends")]
        NEW_CM = "I will order replacement laptops for the support team"

        def cm_at(ws: Path, folder: str, speaker: str, texts: list[str]) -> None:
            (ws / folder).mkdir(exist_ok=True)
            items = [{"speaker": speaker, "speaker_role": None, "text": t, "time": "00:00:00",
                      "cue": "i will", "negative": False, "priority": "normal"} for t in texts]
            (ws / folder / "commitments.json").write_text(json.dumps(
                {"source": "heuristic", "speakers": [speaker], "roles": {}, "min_words": 2,
                 "items": items, "dropped": []}))

        def cm_corpus(ws: Path) -> dict:
            return json.loads((ws / "_commitments.json").read_text())

        def cm_num(i: dict) -> int:
            return int(i["id"].split("-")[1])

        ws = fresh(tmp, "cm-burn")
        (ws / "whosaid.toml").write_text("[search]\nembed = false\n")
        cm_at(ws, M1, "SPEAKER_00", M1_CM)
        cm_at(ws, M2, "SPEAKER_00", M2_CM)
        check(rollup(ws).returncode == 0, "cm burn: initial")
        before = cm_corpus(ws)
        check(before["next_id"] == 9 and len(before["items"]) == 8, f"cm burn: initial next_id {before['next_id']}")
        cm_at(ws, M1, "Zaphod", M1_CM + [NEW_CM])
        cm_at(ws, M2, "Zaphod", M2_CM)
        r = rollup(ws)
        check(r.returncode == 0 and "replacing derived contributions" in r.stderr + r.stdout,
              "cm burn: fingerprint refresh ran")
        after = cm_corpus(ws)
        old_ids = {i["id"] for i in before["items"]}
        new = [i for i in after["items"] if NEW_CM in i["text"]]
        check(len(new) == 1, "cm burn: one new item")
        check(old_ids <= {i["id"] for i in after["items"]}, "cm burn: every old id kept")
        check(after["next_id"] == before["next_id"] + 1,
              f"cm burn: next_id advances by one new item (got {after['next_id']}, want {before['next_id'] + 1})")
        check(new[0]["id"] == "CM-009", f"cm burn: new item takes the old next_id (got {new[0]['id']})")

        # --- issue #74: a near-identical text keeps its id; a different one does not.
        ws = fresh(tmp, "cm-fuzzy")
        (ws / "whosaid.toml").write_text("[search]\nembed = false\n")
        cm_at(ws, M1, "SPEAKER_00", M1_CM)
        check(rollup(ws).returncode == 0, "cm fuzzy: initial")
        before = cm_corpus(ws)
        ids = {i["text"]: i["id"] for i in before["items"]}
        tweaked = M1_CM[0] + " team"
        check(w_ratio(M1_CM[0], tweaked) >= 0.85, "cm fuzzy: fixture ratio >= 0.85")
        cm_at(ws, M1, "Zaphod", [tweaked, M1_CM[1], M1_CM[2], "I will repaint the lobby walls this weekend"])
        check(rollup(ws).returncode == 0, "cm fuzzy: refresh")
        after = cm_corpus(ws)
        got = {i["text"]: i["id"] for i in after["items"]}
        check(got[tweaked] == ids[M1_CM[0]], f"cm fuzzy: near-identical keeps its id ({got[tweaked]})")
        check(got["I will repaint the lobby walls this weekend"] == "CM-005",
              "cm fuzzy: different text gets the next fresh id")
        check(M1_CM[3] not in got, "cm fuzzy: dropped item gone")
        check(after["next_id"] == 6, f"cm fuzzy: next_id counts only the genuinely new item ({after['next_id']})")

        # --- issue #74: near-miss review pairs follow renumbered ids.
        near_a = "I will send the revised launch proposal to the team tomorrow morning"
        near_b = "I will send the revised launch plan to the whole team by tomorrow"
        ws = fresh(tmp, "cm-near")
        # difflib only: a local Ollama must not fold the near-miss pair together.
        (ws / "whosaid.toml").write_text("[search]\nembed = false\n")
        cm_at(ws, M1, "SPEAKER_00", M1_CM)
        cm_at(ws, M2, "SPEAKER_00", M2_CM)
        check(rollup(ws).returncode == 0, "cm near: initial")
        cm_at(ws, M1, "Zaphod", M1_CM + [near_a, near_b])
        cm_at(ws, M2, "Zaphod", M2_CM)
        check(rollup(ws).returncode == 0, "cm near: refresh")
        after = cm_corpus(ws)
        ids = {i["text"]: i["id"] for i in after["items"]}
        check({ids[near_a], ids[near_b]} == {"CM-009", "CM-010"}, f"cm near: compact new ids ({ids[near_a]}, {ids[near_b]})")
        check(after["next_id"] == 11, f"cm near: next_id ({after['next_id']})")
        md = (ws / "_COMMITMENTS.md").read_text()
        pair = re.search(r"- (CM-\d+) ↔ (CM-\d+) \(", md)
        check(bool(pair) and {pair.group(1), pair.group(2)} == {"CM-009", "CM-010"},
              f"cm near: review pair names the renumbered ids ({pair and pair.groups()})")

        # --- error paths.
        r = rollup(ws, "--refold", "2020-01-01-0000")
        check(r.returncode == 2, f"unknown folder exits 2 (got {r.returncode})")
        r = rollup(ws, "--refold", M1, "--rebuild")
        check(r.returncode == 2, f"--refold with --rebuild exits 2 (got {r.returncode})")

    print(f"OK: {CHECKS} checks")


if __name__ == "__main__":
    main()
