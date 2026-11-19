#!/usr/bin/env python3
"""#233: backfill keeps the '# Speakers (N):' header's membership (renames applied, N = names
listed) and never pulls uncounted sidecar clusters into it.
Run: uv run --with numpy python test/backfill_header_test.py"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'lib'))
import backfill  # noqa: E402
import diarize_sherpa as d  # noqa: E402

checks = 0


def check(cond, msg):
    global checks
    checks += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def header(text, prefix):
    return next((ln for ln in text.splitlines() if ln.lower().startswith(prefix)), None)


# ---- unit: rewrite_transcript on the issue's shape
names_old = {'SPEAKER_00': 'Alice', 'SPEAKER_01': 'SPEAKER_01', 'SPEAKER_02': 'Stephen',
             **{f'SPEAKER_{i:02d}': f'SPEAKER_{i:02d}' for i in range(3, 12)}}
text = ("# Speaker-labeled transcript: t\n# Diarization: x\n"
        "# Speakers (3): Alice, SPEAKER_01, Stephen\n"
        "# Uncounted (under 1 s of talk, not in the headcount): SPEAKER_03, SPEAKER_04\n\n"
        "[00:00:01] Alice: hi\n[00:00:02] SPEAKER_01: hello\n[00:00:03] SPEAKER_03: uh\n")
names_new = dict(names_old, SPEAKER_01='Bob')
out = backfill.rewrite_transcript(text, names_old, names_new, {})
check(header(out, '# speakers') == "# Speakers (3): Alice, Bob, Stephen",
      f"header keeps membership with the rename applied: {header(out, '# speakers')!r}")
check("SPEAKER_05" not in header(out, '# speakers'), "uncounted clusters stay off the header")
check(header(out, '# uncounted') == header(text, '# uncounted'), "the Uncounted line is unchanged")
check("[00:00:02] Bob: hello" in out, "the renamed turn is rewritten")
check(out.replace("Bob", "SPEAKER_01") == text, "nothing but the renamed names changed")

# No rename: the header is byte-identical (count and list).
same = backfill.rewrite_transcript(text, names_old, dict(names_old), {})
check(same == text, "no rename: the transcript is unchanged")

# Two listed speakers renamed to one person: N follows the list.
merged = backfill.rewrite_transcript(text, names_old, dict(names_old, SPEAKER_01='Alice'), {})
check(header(merged, '# speakers') == "# Speakers (2): Alice, Stephen",
      f"N matches the names listed after a merge: {header(merged, '# speakers')!r}")

# An uncounted cluster that the registry now names: its turns get the name, and the name
# moves off the Uncounted line onto the header (it is a real, named speaker now).
named_unc = backfill.rewrite_transcript(text, names_old, dict(names_old, SPEAKER_03='Carol'), {})
check(header(named_unc, '# speakers') == "# Speakers (3): Alice, SPEAKER_01, Stephen",
      f"a named uncounted cluster stays out of the headcount: {header(named_unc, '# speakers')!r}")
check("[00:00:03] Carol: uh" in named_unc, "the named uncounted cluster's turn is renamed")
check(header(named_unc, '# uncounted') ==
      "# Uncounted (under 1 s of talk, not in the headcount): Carol, SPEAKER_04",
      f"the Uncounted line follows the rename: {header(named_unc, '# uncounted')!r}")

# ---- end to end: render_outputs writes the header, `whosaid backfill` rewrites it
with tempfile.TemporaryDirectory(prefix='whosaid-backfill-hdr-') as tmp:
    root = Path(tmp)
    ws, home, registry = root / 'ws', root / 'home', root / 'speakers.json'
    ws.mkdir(), home.mkdir()
    env = dict(os.environ, HOME=str(home), WHOSAID_SPEAKER_DB=str(registry),
               UV_CACHE_DIR=os.environ.get('UV_CACHE_DIR', str(Path.home() / '.cache/uv')),
               UV_OFFLINE='1', PYTHONDONTWRITEBYTECODE='1')
    (ws / 'whosaid.toml').write_text('[search]\nembed = false\n[commitments]\nmin_words = 0\n')
    registry.write_text(json.dumps(dict(speakers=[dict(name='Bob', model='test', embedding=[0, 1])])))
    folder = ws / '2026-09-03-0900'
    folder.mkdir()
    segs = [dict(start=0, end=4, speaker='SPEAKER_00'), dict(start=5, end=9, speaker='SPEAKER_01')]
    segs += [dict(start=10 + i, end=10.3 + i, speaker=f'SPEAKER_{i:02d}') for i in range(2, 8)]
    whisper = folder / 'transcript.json'
    whisper.write_text(json.dumps(dict(segments=[dict(start=s['start'], end=s['end'], text=f"line {i}")
                                                 for i, s in enumerate(segs)])))
    names = {'SPEAKER_00': 'Alice', **{f'SPEAKER_{i:02d}': f'SPEAKER_{i:02d}' for i in range(1, 8)}}
    emb = {'SPEAKER_00': [1, 0], 'SPEAKER_01': [0, 1], **{f'SPEAKER_{i:02d}': [0.7, -0.7] for i in range(2, 8)}}
    data = dict(base='transcript', emb_model='test', names=names, segments=segs, cluster_emb=emb,
                whisper_json=str(whisper))
    (folder / 'transcript.diarization.json').write_text(json.dumps(data))
    d.render_outputs(folder, 'transcript', segs, list(names), names,
                     d.build_turns(segs, str(whisper)), 3, '2 speakers', emb_name='test')
    st = folder / 'transcript.speakers.txt'
    before = st.read_text()
    check(header(before, '# speakers') == "# Speakers (2): Alice, SPEAKER_01",
          f"fixture: the diarizer counts two speakers: {header(before, '# speakers')!r}")
    check(header(before, '# uncounted') is not None, "fixture: six clusters are uncounted")
    p = subprocess.run([str(REPO / 'whosaid'), 'backfill', str(ws)], env=env, text=True, capture_output=True)
    check(p.returncode == 0, f"backfill runs: {p.returncode} {p.stderr[-400:]}")
    after = st.read_text()
    check(header(after, '# speakers') == "# Speakers (2): Alice, Bob",
          f"backfill header: only the rename changed, N matches: {header(after, '# speakers')!r}")
    check(header(after, '# uncounted') == header(before, '# uncounted'), "backfill keeps the Uncounted line")

print(f"PASS: backfill_header_test ({checks} checks)")
