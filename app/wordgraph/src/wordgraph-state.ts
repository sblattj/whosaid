// Adapted from jibbr (Stephen Blatt); MIT in whosaid.
// Only the pure helpers; the fetch-driven WordgraphState class stays in jibbr.
import type { WordgraphNode } from './wordgraph-shared';
import { normalizeWordgraphTerm } from './wordgraph-shared';
import type { BalloonBridge } from './balloon-types';

export type WordgraphView = { nodes: WordgraphNode[]; expandedIds: Set<string> };

/** Nodes are visible only when every ancestor branch is explicitly open. */
export function visibleWordgraphNodes(view: WordgraphView): WordgraphNode[] {
  const byId = new Map(view.nodes.map(node => [node.id, node]));
  return view.nodes.filter(node => {
    for (let parent = node.parentId; parent; parent = byId.get(parent)?.parentId ?? null) {
      if (!view.expandedIds.has(parent)) return false;
    }
    return true;
  });
}

/** A bridge is a labelled shared spelling between distinct visible topic islands. */
export function wordgraphBridges(nodes: WordgraphNode[]): BalloonBridge[] {
  const terms = new Map<string, WordgraphNode[]>();
  for (const node of nodes) {
    const key = normalizeWordgraphTerm(node.label);
    const group = terms.get(key) ?? [];
    group.push(node); terms.set(key, group);
  }
  const bridges: BalloonBridge[] = [];
  for (const [term, group] of terms) {
    const seen = new Set<string>();
    // A stable chain makes each shared spelling legible without drawing a complete hairball.
    for (let i = 1; i < group.length; i++) {
      const j = i - 1;
      const a = group[j]!, b = group[i]!;
      if (a.topicId === b.topicId) continue;
      const pair = [a.topicId, b.topicId].sort().join('|');
      if (seen.has(pair)) continue;
      seen.add(pair);
      bridges.push({ id: `term:${term}:${pair}`, from: a.id, to: b.id, label: `${a.label} · shared term` });
    }
  }
  return bridges;
}
