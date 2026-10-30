// Adapted from jibbr (Stephen Blatt); MIT in whosaid.
import type { WordgraphNode, WordgraphTopic } from './wordgraph-shared';

export type BalloonMotion = 'lively' | 'calm' | 'paused';
export interface BalloonBridge { id: string; from: string; to: string; label: string; }
export interface BalloonScene {
  topics: WordgraphTopic[];
  nodes: WordgraphNode[];
  bridges: BalloonBridge[];
  expandedIds: Set<string>;
  pendingIds: Set<string>;
  childCounts: Map<string, number>;
}
export interface PhysicsBalloon {
  id: string; topicId: string; parentId: string | null; depth: number;
  radius: number; x: number; y: number; vx: number; vy: number; homeX: number; homeY: number;
}
export interface BalloonSpring { source: string; target: string; restLength: number; }
export interface BalloonDragSample { x: number; y: number; t: number; }
export interface BalloonController {
  update(scene: BalloonScene): void;
  select(id: string | undefined): void;
  focusTopic(topicId: string): void;
  zoomBy(factor: number): void;
  fit(): void;
  reset(): void;
  shake(): void;
  setMotion(motion: BalloonMotion): void;
  snapshot(): { nodes: PhysicsBalloon[]; camera: { x: number; y: number; k: number }; running: boolean; motion: BalloonMotion; dragging: string | null };
  destroy(): void;
}
