// Adapted from jibbr (Stephen Blatt); MIT in whosaid.
import { describe, expect, test } from 'bun:test';
import { balloonThrowVelocity, stepBalloonPhysics } from '../src/balloonPhysics';
import type { PhysicsBalloon } from '../src/balloon-types';

function balloon(id: string, x: number, y = 0, homeX = x, homeY = y, depth = 1): PhysicsBalloon {
  return { id, topicId: 'topic', parentId: null, depth, radius: 10, x, y, vx: 0, vy: 0, homeX, homeY };
}

describe('balloon physics', () => {
  test('a thrown node overshoots and reverses through its tree spring, unlike an unsprung control', () => {
    const sprung = [balloon('a', 100), balloon('b', 0)];
    const control = [balloon('a', 100), balloon('b', 0)];
    sprung[0]!.vx = control[0]!.vx = 20;
    let overshot = false; let reversed = false;
    for (let tick = 0; tick < 30; tick++) {
      stepBalloonPhysics(sprung, [{ source: 'a', target: 'b', restLength: 100 }]);
      stepBalloonPhysics(control, []);
      overshot ||= sprung[0]!.x > 100;
      reversed ||= sprung[0]!.vx < 0;
    }
    expect(overshot).toBe(true); expect(reversed).toBe(true); expect(control[0]!.vx).toBeGreaterThan(0);
  });

  test('a held balloon tugs its spring neighbor', () => {
    const nodes = [balloon('held', 220), balloon('neighbor', 0)];
    stepBalloonPhysics(nodes, [{ source: 'held', target: 'neighbor', restLength: 100 }], { heldId: 'held' });
    expect(nodes[0]!.x).toBe(220); expect(nodes[1]!.vx).toBeGreaterThan(0);
  });

  test('stale releases produce no throw while fresh throws are vector capped', () => {
    expect(balloonThrowVelocity([{ x: 0, y: 0, t: 0 }, { x: 50, y: 0, t: 30 }], 130)).toEqual({ vx: 0, vy: 0 });
    const velocity = balloonThrowVelocity([{ x: 0, y: 0, t: 920 }, { x: 500, y: 500, t: 980 }], 1_000);
    expect(Math.hypot(velocity.vx, velocity.vy)).toBeLessThanOrEqual(24);
  });

  test('coincident balloons separate deterministically without producing non-finite state', () => {
    const nodes = [balloon('left', 0), balloon('right', 0)];
    stepBalloonPhysics(nodes, []);
    expect(Math.hypot(nodes[0]!.x - nodes[1]!.x, nodes[0]!.y - nodes[1]!.y)).toBeGreaterThan(0);
    for (const node of nodes) for (const value of [node.x, node.y, node.vx, node.vy]) expect(Number.isFinite(value)).toBe(true);
  });

  test('an 800-node scene with six spring spokes remains finite and speed bounded', () => {
    const nodes = Array.from({ length: 800 }, (_, index) => balloon(`n-${index}`, (index % 40) * 48, Math.floor(index / 40) * 48));
    const springs = Array.from({ length: 6 }, (_, index) => ({ source: 'n-0', target: `n-${index + 1}`, restLength: 80 }));
    nodes[0]!.x += 200; nodes[0]!.vx = 24;
    for (let tick = 0; tick < 40; tick++) stepBalloonPhysics(nodes, springs);
    for (const node of nodes) {
      expect(Number.isFinite(node.x) && Number.isFinite(node.y) && Number.isFinite(node.vx) && Number.isFinite(node.vy)).toBe(true);
      expect(Math.hypot(node.vx, node.vy)).toBeLessThanOrEqual(24);
      expect(Math.abs(node.x)).toBeLessThanOrEqual(100_000);
      expect(Math.abs(node.y)).toBeLessThanOrEqual(100_000);
    }
  });

  test('energy settles after sufficient fixed ticks', () => {
    const nodes = [balloon('root', 360, 0, 0, 0, 0)];
    let energy = Infinity;
    for (let tick = 0; tick < 1_000; tick++) energy = stepBalloonPhysics(nodes, []);
    expect(energy).toBeLessThan(0.02);
    expect(Math.abs(nodes[0]!.x)).toBeLessThan(2);
  });
});
