// Pure scene building for the whosaid wordgraph (no DOM, so bun test can import it).
import type { BalloonScene } from './balloon-types';
import { visibleWordgraphNodes, wordgraphBridges } from './wordgraph-state';
import { WORDGRAPH_MAX_DEPTH, WORDGRAPH_MAX_NODES, type WordgraphNode, type WordgraphTopic } from './wordgraph-shared';

export type InsightsGraph = { topics: WordgraphTopic[]; nodes: WordgraphNode[] };

const text = (value: unknown, fallback = '') => typeof value === 'string' ? value : fallback;
const num = (value: unknown, fallback = 0) => typeof value === 'number' && Number.isFinite(value) ? value : fallback;

/** Reads only topics and nodes from an insights object; malformed entries are dropped. */
export function readInsights(input: unknown): InsightsGraph {
  const obj = (input && typeof input === 'object' ? input : {}) as { topics?: unknown; nodes?: unknown };
  const topics: WordgraphTopic[] = [];
  for (const raw of Array.isArray(obj.topics) ? obj.topics : []) {
    if (!raw || typeof raw !== 'object') continue;
    const t = raw as Record<string, unknown>;
    const id = text(t.id);
    if (!id) continue;
    topics.push({
      id, rootId: text(t.rootId, id), label: text(t.label, id), rank: Math.max(1, Math.round(num(t.rank, topics.length + 1))),
      speakingMs: typeof t.speakingMs === 'number' ? t.speakingMs : null, visits: num(t.visits),
      share: Math.min(1, Math.max(0, num(t.share))),
    });
  }
  const nodes: WordgraphNode[] = [];
  const seen = new Set<string>();
  for (const raw of Array.isArray(obj.nodes) ? obj.nodes : []) {
    if (nodes.length >= WORDGRAPH_MAX_NODES) break;
    if (!raw || typeof raw !== 'object') continue;
    const n = raw as Record<string, unknown>;
    const id = text(n.id), topicId = text(n.topicId), label = text(n.label).trim();
    if (!id || !topicId || !label || seen.has(id)) continue;
    seen.add(id);
    const source = n.source === 'topic' || n.source === 'related' ? n.source : 'meeting';
    nodes.push({
      id, topicId, parentId: typeof n.parentId === 'string' && n.parentId ? n.parentId : null,
      depth: Math.min(WORDGRAPH_MAX_DEPTH, Math.max(0, Math.round(num(n.depth)))),
      label, description: text(n.description), source, evidence: [],
    });
  }
  return { topics, nodes };
}

/** Child count for every node, leaves included as 0 (jibbr draws "+" for an unknown count). */
export function childCountsOf(nodes: WordgraphNode[]): Map<string, number> {
  const counts = new Map(nodes.map(node => [node.id, 0]));
  for (const node of nodes) if (node.parentId && counts.has(node.parentId)) counts.set(node.parentId, counts.get(node.parentId)! + 1);
  return counts;
}

/** Everything is open by default; only the user's folds are stored, so new parents arrive expanded. */
export class FoldState {
  readonly collapsed = new Set<string>();
  expandedIds(counts: Map<string, number>): Set<string> {
    return new Set([...counts].filter(([id, count]) => count > 0 && !this.collapsed.has(id)).map(([id]) => id));
  }
  /** Returns the new expanded flag, or undefined for a leaf. */
  toggle(id: string, counts: Map<string, number>): boolean | undefined {
    if (!(counts.get(id)! > 0)) return undefined;
    if (this.collapsed.delete(id)) return true;
    this.collapsed.add(id);
    return false;
  }
  expandAll(on: boolean, counts: Map<string, number>) {
    this.collapsed.clear();
    if (!on) for (const [id, count] of counts) if (count > 0) this.collapsed.add(id);
  }
}

export function buildScene(graph: InsightsGraph, folds: FoldState): { scene: BalloonScene; visible: WordgraphNode[]; childCounts: Map<string, number> } {
  const childCounts = childCountsOf(graph.nodes);
  const expandedIds = folds.expandedIds(childCounts);
  const visible = visibleWordgraphNodes({ nodes: graph.nodes, expandedIds });
  const bridges = wordgraphBridges(visible).map(({ id, from, to, label }) => ({ id, from, to, label }));
  return { scene: { topics: graph.topics, nodes: visible, bridges, expandedIds, pendingIds: new Set(), childCounts }, visible, childCounts };
}
