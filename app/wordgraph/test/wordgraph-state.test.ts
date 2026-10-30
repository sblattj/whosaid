// Adapted from jibbr (Stephen Blatt); MIT in whosaid.
import { describe, expect, test } from 'bun:test';
import { visibleWordgraphNodes, wordgraphBridges, type WordgraphView } from '../src/wordgraph-state';

const root = { id: 'root:a', topicId: 'a', parentId: null, depth: 0, label: 'Architecture', description: '', source: 'topic' as const, evidence: [] };
const child = { id: 'child:a', topicId: 'a', parentId: root.id, depth: 1, label: 'Activation', description: 'The first useful moment.', source: 'meeting' as const, evidence: [] };
const other = { ...child, id: 'child:b', topicId: 'b', parentId: 'root:b' };

describe('wordgraph state', () => {
  test('hides collapsed descendants and creates one bridge per pair of topic islands', () => {
    const view: WordgraphView = { nodes: [root, child], expandedIds: new Set() };
    expect(visibleWordgraphNodes(view)).toEqual([root]);
    view.expandedIds.add(root.id);
    expect(visibleWordgraphNodes(view).map(node => node.id)).toEqual([root.id, child.id]);
    expect(wordgraphBridges([child, other])).toEqual([{ id: 'term:activation:a|b', from: child.id, to: other.id, label: 'Activation · shared term' }]);
  });
  test('same-topic repeats never bridge and spellings normalize before matching', () => {
    const twin = { ...child, id: 'child:a2', label: '  ACTIVATION ' };
    expect(wordgraphBridges([child, twin])).toEqual([]);
    expect(wordgraphBridges([child, { ...other, label: 'activation' }]).length).toBe(1);
  });
});
