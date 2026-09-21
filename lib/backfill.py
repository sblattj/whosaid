#!/usr/bin/env python3
"""Auditable historical registry application. No audio or model execution."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
import shutil
from pathlib import Path

import diarize_sherpa as diarizer
import workspace


def rewrite_transcript(text, old_names, names, roles):
    renamed = {old_names[c]: n for c, n in names.items() if old_names.get(c, c) != n}
    lines = []
    for line in text.splitlines():
        if workspace.ROLE_HEADER_RE.match(line):
            continue
        header = workspace.SPEAKERS_HEADER_RE.match(line)
        if header:
            line = re.sub(r":.*$", ": " + ", ".join(sorted(set(names.values()))), line)
            lines.append(line)
            lines.extend(f"# Role: {n} = {r}" for n, r in sorted(roles.items()))
            continue
        turn = workspace.TURN_TIME_RE.match(line)
        if turn and turn['name'] in renamed:
            line = f"[{turn['time']}] {renamed[turn['name']]}: {turn['text']}"
        lines.append(line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def plan_meeting(folder, registry, cfg):
    sidecars = sorted(folder.glob('*.diarization.json'))
    if len(sidecars) != 1:
        raise ValueError(f"expected one cached diarization sidecar; found {len(sidecars)}")
    sidecar = sidecars[0]
    data = json.loads(sidecar.read_text())
    base = data['base']
    if Path(base).name != base:
        raise ValueError("sidecar base must be a filename")
    model = data.get('emb_model', diarizer.EMB_NAME)
    entries = [e for e in registry['speakers'] if e.get('model') == model]
    old_names = dict(data['names'])
    names = dict(old_names)
    local = data.get('local_labels', {})
    embeddings = {c: diarizer.np.asarray(v, dtype=diarizer.np.float32)
                  for c, v in data.get('cluster_emb', {}).items() if c not in local}
    # Match only unnamed clusters; named and transcript-only identities are never guessed over.
    diarizer.name_clusters(embeddings, diarizer.default_ref_threshold(),
                          0.85, entries, [], names)
    roles = {}
    for c, name in names.items():
        if c in local:
            if name in data.get('roles', {}):
                roles[name] = data['roles'][name]
            continue
        entry = next((e for e in entries if e.get('name') == name), None)
        if entry is None:
            if name in data.get('roles', {}):
                roles[name] = data['roles'][name]
        elif entry.get('role'):
            roles[name] = diarizer.normalize_role(entry['role'])
    transcript = folder / f'{base}.speakers.txt'
    original = transcript.read_text()
    old_roles = data.get('roles', {})
    if workspace.parse_roles(original) != old_roles:
        raise ValueError("transcript role headers differ from sidecar; reconcile these hand edits first")
    updated = rewrite_transcript(original, old_names, names, roles)
    writes = {}
    data['names'] = names
    if roles:
        data['roles'] = roles
    else:
        data.pop('roles', None)
    if names != old_names or roles != old_roles:
        writes[sidecar] = json.dumps(data, indent=2)
    if updated != original:
        writes[transcript] = updated
    cards = folder / f'{base}.speaker-cards.txt'
    if cards.is_file() and (names != old_names or roles != old_roles):
        card_text = cards.read_text()
        lines = []
        for line in card_text.splitlines():
            if '   —   ' in line:
                for cluster, old in old_names.items():
                    if line.startswith(old + ' '):
                        suffix = line[len(old):]
                        suffix = re.sub(r'^  \[[^\]]+\]', '', suffix)
                        name = names[cluster]
                        if name != old:
                            suffix = suffix.replace('  (UNIDENTIFIED)', '')
                        line = name + (f'  [{roles[name]}]' if name in roles else '') + suffix
                        break
            lines.append(line)
        writes[cards] = '\n'.join(lines) + '\n'
    # Re-extract from the preserved transcript body. Refuse to discard local
    # commitment edits or hook output: their regeneration needs human judgment.
    cj_path, cm_path = folder / 'commitments.json', folder / 'commitments.md'
    previous = json.loads(cj_path.read_text()) if cj_path.exists() else None
    if cj_path.exists():
        if not isinstance(previous, dict) or not isinstance(previous.get('items'), list):
            raise ValueError('commitments.json must be an object with an items list')
        if any(not isinstance(item, dict) for item in previous['items']):
            raise ValueError('commitments.json items must be objects')
        if not isinstance(previous.get('roles', {}), dict):
            raise ValueError('commitments.json roles must be an object')
        if not isinstance(previous.get('dropped', []), list):
            raise ValueError('commitments.json dropped must be a list')
    min_words = int(workspace.commitments_config(cfg)['min_words'])
    dropped = []
    items = workspace.extract_commitments(updated, roles, min_words, dropped)
    markdown = workspace.commitments_markdown(folder.name, items, dropped)
    if not markdown.endswith('\n'):
        markdown += '\n'
    payload = dict(transcript=str(transcript), md_out=str(cm_path), source='heuristic',
                   speakers=workspace.parse_speakers(updated), roles=roles,
                   min_words=min_words, items=items,
                   dropped=[dict(speaker=s, time=t, text=c, reason=r) for s,t,c,r in dropped])
    if previous is None and cm_path.exists():
        raise ValueError('commitments.md exists without commitments.json; preserve/reconcile this untracked document first')
    needs = previous is None or any(previous.get(k) != payload[k]
                                   for k in ('roles', 'items', 'min_words', 'dropped'))
    if needs and previous is not None:
        if previous.get('source') != 'heuristic':
            raise ValueError('hook commitments require explicit regeneration before backfill')
        baseline_dropped = [(d['speaker'], d['time'], d['text'], d['reason'])
                            for d in previous.get('dropped', [])]
        baseline = workspace.commitments_markdown(folder.name, previous['items'], baseline_dropped)
        baseline = baseline if baseline.endswith('\n') else baseline + '\n'
        if cm_path.exists() and cm_path.read_text() != baseline:
            raise ValueError('commitments.md has hand edits; preserved, reconcile before backfill')
        old_extracted = workspace.extract_commitments(original, previous.get('roles', old_roles),
                                                       previous.get('min_words', min_words))
        if previous['items'] != old_extracted:
            raise ValueError('commitments.json or transcript has edits affecting extraction; preserved')
    if needs:
        writes[cj_path] = json.dumps(payload, indent=2) + '\n'
        writes[cm_path] = markdown
    return {p: text for p,text in writes.items() if not p.exists() or p.read_text() != text}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workspace_dir')
    parser.add_argument('--dry-run', action='store_true', help='report proposed changes/conflicts; write nothing')
    args = parser.parse_args(argv)
    ws = Path(args.workspace_dir).expanduser().resolve()
    if not ws.is_dir():
        parser.error(f'workspace not found: {ws}')
    # Strict read: a damaged/missing registry must never look like role removal.
    try:
        registry = json.loads(diarizer.SPEAKER_DB.read_text())
        if not isinstance(registry.get('speakers'), list):
            raise ValueError('registry speakers must be a list')
        seen = set()
        for entry in registry['speakers']:
            if not isinstance(entry, dict):
                raise ValueError('registry entries must be objects')
            if any(not isinstance(entry.get(k), str) or not entry[k].strip() for k in ('name', 'model')):
                raise ValueError('registry entries require nonempty name and model')
            key = (entry['name'], entry['model'])
            if key in seen:
                raise ValueError(f'duplicate registry identity: {key}')
            seen.add(key)
            embedding = entry.get('embedding')
            if not isinstance(embedding, list) or not embedding or any(
                    isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                    for v in embedding):
                raise ValueError('registry embeddings must be nonempty finite numeric arrays')
            if 'role' in entry and not isinstance(entry['role'], str):
                raise ValueError('registry role must be a string')
        cfg = workspace._wsconfig().load_config(ws)
        plans, failures = [], []
        for folder in sorted(ws.iterdir()):
            if not folder.is_dir() or not workspace.DATE_DIR_RE.match(folder.name):
                continue
            try:
                writes = plan_meeting(folder, registry, cfg)
                plans.append((folder, writes))
                print(json.dumps(dict(meeting=folder.name, status='change' if writes else 'unchanged',
                                      files=[p.name for p in writes], dry_run=args.dry_run)))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                failures.append(folder.name)
                print(json.dumps(dict(meeting=folder.name, status='conflict', error=str(exc))))
        # Preflight the whole workspace before writing any meeting. A conflict
        # cannot leave a workspace half migrated and advertised as refreshed.
        if failures:
            return 1
        if not args.dry_run:
            writes = [(path, text) for _, changes in plans for path, text in changes.items()]
            # Stage every new file and every rollback copy before the first
            # replacement. Atomic rename avoids truncating originals on ENOSPC.
            with tempfile.TemporaryDirectory(prefix='.backfill-', dir=ws) as stage_name:
                stage = Path(stage_name)
                staged, backups, committed = {}, {}, []
                for index, (path, text) in enumerate(writes):
                    new = stage / f'{index}.new'
                    new.write_text(text)
                    new.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o600)
                    staged[path] = new
                    if path.exists():
                        backup = stage / f'{index}.old'
                        shutil.copy2(path, backup)
                        backups[path] = backup
                try:
                    for path, _ in writes:
                        os.replace(staged[path], path)
                        committed.append(path)
                except OSError as failure:
                    rollback_errors = []
                    for path in reversed(committed):
                        try:
                            if path in backups:
                                os.replace(backups[path], path)
                            else:
                                path.unlink()
                        except OSError as exc:
                            rollback_errors.append(f'{path}: {exc}')
                    if rollback_errors:
                        # Keep recovery copies outside TemporaryDirectory cleanup.
                        recovery = Path(tempfile.mkdtemp(prefix='.backfill-recovery-', dir=ws))
                        for path, backup in backups.items():
                            if backup.exists():
                                target = recovery / path.parent.name / path.name
                                target.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(backup, target)
                        raise OSError(f'{failure}; rollback failed: {rollback_errors}; recovery copies: {recovery}')
                    raise OSError(f'{failure}; all replaced meeting files rolled back')
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'backfill: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
