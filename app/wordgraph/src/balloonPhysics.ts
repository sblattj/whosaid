// Adapted from jibbr (Stephen Blatt); MIT in whosaid.
import type { BalloonDragSample, BalloonMotion, BalloonSpring, PhysicsBalloon } from './balloon-types';

const SPRING_STRENGTH = 0.01;
const ROOT_HOME_STRENGTH = 0.0005;
const CHILD_HOME_STRENGTH = 0.00015;
const COLLISION_GAP = 10;
const COLLISION_STRENGTH = 0.075;
const MAX_SPEED = 24;
const MAX_POSITION = 100_000;
const TICK_MS = 16.667;

type PhysicsOptions = { heldId?: string; motion?: Exclude<BalloonMotion, 'paused'>; step?: number };

function finite(value: number, fallback = 0): number {
  return Number.isFinite(value) ? value : fallback;
}

function clampVector(x: number, y: number, maximum: number): [number, number] {
  const length = Math.hypot(x, y);
  // Leave a minute margin so binary rounding cannot return a speed over the cap.
  const capped = maximum * (1 - 1e-12);
  return length > maximum ? [x * capped / length, y * capped / length] : [x, y];
}

function collisionDirection(first: string, second: string): [number, number] {
  let hash = 2166136261;
  for (const character of `${first}\u0000${second}`) hash = Math.imul(hash ^ character.charCodeAt(0), 16777619);
  const angle = (hash >>> 0) / 0x1_0000_0000 * Math.PI * 2;
  return [Math.cos(angle), Math.sin(angle)];
}

/** Advances one fixed 16.667ms physics tick (or a bounded multiple of one). */
export function stepBalloonPhysics(nodes: PhysicsBalloon[], springs: BalloonSpring[], options: PhysicsOptions = {}): number {
  const motion = options.motion === 'calm' ? 'calm' : 'lively';
  const step = Math.min(4, Math.max(0, finite(options.step ?? 1, 1)));
  if (step === 0 || nodes.length === 0) return nodes.reduce((energy, node) => energy + Math.abs(finite(node.vx)) + Math.abs(finite(node.vy)), 0);

  const forceScale = motion === 'calm' ? 0.42 : 1;
  const damping = Math.pow(motion === 'calm' ? 0.89 : 0.96, step);
  const byId = new Map<string, number>();
  const forces = nodes.map(() => ({ x: 0, y: 0 }));
  let largestRadius = 1;

  nodes.forEach((node, index) => {
    node.radius = Math.max(1, finite(node.radius, 1));
    node.x = Math.max(-MAX_POSITION, Math.min(MAX_POSITION, finite(node.x)));
    node.y = Math.max(-MAX_POSITION, Math.min(MAX_POSITION, finite(node.y)));
    node.vx = finite(node.vx);
    node.vy = finite(node.vy);
    node.homeX = Math.max(-MAX_POSITION, Math.min(MAX_POSITION, finite(node.homeX, node.x)));
    node.homeY = Math.max(-MAX_POSITION, Math.min(MAX_POSITION, finite(node.homeY, node.y)));
    largestRadius = Math.max(largestRadius, node.radius);
    byId.set(node.id, index);
  });

  // Shared-term bridges never enter this input: each entry is a physical tree edge.
  for (const spring of springs) {
    const sourceIndex = byId.get(spring.source);
    const targetIndex = byId.get(spring.target);
    if (sourceIndex === undefined || targetIndex === undefined || sourceIndex === targetIndex) continue;
    const source = nodes[sourceIndex]!;
    const target = nodes[targetIndex]!;
    let dx = target.x - source.x;
    let dy = target.y - source.y;
    let length = Math.hypot(dx, dy);
    if (length < 0.0001) {
      [dx, dy] = collisionDirection(source.id, target.id);
      length = 1;
    }
    const power = (length - Math.max(0, finite(spring.restLength))) * SPRING_STRENGTH * forceScale;
    const fx = dx / length * power;
    const fy = dy / length * power;
    if (source.id !== options.heldId) { forces[sourceIndex]!.x += fx; forces[sourceIndex]!.y += fy; }
    if (target.id !== options.heldId) { forces[targetIndex]!.x -= fx; forces[targetIndex]!.y -= fy; }
  }

  const cellSize = largestRadius * 2 + COLLISION_GAP;
  const cells = new Map<string, number[]>();
  nodes.forEach((node, index) => {
    const key = `${Math.floor(node.x / cellSize)},${Math.floor(node.y / cellSize)}`;
    const cell = cells.get(key);
    if (cell) cell.push(index); else cells.set(key, [index]);
  });
  for (let index = 0; index < nodes.length; index++) {
    const node = nodes[index]!;
    const cellX = Math.floor(node.x / cellSize);
    const cellY = Math.floor(node.y / cellSize);
    for (let offsetX = -1; offsetX <= 1; offsetX++) for (let offsetY = -1; offsetY <= 1; offsetY++) {
      for (const otherIndex of cells.get(`${cellX + offsetX},${cellY + offsetY}`) ?? []) {
        if (otherIndex <= index) continue;
        const other = nodes[otherIndex]!;
        let dx = other.x - node.x;
        let dy = other.y - node.y;
        let length = Math.hypot(dx, dy);
        const minimum = node.radius + other.radius + COLLISION_GAP;
        if (length >= minimum) continue;
        if (length < 0.0001) { [dx, dy] = collisionDirection(node.id, other.id); length = 1; }
        const push = (minimum - length) * COLLISION_STRENGTH * forceScale;
        const fx = dx / length * push;
        const fy = dy / length * push;
        if (node.id !== options.heldId) { forces[index]!.x -= fx; forces[index]!.y -= fy; }
        if (other.id !== options.heldId) { forces[otherIndex]!.x += fx; forces[otherIndex]!.y += fy; }
      }
    }
  }

  let energy = 0;
  nodes.forEach((node, index) => {
    if (node.id === options.heldId) { energy += Math.abs(node.vx) + Math.abs(node.vy); return; }
    const homeStrength = node.depth === 0 ? ROOT_HOME_STRENGTH : CHILD_HOME_STRENGTH;
    const homeX = (node.homeX - node.x) * homeStrength * forceScale;
    const homeY = (node.homeY - node.y) * homeStrength * forceScale;
    const [forceX, forceY] = clampVector(forces[index]!.x + homeX, forces[index]!.y + homeY, MAX_SPEED);
    const [vx, vy] = clampVector((node.vx + forceX * step) * damping, (node.vy + forceY * step) * damping, MAX_SPEED);
    node.vx = vx; node.vy = vy;
    node.x = Math.max(-MAX_POSITION, Math.min(MAX_POSITION, node.x + vx * step));
    node.y = Math.max(-MAX_POSITION, Math.min(MAX_POSITION, node.y + vy * step));
    energy += Math.abs(vx) + Math.abs(vy);
  });
  return energy;
}

/** Converts the most recent drag trajectory into velocity per physics tick. */
export function balloonThrowVelocity(samples: BalloonDragSample[], timestamp: number): { vx: number; vy: number } {
  const now = finite(timestamp);
  const recent = samples.filter(sample => Number.isFinite(sample.x) && Number.isFinite(sample.y) && Number.isFinite(sample.t) && sample.t <= now && now - sample.t <= 80)
    .sort((left, right) => left.t - right.t);
  const last = recent.at(-1);
  const first = recent[0];
  if (!first || !last || now - last.t >= 100 || last.t <= first.t) return { vx: 0, vy: 0 };
  const [vx, vy] = clampVector((last.x - first.x) / (last.t - first.t) * TICK_MS, (last.y - first.y) / (last.t - first.t) * TICK_MS, MAX_SPEED);
  return { vx, vy };
}
