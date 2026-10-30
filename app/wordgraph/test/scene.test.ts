import { describe, expect, test } from 'bun:test';
import { demoInsights } from '../src/demo';
import { buildScene, childCountsOf, FoldState, readInsights } from '../src/scene';

const sample = {
  version: 1,
  topics: [
    { id: 'parser-1', rootId: 'parser-1', label: 'Parser', rank: 1, speakingMs: 412000, visits: 2, share: 0.6, new: false, firstT: 40, lastT: 1400, terms: ['regex'] },
    { id: 'deploy-2', rootId: 'deploy-2', label: 'Deploy', rank: 2, speakingMs: 200000, visits: 1, share: 0.4 },
  ],
  nodes: [
    { id: 'parser-1', topicId: 'parser-1', parentId: null, depth: 0, label: 'Parser', description: '', source: 'topic', evidence: [] },
    { id: 'parser-1/regex', topicId: 'parser-1', parentId: 'parser-1', depth: 1, label: 'regex', description: '', source: 'meeting', evidence: [] },
    { id: 'parser-1/regex/greedy-match', topicId: 'parser-1', parentId: 'parser-1/regex', depth: 2, label: 'greedy match', description: '', source: 'meeting', evidence: [] },
    { id: 'parser-1/fixture', topicId: 'parser-1', parentId: 'parser-1', depth: 1, label: 'fixture', description: '', source: 'meeting', evidence: [] },
    { id: 'deploy-2', topicId: 'deploy-2', parentId: null, depth: 0, label: 'Deploy', description: '', source: 'topic', evidence: [] },
    { id: 'deploy-2/regex', topicId: 'deploy-2', parentId: 'deploy-2', depth: 1, label: 'Regex', description: '', source: 'meeting', evidence: [] },
  ],
  summaries: [], nextSteps: [],
};

describe('scene builder', () => {
  test('everything starts expanded and childCounts cover leaves as 0', () => {
    const { scene, visible, childCounts } = buildScene(readInsights(sample), new FoldState());
    expect(scene.expandedIds).toEqual(new Set(['parser-1', 'parser-1/regex', 'deploy-2']));
    expect(childCounts).toEqual(new Map([
      ['parser-1', 2], ['parser-1/regex', 1], ['parser-1/regex/greedy-match', 0], ['parser-1/fixture', 0], ['deploy-2', 1], ['deploy-2/regex', 0],
    ]));
    expect(visible.length).toBe(6);
    expect(scene.pendingIds.size).toBe(0);
    expect(scene.bridges).toEqual([{ id: 'term:regex:deploy-2|parser-1', from: 'parser-1/regex', to: 'deploy-2/regex', label: 'regex · shared term' }]);
  });

  test('folding a root hides all descendants and drops their bridges; unfolding restores them', () => {
    const graph = readInsights(sample);
    const folds = new FoldState();
    const counts = childCountsOf(graph.nodes);
    expect(folds.toggle('parser-1', counts)).toBe(false);
    let built = buildScene(graph, folds);
    expect(built.visible.map(node => node.id)).toEqual(['parser-1', 'deploy-2', 'deploy-2/regex']);
    expect(built.scene.expandedIds.has('parser-1')).toBe(false);
    // The inner fold flag is untouched, matching jibbr's collapse semantics.
    expect(built.scene.expandedIds.has('parser-1/regex')).toBe(true);
    expect(built.scene.bridges).toEqual([]);
    expect(folds.toggle('parser-1', counts)).toBe(true);
    built = buildScene(graph, folds);
    expect(built.visible.length).toBe(6);
  });

  test('leaves do not toggle and expandAll folds or opens every parent', () => {
    const graph = readInsights(sample);
    const folds = new FoldState();
    const counts = childCountsOf(graph.nodes);
    expect(folds.toggle('parser-1/fixture', counts)).toBeUndefined();
    folds.expandAll(false, counts);
    expect(buildScene(graph, folds).visible.map(node => node.id)).toEqual(['parser-1', 'deploy-2']);
    folds.expandAll(true, counts);
    expect(buildScene(graph, folds).visible.length).toBe(6);
  });

  test('a later update keeps user folds and new parents arrive expanded', () => {
    const folds = new FoldState();
    const first = readInsights(sample);
    folds.toggle('deploy-2', childCountsOf(first.nodes));
    const later = readInsights({ ...sample, nodes: [...sample.nodes, { id: 'parser-1/fixture/golden', topicId: 'parser-1', parentId: 'parser-1/fixture', depth: 2, label: 'golden file', description: '', source: 'meeting', evidence: [] }] });
    const built = buildScene(later, folds);
    expect(built.scene.expandedIds.has('parser-1/fixture')).toBe(true);
    expect(built.visible.some(node => node.id === 'parser-1/fixture/golden')).toBe(true);
    expect(built.visible.some(node => node.id === 'deploy-2/regex')).toBe(false);
  });

  test('malformed input is tolerated and the demo is about 60 nodes across 4 topics', () => {
    expect(readInsights(null)).toEqual({ topics: [], nodes: [] });
    expect(readInsights({ nodes: [{ id: 'x' }, 5, { id: 'y', topicId: 't', label: 'ok', depth: 1, parentId: null }] }).nodes.map(node => node.id)).toEqual(['y']);
    const demo = readInsights(demoInsights());
    expect(demo.topics.length).toBe(4);
    expect(demo.nodes.length).toBe(60);
    expect(buildScene(demo, new FoldState()).scene.bridges.length).toBeGreaterThan(0);
  });
});
