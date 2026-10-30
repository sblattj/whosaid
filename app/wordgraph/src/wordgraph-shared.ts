// Adapted from jibbr (Stephen Blatt); MIT in whosaid.
// The constants and normalizer are verbatim; the zod schemas became plain types.

export const WORDGRAPH_MAX_DEPTH = 7;
export const WORDGRAPH_MAX_CHILDREN = 6;
export const WORDGRAPH_MAX_NODES = 800;
export const WORDGRAPH_COLORS = ['#ffdb61', '#89caff', '#96e7b9', '#c6a3f2', '#ffba83', '#fa99b7', '#5ce1e6', '#a3d977'] as const;
export function normalizeWordgraphTerm(label: string): string {
  return label.normalize('NFKC').trim().replace(/\s+/gu, ' ').toLowerCase();
}
export interface WordgraphEvidence {
  segmentId: number; segmentSeq: number; quote: string;
  t0: number | null; t1: number | null; speaker: string | null;
}
export interface WordgraphNode {
  id: string; topicId: string; parentId: string | null; depth: number;
  label: string; description: string; source: 'topic' | 'meeting' | 'related'; evidence: WordgraphEvidence[];
}
export interface WordgraphTopic {
  id: string; rootId: string; label: string; rank: number;
  speakingMs: number | null; visits: number; share: number;
}
