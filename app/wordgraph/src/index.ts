// whosaid wordgraph page: wires the jibbr balloon world to the app's JS bridge.
import { createBalloonWorld } from './balloonWorld';
import type { BalloonController, BalloonMotion } from './balloon-types';
import { demoInsights } from './demo';
import { buildScene, FoldState, readInsights, type InsightsGraph } from './scene';
import { WORDGRAPH_COLORS } from './wordgraph-shared';

type Message =
  | { type: 'ready' }
  | { type: 'click'; id: string; label: string; topicId: string; depth: number }
  | { type: 'fold'; id: string; expanded: boolean }
  | { type: 'fps'; fps: number | null; nodes: number };

declare global {
  interface Window {
    webkit?: { messageHandlers?: { whosaid?: { postMessage(message: unknown): void } } };
    whosaidWordgraph?: {
      setInsights(obj: unknown): void;
      setMotion(motion: BalloonMotion): void;
      fit(): void;
      focusTopic(id: string): void;
      expandAll(on: boolean): void;
      shake(): void;
      setMaxDepth(depth: number): void;
      state(): { running: boolean; motion: string; nodes: number; speed: number; camera: number };
    };
  }
}

function post(message: Message) {
  try { window.webkit?.messageHandlers?.whosaid?.postMessage(message); } catch { /* no host */ }
}

function element<K extends keyof HTMLElementTagNameMap>(tag: K, className: string, text?: string) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function start() {
  const root = element('div', 'wordgraph wordgraph--full');
  const viewport = element('div', 'wordgraph-balloon-viewport');
  viewport.setAttribute('role', 'group');
  viewport.setAttribute('aria-label', 'Topic balloons. Click a balloon to fold or unfold it; drag to throw it; drag the background to pan.');
  const stage = element('div', 'wordgraph-balloon-stage');
  const hint = element('p', 'wordgraph-floating-hint', 'Topics appear here as the call goes on');
  const footer = element('footer', 'wordgraph-canvas-footer');
  const legend = element('aside', 'wordgraph-legend');
  legend.setAttribute('aria-label', 'Colors indicate depth');
  ['Topic', 'Idea', 'Detail'].forEach((label, depth) => {
    const item = element('span', '');
    const dot = element('i', '');
    dot.style.setProperty('--dot', WORDGRAPH_COLORS[depth]!);
    item.append(dot, label);
    legend.append(item);
  });
  legend.append(element('span', 'wordgraph-shared-key', 'Shared term'));
  footer.append(legend);
  viewport.append(stage, hint, footer);
  root.append(viewport);
  document.body.append(root);

  let graph: InsightsGraph = { topics: [], nodes: [] };
  const folds = new FoldState();
  let current = buildScene(graph, folds);
  let controller: BalloonController | undefined;
  let selected: string | undefined;

  // A narrow lane shows topics and ideas only, for the top LANE_TOPICS topics;
  // the full window shows every topic at every depth.
  const LANE_TOPICS = 6;
  let maxDepth = Number.POSITIVE_INFINITY;
  const shown = (): InsightsGraph => {
    if (!Number.isFinite(maxDepth)) return graph;
    const top = new Set([...graph.topics].sort((a, b) => a.rank - b.rank).slice(0, LANE_TOPICS).map(t => t.id));
    return { topics: graph.topics.filter(t => top.has(t.id)),
             nodes: graph.nodes.filter(node => node.depth <= maxDepth && top.has(node.topicId)) };
  };
  const refresh = () => {
    current = buildScene(shown(), folds);
    hint.hidden = current.visible.length > 0;
    controller?.update(current.scene);
  };

  const choose = (id: string) => {
    const node = graph.nodes.find(item => item.id === id);
    if (!node) return;
    post({ type: 'click', id: node.id, label: node.label, topicId: node.topicId, depth: node.depth });
    selected = id;
    controller?.select(id);
    const expanded = folds.toggle(id, current.childCounts);
    if (expanded === undefined) return;
    post({ type: 'fold', id, expanded });
    refresh();
  };

  controller = createBalloonWorld(stage, current.scene, { allowWheelZoom: true, onSelect: id => choose(id) });
  hint.hidden = false;

  window.whosaidWordgraph = {
    setInsights(obj) {
      const wasEmpty = current.visible.length === 0, before = current.visible.length;
      graph = readInsights(obj);
      refresh();
      if (selected) controller?.select(selected);
      if (wasEmpty && current.visible.length > 0) controller?.fit();
      else if (current.visible.length !== before) controller?.refit();  // new nodes stay in view
    },
    setMotion(motion) {
      if (motion === 'lively' || motion === 'calm' || motion === 'paused') controller?.setMotion(motion);
    },
    fit() { controller?.fit(); },
    focusTopic(id) {
      const topic = graph.topics.find(item => item.id === id);
      const rootNode = graph.nodes.find(node => node.topicId === id && node.depth === 0);
      const rootId = rootNode?.id ?? topic?.rootId;
      if (rootId && folds.collapsed.has(rootId)) {
        folds.toggle(rootId, current.childCounts);
        post({ type: 'fold', id: rootId, expanded: true });
        refresh();
      }
      controller?.focusTopic(id);
    },
    expandAll(on) {
      folds.expandAll(Boolean(on), current.childCounts);
      refresh();
    },
    shake() { controller?.shake(); },
    setMaxDepth(depth) {
      const next = Number.isFinite(depth) && depth >= 0 ? Math.floor(depth) : Number.POSITIVE_INFINITY;
      if (next === maxDepth) return;
      maxDepth = next;
      refresh();
      controller?.fit();
    },
    state() {
      const snap = controller?.snapshot();
      const nodes = snap?.nodes ?? [];
      const speed = nodes.reduce((sum, n) => sum + Math.abs(n.vx) + Math.abs(n.vy), 0) / Math.max(1, nodes.length);
      return { running: snap?.running ?? false, motion: snap?.motion ?? 'paused', nodes: nodes.length, speed, camera: snap?.camera.k ?? 0 };
    },
  };

  // fps counts only seconds when the balloons are moving; while the world is idle the
  // counter drops its own frame loop too and checks once a second (fps: null).
  let frames = 0;
  let since = performance.now();
  let looping = false;
  const running = () => controller?.snapshot().running ?? false;
  const tick = (now: number) => {
    frames++;
    if (now - since >= 1000) {
      // A second that ended idle was partly idle: report it as idle, not as a low fps.
      const moving = running();
      post({ type: 'fps', fps: moving ? Math.round(frames * 1000 / (now - since)) : null, nodes: current.visible.length });
      frames = 0;
      since = now;
      if (!moving) { looping = false; idle(); return; }
    }
    requestAnimationFrame(tick);
  };
  const loop = () => {
    if (looping) return;
    looping = true; frames = 0; since = performance.now();
    requestAnimationFrame(tick);
  };
  const idle = () => setTimeout(() => (running() ? loop() : idle()), 1000);
  loop();

  if (new URLSearchParams(location.search).has('demo')) window.whosaidWordgraph.setInsights(demoInsights());
  post({ type: 'ready' });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
else start();
