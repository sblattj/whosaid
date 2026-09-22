#!/usr/bin/env python3
"""Issue #38 regression: noisy short turns, conservative cached folding and controls.
Run: uv run --with numpy python test/diarize_recovery_test.py
Synthetic embeddings exercise the real cluster_segments path, not model quality.
"""
import copy
import sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))
import diarize_sherpa as d
from estimate_k_test import turns


def unit(v):
    return v / (np.linalg.norm(v) + 1e-9)


def fixture():
    X = turns(2, 70, seed=38)
    bases = [unit(X[:70].sum(axis=0)), unit(X[70:].sum(axis=0))]
    rng = np.random.default_rng(38)
    data = [{'start': i*16., 'end': i*16.+15., 'emb': v.tolist()} for i,v in enumerate(X)]
    clean = copy.deepcopy(data)
    for i in range(90):
        noise = rng.normal(size=192)
        noise = unit(noise - float(noise @ bases[1])*bases[1])
        v = .55*bases[1] + np.sqrt(1-.55**2)*noise
        start = len(data)*16.
        data.append({'start':start, 'end':start+.6, 'emb':v.tolist()})
    return clean, data


def test_recovery():
    clean, noisy = fixture()
    X = np.array([s['emb'] for s in noisy])
    old = d.estimate_speakers(X)
    assert old['raw_k'] >= 20 and old['k'] == 20, 'control reproduces old saturation'
    assert len(d.cluster_segments(copy.deepcopy(clean), -1)[2]) == 2
    result = d.cluster_segments(copy.deepcopy(noisy), -1)
    segs, emb, speakers, estimate, _ = result
    assert len(speakers) == 2 and estimate['fallback']['k'] == 2
    assert estimate['raw_k'] == old['raw_k'] and estimate['saturated']
    assert [(s['start'],s['end']) for s in segs] == [(s['start'],s['end']) for s in noisy]
    assert d.cluster_segments(copy.deepcopy(noisy), -1)[0] == segs
    assert len(d.cluster_segments(copy.deepcopy(noisy), 4)[2]) == 4
    assert len(d.cluster_segments(copy.deepcopy(noisy), -1, min_speakers=4)[2]) >= 4
    assert len(d.cluster_segments(copy.deepcopy(noisy), -1, max_speakers=1)[2]) == 1
    # One short distinct guest: varying only the guest makes recovery abstain.
    rng = np.random.default_rng(12)
    guest = {'start':9999.,'end':9999.6,'emb':unit(rng.normal(size=192)).tolist()}
    assert d.cluster_segments(copy.deepcopy(noisy)+[guest], -1)[3]['fallback'] is None
    for k in (6,8,25):
        x = turns(k, 30, seed=9)
        e = d.estimate_speakers(x, durations=np.full(len(x),15.))
        assert e['k'] == min(k,20) and e['fallback'] is None


def test_fold():
    rng = np.random.default_rng(8)
    a,b,c = np.eye(192)[:3]
    embs = {'SPEAKER_00': a, 'SPEAKER_01': b,
            'SPEAKER_02': unit(b+.025*rng.normal(size=192)),
            'SPEAKER_03': unit(.55*b+np.sqrt(1-.55**2)*c),
            'SPEAKER_04': np.eye(192)[4], 'SPEAKER_05': np.eye(192)[5]}
    names = {sp:sp for sp in embs}
    names['SPEAKER_00'] = 'Host'
    names['SPEAKER_05'] = 'Local guest'
    segs=[]
    for sp in embs:
        for _ in range(10 if sp in ('SPEAKER_00','SPEAKER_01','SPEAKER_02') else 1):
            start = len(segs)*20.
            segs.append({'start':start,'end':start+(10. if sp in ('SPEAKER_00','SPEAKER_01','SPEAKER_02') else .6),'speaker':sp})
    result = d.fold_unknown_clusters(segs,embs,names,protected={'SPEAKER_05'})
    folded, vectors, labels, note = result
    assert len(labels)==4, labels
    assert labels['SPEAKER_00']=='Host' and labels['SPEAKER_05']=='Local guest'
    assert 'SPEAKER_04' in labels, 'distinct single-turn guest retained'
    assert len(folded)==len(segs)
    assert [(s['start'],s['end']) for s in folded]==[(s['start'],s['end']) for s in segs]
    assert set(labels)==set(vectors)=={s['speaker'] for s in folded}
    again=d.fold_unknown_clusters(folded,vectors,labels,protected={'SPEAKER_05'})
    assert again[0]==folded and again[2]==labels
    assert len(d.fold_unknown_clusters(segs,embs,names,minimum=5)[2])==5
    # Strong ties between distinct candidates must not assign a tiny fragment.
    ambiguous = unit(b+np.eye(192)[4])
    es = {'A':b,'B':np.eye(192)[4],'C':ambiguous}
    ss=[{'start':i*5.,'end':i*5.+4.,'speaker':sp} for sp in ('A','B') for i in range(8)]
    ss.append({'start':100.,'end':100.5,'speaker':'C'})
    assert len(d.fold_unknown_clusters(ss,es,{sp:sp for sp in es})[2])==3


def test_fold_original_evidence_survives_serialization():
    import json
    e = {str(i):np.array([np.cos(np.deg2rad(angle)),np.sin(np.deg2rad(angle))])
         for i,angle in enumerate((0,20,40))}
    segs = [{"start":i*100+j*20.,"end":i*100+j*20.+duration,"speaker":str(i)}
            for i,duration in enumerate((8.,10.,12.)) for j in range(5)]
    evidence = {}
    one = d.fold_unknown_clusters(segs,e,{sp:sp for sp in e},evidence=evidence)
    assert len(one[2])==2
    evidence = json.loads(json.dumps(evidence))
    two = d.fold_unknown_clusters(*one[:3], evidence=evidence)
    assert two[0]==one[0] and two[2]==one[2], 'cached centroids must not create similarity chains'
    # Updated support must be considered in the first invocation, not a later repair.
    e = {str(i):np.array([np.cos(np.deg2rad(angle)),np.sin(np.deg2rad(angle))])
         for i,angle in enumerate((0,10,55))}
    segs = [{"start":i*100+j*20.,"end":i*100+j*20.+duration,"speaker":str(i)}
            for i,duration in enumerate((3.,3.2)) for j in range(5)]
    segs.append({"start":500.,"end":500.6,"speaker":"2"})
    evidence = {}
    one = d.fold_unknown_clusters(segs,e,{sp:sp for sp in e},evidence=evidence)
    two = d.fold_unknown_clusters(*one[:3], evidence=json.loads(json.dumps(evidence)))
    assert len(one[2])==len(two[2])==1 and one[0]==two[0]
    # A known voice competes even though it cannot be a destination.
    e = {'Unknown':np.array([1.,0.,0.]), 'Host':np.array([0.,1.,0.]),
         'Tiny':np.array([.6,.8,0.])}
    segs=[{'start':i*10.,'end':i*10.+5.,'speaker':sp}
          for sp in ('Unknown','Host') for i in range(6)]
    segs.append({'start':99.,'end':99.6,'speaker':'Tiny'})
    assert len(d.fold_unknown_clusters(segs,e,{'Unknown':'Unknown','Host':'Named','Tiny':'Tiny'})[2])==3


def test_minimum_larger_than_reliable_pool():
    _, noisy = fixture()
    # Only ten long turns; retain all the noisy short turns.
    rows = noisy[:5]+noisy[70:75]+noisy[140:]
    result = d.cluster_segments(copy.deepcopy(rows), -1, min_speakers=15)
    assert len(result[2])>=15, 'duration filtering cannot shrink an explicit minimum'


def test_fold_match_schema():
    import json
    import subprocess
    import tempfile
    record = {"cluster":"SPEAKER_02", "name":"Host", "similarity":.2,
              "threshold":.5, "matched":False, "pass":"registry"}
    payload = {"registry_matches":[record], "source":{"path":"fixture.wav",
               "duration_seconds":100., "creation_time":None}}
    checker = Path(__file__).with_name('check_sidecar_schema.py')
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)/'fixture.json'
        def validate():
            target.write_text(json.dumps(payload))
            return subprocess.run([sys.executable,str(checker),str(target)],capture_output=True).returncode
        assert validate()==0, 'existing schema remains accepted'
        d.remap_fold_matches([record],[{"speaker":"SPEAKER_02"}],[{"speaker":"SPEAKER_00"}])
        assert record['cluster']=='SPEAKER_00' and record['original_cluster']=='SPEAKER_02'
        assert validate()==0, 'fold provenance accepted'
        record['original_cluster']=12
        assert validate()!=0, 'invalid provenance rejected'
        record['original_cluster']='SPEAKER_02'
        record['unexpected']=True
        assert validate()!=0, 'unknown fields still rejected'


def test_real_cli_cached_repeat():
    """Legacy 20-cluster cache: repair through the shipped CLI twice."""
    import json
    import os
    import subprocess
    import tempfile
    rng = np.random.default_rng(3801)
    channel = unit(rng.normal(size=192))
    bases = [unit(.4*channel+.6*unit(rng.normal(size=192))) for _ in range(2)]
    rows = []
    for person in range(2):
        for _ in range(70):
            start = len(rows)*16.
            rows.append({'start':start,'end':start+15.,
                         'emb':unit(bases[person]+rng.normal(scale=.024,size=192)).tolist()})
    for _ in range(90):
        v = rng.normal(size=192)
        v = unit(v-float(v@bases[1])*bases[1])
        start = len(rows)*16.
        rows.append({'start':start,'end':start+.6,
                     'emb':unit(.55*bases[1]+np.sqrt(1-.55**2)*v).tolist()})
    segs, embs, speakers, _, _ = d.cluster_segments(rows, 20)
    names = {sp:sp for sp in speakers}
    registry = {'speakers':[{'name':'Host','model':d.EMB_NAME,
                            'embedding':bases[0].tolist(),'role':'self'}]}
    d.name_clusters(embs,.5,.85,registry['speakers'],[],names)
    host = next(sp for sp,nm in names.items() if nm=='Host')
    labels = {host:{'name':'Host','note':'verified transcript label'}}
    payload = {'base':'legacy','emb_model':d.EMB_NAME,'num_speakers':20,
               'names':names,'segments':segs,'cluster_emb':{sp:v.tolist() for sp,v in embs.items()},
               'local_labels':labels,'roles':{'Host':'self'},'whisper_json':None,
               'count_estimate':{'raw_k':92,'k':20,'saturated':True,'min':None}}
    cli = Path(__file__).resolve().parent.parent/'whosaid'
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cache, reg = root/'legacy.diarization.json', root/'registry.json'
        whisper = root/'legacy.json'
        whisper.write_text(json.dumps({'segments':[dict(s, text='Fixture statement') for s in segs]}))
        payload['whisper_json'] = str(whisper)
        cache.write_text(json.dumps(payload))
        reg.write_text(json.dumps(registry))
        before_registry = reg.read_bytes()
        env = dict(os.environ, WHOSAID_SPEAKER_DB=str(reg), WHOSAID_VOICE_REFS=str(root/'refs'),
                   UV_OFFLINE='1')
        def repair():
            run = subprocess.run([str(cli),'relabel',str(cache),'--auto','--fold-unknown'],
                                 env=env,text=True,capture_output=True)
            assert run.returncode==0, run.stderr
            return json.loads(cache.read_text())
        first = repair()
        assert 2 <= first['num_speakers'] <= 3
        assert first['count_before_fold']==20
        second = repair()
        assert second['segments']==first['segments'] and second['names']==first['names']
        assert second['count_before_fold']==second['count_after_fold']==first['num_speakers']
        assert second['roles']==first['roles']==payload['roles']
        assert second['local_labels']==first['local_labels']==labels
        assert reg.read_bytes()==before_registry
        assert [(s['start'],s['end']) for s in second['segments']]==[(s['start'],s['end']) for s in segs]
        assert all((root/f'legacy.{suffix}').exists()
                   for suffix in ('rttm','speakers.txt','speaker-cards.txt'))


if __name__ == '__main__':
    test_recovery()
    test_fold()
    test_fold_original_evidence_survives_serialization()
    test_minimum_larger_than_reliable_pool()
    test_fold_match_schema()
    test_real_cli_cached_repeat()
    print('PASS: recovery, 6/8/25 controls, bounds, brief guest, folding, ambiguity and repeatability')
