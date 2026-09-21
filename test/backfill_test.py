#!/usr/bin/env python3
"""Offline production-wrapper regressions; run: uv run --with numpy python test/backfill_test.py."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'lib'))
import diarize_sherpa as d


def run(*args, ok=True):
    p = subprocess.run([str(REPO / 'whosaid'), *map(str, args)], env=env,
                       text=True, capture_output=True)
    assert (p.returncode == 0) == ok, (args, p.returncode, p.stdout, p.stderr)
    return p


def hashes(ws):
    return {str(p.relative_to(ws)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in ws.rglob('*') if p.is_file() and '__pycache__' not in str(p)}


with tempfile.TemporaryDirectory(prefix='whosaid-backfill-') as tmp:
    root = Path(tmp)
    ws = root / 'ws'
    ws.mkdir()
    home = root / 'home'
    home.mkdir()
    registry = root / 'speakers.json'
    env = dict(os.environ, HOME=str(home), WHOSAID_SPEAKER_DB=str(registry),
               UV_CACHE_DIR=os.environ.get('UV_CACHE_DIR', str(Path.home() / '.cache/uv')),
               UV_OFFLINE='1', PYTHONDONTWRITEBYTECODE='1')
    (ws / 'whosaid.toml').write_text('[search]\nembed = false\n[commitments]\nmin_words = 0\n')
    reg = dict(speakers=[dict(name='Alice', model='test', embedding=[1, 0]),
                         dict(name='Bob', model='test', embedding=[0, 1])])
    registry.write_text(json.dumps(reg))
    folders = []
    for day in (1, 2):
        folder = ws / f'2026-09-{day:02d}-0900'
        folder.mkdir()
        folders.append(folder)
        segs = [dict(start=0, end=4, speaker='SPEAKER_00'), dict(start=5, end=9, speaker='SPEAKER_01')]
        whisper = folder / 'transcript.json'
        whisper.write_text(json.dumps(dict(segments=[dict(start=0, end=4, text="I'll send the revised launch proposal tomorrow."),
                                                    dict(start=5, end=9, text="I'll review the security checklist today.")])))
        data = dict(base='transcript', emb_model='test', names={'SPEAKER_00':'Alice','SPEAKER_01':'Bob'},
                    segments=segs, cluster_emb={'SPEAKER_00':[1,0], 'SPEAKER_01':[0,1]}, whisper_json=str(whisper))
        (folder / 'transcript.diarization.json').write_text(json.dumps(data))
        d.render_outputs(folder, 'transcript', segs, list(data['names']), data['names'],
                         d.build_turns(segs, str(whisper)), 3, '2 speakers', emb_name='test')
        subprocess.run([sys.executable, str(REPO/'lib/workspace.py'), 'commitments',
                        '--transcript', str(folder/'transcript.speakers.txt'),
                        '--json-out', str(folder/'commitments.json')], env=env, check=True, capture_output=True)
    run('roll-up', ws, '--action-items')
    initial = json.loads((ws/'_commitments.json').read_text())
    assert len(initial['items']) == 2 and len(initial['folded_meetings']) == 2
    # One factor: registry roles; both already-folded meetings must change.
    reg['speakers'][0]['role'] = 'self'
    reg['speakers'][1]['role'] = 'advisor'
    registry.write_text(json.dumps(reg))
    before = hashes(ws)
    preview = run('backfill', ws, '--dry-run')
    assert hashes(ws) == before and preview.stdout.count('"status": "change"') == 2
    # Inject an actual second-meeting replacement failure through the wrapper;
    # every already-replaced first-meeting file must be restored.
    inject = root/'inject'
    inject.mkdir()
    (inject/'sitecustomize.py').write_text("""import os
_original = os.replace
_failed = False
def replace(src, dst, *args, **kwargs):
    global _failed
    if not _failed and '2026-09-02-0900' in str(dst) and '.new' in str(src):
        _failed = True
        raise OSError('controlled second-meeting replace failure')
    return _original(src, dst, *args, **kwargs)
os.replace = replace
""")
    env['PYTHONPATH'] = str(inject)
    failed_write = run('backfill', ws, ok=False)
    assert 'all replaced meeting files rolled back' in failed_write.stderr
    assert hashes(ws) == before
    env.pop('PYTHONPATH')
    run('backfill', ws)
    corpus = json.loads((ws/'_commitments.json').read_text())
    assert len(corpus['items']) == 1 and corpus['items'][0]['speaker'] == 'Alice'
    assert corpus['items'][0]['id'] == initial['items'][0]['id']
    assert all(json.loads((f/'commitments.json').read_text())['roles'] == {'Alice':'self','Bob':'advisor'} for f in folders)
    assert all('# Role: Bob = advisor' in (f/'transcript.speakers.txt').read_text() for f in folders)
    assert all('Bob  [advisor]' in (f/'transcript.speaker-cards.txt').read_text() for f in folders)
    assert (ws/'_search.db').exists()
    with sqlite3.connect(ws/'_search.db') as db:
        assert db.execute("select count(*) from sqlite_master where type='table'").fetchone()[0] > 4
    assert (ws/'_WIKI.md').is_file()
    before = hashes(ws)
    run('backfill', ws)
    # Search DB build metadata may change; source and corpus files must not.
    changed = [p for p,h in before.items() if not p.endswith('.db') and p != '_WIKI.md' and hashes(ws)[p] != h]
    assert not changed, changed
    # Reverting a manual text/status/speaker edit clears its persistent override.
    md = ws/'_COMMITMENTS.md'
    unedited = md.read_text()
    edited = unedited.replace('[open]', '[done]').replace('(Alice)', '(ManualOwner)')
    edited = edited.replace(corpus['items'][0]['text'], 'TEMPORARY manual title')
    md.write_text(edited)
    run('roll-up', ws)
    changed_item = json.loads((ws/'_commitments.json').read_text())['items'][0]
    assert changed_item['status']=='done' and changed_item['speaker']=='ManualOwner'
    assert changed_item['text']=='TEMPORARY manual title'
    md.write_text(unedited)
    run('roll-up', ws)
    (ws/'whosaid.toml').write_text('[search]\nembed = false\n[commitments]\nmin_words = 1\n')
    run('backfill', ws)
    restored = json.loads((ws/'_commitments.json').read_text())['items'][0]
    assert restored['text']==corpus['items'][0]['text'] and restored['status']=='open'
    assert restored['speaker']=='Alice' and not restored['curated']
    (ws/'whosaid.toml').write_text('[search]\nembed = false\n[commitments]\nmin_words = 0\n')
    run('backfill', ws)
    # Persistent corpus curation survives both regeneration and lost evidence.
    md = ws/'_COMMITMENTS.md'
    md.write_text(md.read_text().replace('[open]', '[done]').replace(corpus['items'][0]['text'], 'MANUAL launch decision.'))
    run('roll-up', ws)
    # Reassign self via the real selected-meeting wrapper, then backfill history.
    run('relabel', folders[0]/'transcript.diarization.json', '--role', 'Alice=', '--role', 'Bob=self')
    run('backfill', ws)
    corpus = json.loads((ws/'_commitments.json').read_text())
    assert any(it['speaker']=='Bob' and it['status']=='open' for it in corpus['items'])
    assert any(it['status']=='done' and 'MANUAL' in it['text'] for it in corpus['items']), corpus
    assert '# Role: Alice' not in (folders[1]/'transcript.speakers.txt').read_text()
    # A meeting-level human edit is preserved and preflight rejects all writes.
    meeting_md = folders[1]/'commitments.md'
    meeting_md.write_text(meeting_md.read_text()+'\nHuman follow-up note.\n')
    reg = json.loads(registry.read_text())
    reg['speakers'][1].pop('role')
    registry.write_text(json.dumps(reg))
    before = hashes(ws)
    failure = run('backfill', ws, ok=False)
    assert 'hand edits' in failure.stdout and hashes(ws) == before
    # Remove only the conflicting note: same operation now succeeds, no roles remain.
    meeting_md.write_text(meeting_md.read_text().replace('\nHuman follow-up note.\n',''))
    run('backfill', ws)
    assert all(not json.loads((f/'commitments.json').read_text())['roles'] for f in folders)
    # Transcript prose which does not alter extraction is preserved.
    transcript = folders[1]/'transcript.speakers.txt'
    transcript.write_text(transcript.read_text()+'\nHuman transcript annotation.\n')
    reg['speakers'][0]['role']='self'
    registry.write_text(json.dumps(reg))
    run('backfill', ws)
    assert 'Human transcript annotation.' in transcript.read_text()
    # A lone markdown artifact cannot be treated as generated disposable output.
    source = folders[0]/'commitments.json'
    valid = source.read_text()
    source.unlink()
    before = hashes(ws)
    orphaned = run('backfill', ws, ok=False)
    assert 'without commitments.json' in orphaned.stdout and hashes(ws) == before
    source.write_text('{}')
    before = hashes(ws)
    empty_json = run('backfill', ws, ok=False)
    assert 'items list' in empty_json.stdout and hashes(ws) == before
    source.write_text(valid)
    run('backfill', ws)
    # Corrupt historical sources fail closed in rollup, then recover after repair.
    source = folders[0]/'commitments.json'
    valid = source.read_text()
    source.write_text('{broken')
    before = hashes(ws)
    run('roll-up', ws, ok=False)
    assert hashes(ws) == before
    source.write_text(valid)
    run('backfill', ws)
    # Manual merges retain IDs and valid targets through a source refresh.
    reg['speakers'][0].pop('role')
    registry.write_text(json.dumps(reg))
    run('backfill', ws)
    corpus = json.loads((ws/'_commitments.json').read_text())
    active = [it for it in corpus['items'] if it['status']=='open']
    assert len(active) >= 2, corpus
    survivor, merged = active[:2]
    text = (ws/'_COMMITMENTS.md').read_text()
    text = text.replace(survivor['text'], survivor['text'] + f" (merged {merged['id']})", 1)
    (ws/'_COMMITMENTS.md').write_text(text)
    run('roll-up', ws)
    (ws/'whosaid.toml').write_text('[search]\nembed = false\n[commitments]\nmin_words = 1\n')
    run('backfill', ws)
    corpus = json.loads((ws/'_commitments.json').read_text())
    by_id = {it['id']:it for it in corpus['items']}
    assert by_id[merged['id']]['merged_into'] == survivor['id']
    assert survivor['id'] in by_id
    assert len(by_id[survivor['id']]['occurrences']) == 4
    # Transcript-only identities stay local even when a same-name registry role exists.
    local_sidecar = folders[1]/'transcript.diarization.json'
    local_data = json.loads(local_sidecar.read_text())
    local_data['local_labels'] = {'SPEAKER_01': {'name':'Bob', 'note':'local identity'}}
    local_sidecar.write_text(json.dumps(local_data))
    reg['speakers'][1]['role'] = 'advisor'
    registry.write_text(json.dumps(reg))
    run('backfill', ws)
    assert 'Bob' not in json.loads(local_sidecar.read_text()).get('roles', {})
    assert '# Role: Bob' not in (folders[1]/'transcript.speakers.txt').read_text()
    # Structurally invalid registry entries also fail before writes.
    registry.write_text(json.dumps({'speakers':[{'name':'Alice','model':'test','role':None}]}))
    before = hashes(ws)
    run('backfill', ws, ok=False)
    assert hashes(ws) == before
    # Registry damage is a failing control, never interpreted as role deletion.
    registry.write_text('{broken')
    before = hashes(ws)
    run('backfill', ws, ok=False)
    assert hashes(ws) == before
print('PASS: historical backfill, freshness, curation, dry-run, reassignment/removal and conflict controls')
