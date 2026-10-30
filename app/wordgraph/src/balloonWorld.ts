// Adapted from jibbr (Stephen Blatt); MIT in whosaid.
import { WORDGRAPH_COLORS, type WordgraphNode } from "./wordgraph-shared";
import { balloonThrowVelocity, stepBalloonPhysics } from "./balloonPhysics";
import type {
  BalloonController,
  BalloonMotion,
  BalloonScene,
  BalloonSpring,
  PhysicsBalloon,
} from "./balloon-types";

const MIN_ZOOM = 0.01,
  MAX_ZOOM = 8,
  TICK = 16.667;
type Point = { x: number; y: number };
type RenderNode = PhysicsBalloon & {
  label: string;
  source: WordgraphNode["source"];
  button: HTMLButtonElement;
  lines: string[];
  born: number;
};
type Camera = {
  x: number;
  y: number;
  k: number;
  targetX: number;
  targetY: number;
  targetK: number;
};

export function wrapBalloonLabel(
  label: string,
  measure: (text: string) => number,
  maxWidth = 150,
): string[] {
  const words = label.trim().split(/\s+/u).filter(Boolean);
  if (!words.length) return [""];
  const split = (value: string) => {
    const pieces: string[] = [];
    let piece = "";
    for (const char of value) {
      if (piece && measure(piece + char) > maxWidth) {
        pieces.push(piece);
        piece = char;
      } else piece += char;
    }
    if (piece) pieces.push(piece);
    return pieces;
  };
  const lines: string[] = [];
  let line = "";
  for (const word of words) {
    const candidate = line ? `${line} ${word}` : word;
    if (line && measure(candidate) > maxWidth) {
      lines.push(...split(line));
      line = word;
    } else line = candidate;
  }
  if (line) lines.push(...split(line));
  return lines;
}

export function balloonRadius(
  node: WordgraphNode,
  share: number | undefined,
  lines: string[],
  measure: (text: string) => number,
): number {
  const fontScale = node.depth === 0 ? 21 / 16 : 1;
  const textWidth = Math.max(
      ...lines.map((line) => measure(line) * fontScale),
      0,
    ),
    textHeight = Math.max(
      node.depth === 0 ? 26 : 20,
      lines.length * (node.depth === 0 ? 26 : 20),
    );
  const legible = Math.hypot(textWidth / 2 + 18, textHeight / 2 + 18);
  const root =
    node.depth === 0 ? 60 + Math.sqrt(Math.max(0, share ?? 0)) * 35 : 55;
  return Math.ceil(Math.max(root, legible));
}

export function createBalloonWorld(
  stage: HTMLDivElement,
  initial: BalloonScene,
  options: {
    onSelect: (id: string) => void;
    onCamera?: (zoom: number) => void;
    onDrag?: (id: string, dragging: boolean) => void;
    onIntent?: (id: string) => void;
    allowWheelZoom: boolean;
  },
): BalloonController {
  let scene = initial,
    nodes: RenderNode[] = [],
    selected: string | undefined,
    width = 0,
    height = 0,
    measurable = false,
    visible = !document.hidden,
    intersecting = true,
    destroyed = false,
    fitted = false,
    userCamera = false,
    requestedTopic: string | undefined,
    motion: BalloonMotion = "lively",
    raf: number | undefined,
    previousTime = 0,
    accumulator = 0,
    energy = Infinity;
  let physicalSprings: BalloonSpring[] = [];
  const camera: Camera = {
    x: 0,
    y: 0,
    k: 1,
    targetX: 0,
    targetY: 0,
    targetK: 1,
  };
  const viewport = document.createElement("div");
  viewport.className = "wordgraph-balloon-viewport";
  const world = document.createElement("div");
  world.className = "wordgraph-balloon-world";
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "wordgraph-balloon-links");
  const nodeLayer = document.createElement("div");
  nodeLayer.className = "wordgraph-balloon-nodes";
  const paths = new Map<
    string,
    { path: SVGPathElement; from: string; to: string }
  >();
  viewport.append(world);
  world.append(svg, nodeLayer);
  stage.replaceChildren(viewport);
  const reduceQuery = window.matchMedia?.("(prefers-reduced-motion: reduce)");
  let reduced = Boolean(reduceQuery?.matches);
  const pointers = new Map<number, Point>();
  let pinch:
    | { d: number; mid: Point; k: number; x: number; y: number }
    | undefined;
  let drag:
    | {
        id?: string;
        x: number;
        y: number;
        startX: number;
        startY: number;
        offsetX: number;
        offsetY: number;
        moved: boolean;
        cancelled: boolean;
        samples: Array<{ x: number; y: number; t: number }>;
      }
    | undefined;
  let intentTimer: ReturnType<typeof setTimeout> | undefined,
    intentId: string | undefined;
  const canRender = () => !destroyed && measurable && visible && intersecting;
  const canAnimate = () => canRender() && !reduced && motion !== "paused";
  const local = (event: { clientX: number; clientY: number }) => {
    const box = stage.getBoundingClientRect();
    return { x: event.clientX - box.left, y: event.clientY - box.top };
  };
  const worldPoint = (point: Point) => ({
    x: (point.x - camera.x) / camera.k,
    y: (point.y - camera.y) / camera.k,
  });
  const measure = (() => {
    const canvas = document.createElement("canvas");
    const context = canvas.getContext?.("2d");
    if (context) context.font = "600 16px ui-sans-serif, system-ui, sans-serif";
    return (text: string) =>
      context?.measureText(text).width ?? [...text].length * 9;
  })();
  const topicShares = () =>
    new Map(scene.topics.map((topic) => [topic.id, topic.share]));
  const byId = () => new Map(nodes.map((node) => [node.id, node]));
  const color = (depth: number) =>
    WORDGRAPH_COLORS[Math.min(depth, WORDGRAPH_COLORS.length - 1)]!;
  const clearIntent = () => {
    if (intentTimer) clearTimeout(intentTimer);
    intentTimer = undefined;
    intentId = undefined;
  };
  const scheduleIntent = (id: string | undefined) => {
    clearIntent();
    if (!id || !options.onIntent) return;
    const node = scene.nodes.find((item) => item.id === id);
    if (
      !node ||
      node.depth >= 7 ||
      scene.expandedIds.has(id) ||
      scene.pendingIds.has(id) ||
      scene.childCounts.has(id)
    )
      return;
    intentId = id;
    intentTimer = setTimeout(() => {
      const current = intentId;
      clearIntent();
      if (current) options.onIntent?.(current);
    }, 200);
  };
  const buttonFor = (node: WordgraphNode, lines: string[]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "wordgraph-balloon";
    button.dataset.node = node.id;
    button.dataset.depth = String(node.depth);
    button.dataset.source = node.source;
    button.setAttribute("aria-label", node.label);
    button.setAttribute(
      "aria-expanded",
      String(scene.expandedIds.has(node.id)),
    );
    button.setAttribute("aria-busy", String(scene.pendingIds.has(node.id)));
    const label = document.createElement("span");
    label.className = "wordgraph-balloon-label";
    label.textContent = lines.join("\n");
    button.append(label);
    const badgeValue = scene.pendingIds.has(node.id)
      ? "…"
      : scene.expandedIds.has(node.id)
        ? "−"
        : node.depth >= 7
          ? "•"
          : scene.childCounts.get(node.id) === undefined
            ? "+"
            : scene.childCounts.get(node.id)! > 0
              ? `+${scene.childCounts.get(node.id)}`
              : "•";
    if (badgeValue) {
      const badge = document.createElement("span");
      badge.className = "wordgraph-balloon-badge";
      badge.textContent = badgeValue;
      button.append(badge);
    }
    button.addEventListener("click", (event) => {
      if ((event as MouseEvent).detail === 0) options.onSelect(node.id);
    });
    return button;
  };
  const roots = (input: BalloonScene) =>
    input.nodes
      .filter((node) => !node.parentId)
      .sort(
        (a, b) =>
          (input.topics.find((topic) => topic.id === a.topicId)?.rank ?? 999) -
          (input.topics.find((topic) => topic.id === b.topicId)?.rank ?? 999),
      );
  const initialPosition = (
    node: WordgraphNode,
    input: BalloonScene,
    current: Map<string, RenderNode>,
  ) => {
    const rootNodes = roots(input),
      rootIndex = rootNodes.findIndex(
        (item) => item.id === node.id || item.topicId === node.topicId,
      ),
      count = Math.max(1, rootNodes.length),
      columns = Math.ceil(Math.sqrt(count));
    if (!node.parentId)
      return {
        x: (rootIndex % columns) * 460,
        y: Math.floor(rootIndex / columns) * 390,
      };
    const parent = current.get(node.parentId);
    const grandparent = parent?.parentId
      ? current.get(parent.parentId)
      : undefined;
    const siblings = input.nodes
      .filter((item) => item.parentId === node.parentId)
      .sort((a, b) => a.id.localeCompare(b.id));
    const index = siblings.findIndex((item) => item.id === node.id);
    const outward = grandparent
      ? Math.atan2(parent!.y - grandparent.y, parent!.x - grandparent.x)
      : node.depth * 0.55;
    const spread = Math.min(
      Math.PI * 1.35,
      Math.max(0.55, siblings.length * 0.46),
    );
    const angle =
      outward +
      (index - (siblings.length - 1) / 2) *
        (spread / Math.max(1, siblings.length - 1));
    const distance = 135 + node.depth * 65;
    return {
      x: (parent?.x ?? 0) + Math.cos(angle) * distance,
      y: (parent?.y ?? 0) + Math.sin(angle) * distance,
    };
  };
  const syncScene = (next: BalloonScene) => {
    const existing = new Map(nodes.map((node) => [node.id, node]));
    const shares = new Map(next.topics.map((topic) => [topic.id, topic.share]));
    const positioned = new Map(existing);
    const visibleIds = new Set(next.nodes.map((node) => node.id));
    // whosaid: set before new buttons are built, so a node that arrives open shows "−", not "+".
    scene = next;
    nodes = [...next.nodes]
      .sort((a, b) => a.depth - b.depth || a.id.localeCompare(b.id))
      .map((node) => {
        const lines = wrapBalloonLabel(
          node.label,
          measure,
          node.depth === 0 ? 170 : 110,
        );
        const radius = balloonRadius(
          node,
          shares.get(node.topicId),
          lines,
          measure,
        );
        const prior = existing.get(node.id);
        const start = prior ?? initialPosition(node, next, positioned);
        const rendered = prior ?? {
          id: node.id,
          topicId: node.topicId,
          parentId: node.parentId,
          depth: node.depth,
          x: start.x,
          y: start.y,
          vx: 0,
          vy: 0,
          homeX: start.x,
          homeY: start.y,
          radius,
          label: node.label,
          source: node.source,
          lines,
          born: performance.now(),
          button: buttonFor(node, lines),
        };
        rendered.topicId = node.topicId;
        rendered.parentId = node.parentId;
        rendered.depth = node.depth;
        rendered.radius = radius;
        rendered.label = node.label;
        rendered.source = node.source;
        rendered.lines = lines;
        if (prior) {
          rendered.button.replaceChildren();
          const label = document.createElement("span");
          label.className = "wordgraph-balloon-label";
          label.textContent = lines.join("\n");
          rendered.button.append(label);
          const value = next.pendingIds.has(node.id)
            ? "…"
            : next.expandedIds.has(node.id)
              ? "−"
              : node.depth >= 7
                ? "•"
                : next.childCounts.get(node.id) === undefined
                  ? "+"
                  : next.childCounts.get(node.id)! > 0
                    ? `+${next.childCounts.get(node.id)}`
                    : "•";
          const badge = document.createElement("span");
          badge.className = "wordgraph-balloon-badge";
          badge.textContent = value;
          rendered.button.append(badge);
        }
        rendered.button.dataset.depth = String(node.depth);
        rendered.button.dataset.source = node.source;
        rendered.button.setAttribute("aria-label", node.label);
        rendered.button.setAttribute(
          "aria-expanded",
          String(next.expandedIds.has(node.id)),
        );
        rendered.button.setAttribute(
          "aria-busy",
          String(next.pendingIds.has(node.id)),
        );
        positioned.set(node.id, rendered);
        return rendered;
      });
    for (const node of nodes)
      if (node.button.parentElement !== nodeLayer)
        nodeLayer.append(node.button);
    for (const button of [...nodeLayer.children])
      if (!nodes.some((node) => node.button === button)) button.remove();
    const edges = [
      ...nodes
        .filter((node) => node.parentId)
        .map((node) => ({
          key: `tree:${node.parentId}:${node.id}`,
          from: node.parentId!,
          to: node.id,
          shared: false,
        })),
      ...next.bridges.map((bridge) => ({
        key: `shared:${bridge.id}`,
        from: bridge.from,
        to: bridge.to,
        shared: true,
      })),
    ];
    const keys = new Set(edges.map((edge) => edge.key));
    for (const [key, edge] of paths)
      if (!keys.has(key)) {
        edge.path.remove();
        paths.delete(key);
      }
    for (const edge of edges)
      if (!paths.has(edge.key)) {
        const path = document.createElementNS(
          "http://www.w3.org/2000/svg",
          "path",
        );
        path.setAttribute(
          "class",
          `wordgraph-balloon-link${edge.shared ? " is-shared" : ""}`,
        );
        svg.append(path);
        paths.set(edge.key, { path, from: edge.from, to: edge.to });
      }
    if (drag?.id && !visibleIds.has(drag.id)) {
      options.onDrag?.(drag.id, false);
      drag = undefined;
    }
    if (selected && !nodes.some((node) => node.id === selected))
      selected = undefined;
    const lookup = new Map(nodes.map((node) => [node.id, node]));
    physicalSprings = nodes
      .filter((node) => node.parentId && lookup.has(node.parentId))
      .map((node) => ({
        source: node.parentId!,
        target: node.id,
        restLength:
          node.radius + (lookup.get(node.parentId!)?.radius ?? 40) + 48,
      }));
    if (reduced) settle();
    else if (motion === "paused") zeroVelocities();
    else energy = Infinity;
    render();
  };
  const zeroVelocities = () => {
    for (const node of nodes) node.vx = node.vy = 0;
    energy = 0;
  };
  const settle = () => {
    for (let tick = 0; tick < 90; tick++)
      stepBalloonPhysics(nodes, physicalSprings, { motion: "calm", step: 0.8 });
    zeroVelocities();
  };
  const snapCamera = () => {
    Object.assign(camera, {
      k: camera.targetK,
      x: camera.targetX,
      y: camera.targetY,
    });
  };
  const render = () => {
    world.style.transform = `translate(${camera.x}px, ${camera.y}px) scale(${camera.k})`;
    const lookup = byId();
    for (const edge of paths.values()) {
      const from = lookup.get(edge.from),
        to = lookup.get(edge.to);
      edge.path.setAttribute(
        "d",
        from && to ? `M ${from.x} ${from.y} L ${to.x} ${to.y}` : "",
      );
    }
    const now = performance.now();
    for (const node of nodes) {
      const age = now - node.born;
      const rawPop =
        reduced || motion === "paused" ? 1 : Math.min(1, age / 280);
      const pop = 1 - (1 - rawPop) ** 3;
      const squash =
        reduced || motion === "paused"
          ? 0
          : Math.min(0.08, Math.hypot(node.vx, node.vy) / 300);
      node.button.classList.toggle("is-selected", node.id === selected);
      node.button.classList.toggle("is-pending", scene.pendingIds.has(node.id));
      node.button.style.setProperty("--balloon-color", color(node.depth));
      node.button.style.width = `${node.radius * 2}px`;
      node.button.style.height = `${node.radius * 2}px`;
      node.button.style.transform = `translate(${node.x - node.radius}px, ${node.y - node.radius}px) scale(${pop * (1 + squash)}, ${pop * (1 - squash)})`;
    }
  };
  const fitNodes = (items: RenderNode[]) => {
    if (!items.length || !measurable) return;
    const left = Math.min(...items.map((n) => n.x - n.radius)),
      right = Math.max(...items.map((n) => n.x + n.radius)),
      top = Math.min(...items.map((n) => n.y - n.radius)),
      bottom = Math.max(...items.map((n) => n.y + n.radius));
    const sidePad = Math.min(70, width * 0.12);
    // short views (a topic's graph above its meetings) cap the pads by height; tall ones are unchanged
    const topPad = Math.min(Math.max(50, height * 0.08), height * 0.12);
    const bottomPad = Math.min(
      Math.max(width < 600 ? 150 : 110, height * 0.16),
      Math.max(84, height * 0.25),
    );
    const k = Math.max(
      MIN_ZOOM,
      Math.min(
        1.6,
        Math.min(
          (width - sidePad * 2) / Math.max(1, right - left),
          (height - topPad - bottomPad) / Math.max(1, bottom - top),
        ),
      ),
    );
    camera.targetK = k;
    camera.targetX = width / 2 - ((left + right) / 2) * k;
    camera.targetY =
      (topPad + height - bottomPad) / 2 - ((top + bottom) / 2) * k;
    if (!canAnimate()) {
      snapCamera();
      render();
    }
    options.onCamera?.(k);
    requestFrame();
  };
  const focus = () => {
    if (!requestedTopic || !measurable) return;
    const items = nodes.filter((node) => node.topicId === requestedTopic);
    if (!items.length) return;
    fitNodes(items);
    const root = items.find((node) => node.depth === 0);
    if (width < 600 && root && camera.targetK < 0.8) {
      camera.targetK = 0.8;
      camera.targetX = width * 0.35 - root.x * 0.8;
      camera.targetY = height * 0.42 - root.y * 0.8;
      options.onCamera?.(camera.targetK);
    }
    if (!canAnimate()) {
      snapCamera();
      render();
    }
    requestedTopic = undefined;
    userCamera = true;
  };
  const stopFrame = () => {
    if (raf) cancelAnimationFrame(raf);
    raf = undefined;
    previousTime = 0;
    accumulator = 0;
  };
  const requestFrame = () => {
    if (
      !raf &&
      canAnimate() &&
      // whosaid: settle on the mean speed, not the sum; a live call has 100+ balloons and a
      // summed 0.02 is never reached. Lively keeps a ~0.12 px/tick jiggle where springs and
      // collisions disagree, so it settles below 0.15: the page then stops drawing until a
      // drag, a throw or new nodes wake it, instead of 60 fps for the whole call.
      (energy > (motion === "calm" ? 0.02 : 0.15) * Math.max(1, nodes.length) ||
        nodes.some((node) => performance.now() - node.born < 550) ||
        Math.abs(camera.k - camera.targetK) > 0.001 ||
        Math.abs(camera.x - camera.targetX) > 0.1 ||
        Math.abs(camera.y - camera.targetY) > 0.1)
    )
      raf = requestAnimationFrame(frame);
  };
  const frame = (time: number) => {
    raf = undefined;
    if (!canAnimate()) {
      previousTime = 0;
      accumulator = 0;
      return;
    }
    const elapsed = previousTime ? Math.min(67, time - previousTime) : TICK;
    previousTime = time;
    accumulator += elapsed;
    let steps = 0;
    while (accumulator >= TICK && steps++ < 4) {
      energy = stepBalloonPhysics(nodes, physicalSprings, {
        heldId: drag?.id,
        motion: motion === "calm" ? "calm" : "lively",
      });
      accumulator -= TICK;
    }
    camera.k += (camera.targetK - camera.k) * 0.2;
    camera.x += (camera.targetX - camera.x) * 0.2;
    camera.y += (camera.targetY - camera.y) * 0.2;
    render();
    requestFrame();
  };
  const resize = () => {
    const rect = stage.getBoundingClientRect(),
      nextWidth = Math.round(rect.width),
      nextHeight = Math.round(rect.height);
    measurable = nextWidth > 0 && nextHeight > 0;
    if (!measurable) {
      stopFrame();
      return;
    }
    const changed = width !== nextWidth || height !== nextHeight;
    if (changed && fitted && userCamera) {
      camera.x += (nextWidth - width) / 2;
      camera.targetX += (nextWidth - width) / 2;
      camera.y += (nextHeight - height) / 2;
      camera.targetY += (nextHeight - height) / 2;
    }
    width = nextWidth;
    height = nextHeight;
    if (!fitted || (changed && !userCamera)) {
      fitNodes(nodes);
      fitted = true;
    }
    focus();
    render();
    requestFrame();
  };
  const panOrPinch = () => {
    if (!pinch || pointers.size < 2) return;
    const pair = [...pointers.values()],
      mid = {
        x: (pair[0]!.x + pair[1]!.x) / 2,
        y: (pair[0]!.y + pair[1]!.y) / 2,
      },
      d = Math.hypot(pair[1]!.x - pair[0]!.x, pair[1]!.y - pair[0]!.y);
    const k = Math.max(
        MIN_ZOOM,
        Math.min(MAX_ZOOM, (pinch.k * d) / Math.max(1, pinch.d)),
      ),
      wx = (pinch.mid.x - pinch.x) / pinch.k,
      wy = (pinch.mid.y - pinch.y) / pinch.k;
    camera.targetK = k;
    camera.targetX = mid.x - wx * k;
    camera.targetY = mid.y - wy * k;
    if (!canAnimate()) {
      snapCamera();
      render();
    }
    options.onCamera?.(k);
    requestFrame();
  };
  const onDown = (event: PointerEvent) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    clearIntent();
    const p = local(event);
    pointers.set(event.pointerId, p);
    stage.setPointerCapture?.(event.pointerId);
    if (pointers.size > 1) {
      const pair = [...pointers.values()];
      pinch = {
        d: Math.hypot(pair[1]!.x - pair[0]!.x, pair[1]!.y - pair[0]!.y),
        mid: {
          x: (pair[0]!.x + pair[1]!.x) / 2,
          y: (pair[0]!.y + pair[1]!.y) / 2,
        },
        k: camera.targetK,
        x: camera.targetX,
        y: camera.targetY,
      };
      if (drag?.id) options.onDrag?.(drag.id, false);
      drag = undefined;
      userCamera = true;
      return;
    }
    const target = (event.target as Element | null)?.closest?.(
      "button.wordgraph-balloon",
    ) as HTMLButtonElement | null;
    const id = target?.dataset.node;
    const held = id ? byId().get(id) : undefined;
    const point = worldPoint(p);
    drag = {
      id,
      x: p.x,
      y: p.y,
      startX: p.x,
      startY: p.y,
      offsetX: held ? held.x - point.x : 0,
      offsetY: held ? held.y - point.y : 0,
      moved: false,
      cancelled: false,
      samples: [],
    };
  };
  const onMove = (event: PointerEvent) => {
    const p = local(event);
    if (!pointers.has(event.pointerId)) {
      const target = (event.target as Element | null)?.closest?.(
        "button.wordgraph-balloon",
      ) as HTMLButtonElement | null;
      scheduleIntent(target?.dataset.node);
      return;
    }
    pointers.set(event.pointerId, p);
    if (pinch) {
      clearIntent();
      panOrPinch();
      return;
    }
    if (!drag) return;
    const dx = p.x - drag.x,
      dy = p.y - drag.y;
    drag.moved ||= Math.hypot(p.x - drag.startX, p.y - drag.startY) > 3;
    if (drag.moved) clearIntent();
    if (drag.id && drag.moved) {
      const held = byId().get(drag.id);
      if (held) {
        const next = worldPoint(p);
        held.x = next.x + drag.offsetX;
        held.y = next.y + drag.offsetY;
        held.vx = held.vy = 0;
        energy = Infinity;
        drag.samples.push({ x: held.x, y: held.y, t: event.timeStamp });
        drag.samples = drag.samples.filter(
          (sample) => event.timeStamp - sample.t <= 80,
        );
        options.onDrag?.(held.id, true);
      }
    } else if (drag.moved) {
      userCamera = true;
      camera.x = camera.targetX += dx;
      camera.y = camera.targetY += dy;
    }
    drag.x = p.x;
    drag.y = p.y;
    render();
    requestFrame();
  };
  const finish = (event: PointerEvent, valid: boolean) => {
    const held = drag;
    pointers.delete(event.pointerId);
    if (stage.hasPointerCapture?.(event.pointerId))
      stage.releasePointerCapture?.(event.pointerId);
    if (pointers.size < 2) pinch = undefined;
    if (!held) return;
    if (held.id && held.moved) {
      const node = byId().get(held.id);
      if (node) {
        if (valid && !held.cancelled && !reduced && motion !== "paused")
          Object.assign(
            node,
            balloonThrowVelocity(held.samples, event.timeStamp),
          );
        if (valid && !reduced && motion !== "paused") energy = Infinity;
        options.onDrag?.(held.id, false);
      }
    } else if (valid && held.id && !held.cancelled) options.onSelect(held.id);
    drag = undefined;
    requestFrame();
  };
  const onWheel = (event: WheelEvent) => {
    if (!options.allowWheelZoom) return;
    event.preventDefault();
    userCamera = true;
    const p = local(event),
      w = worldPoint(p),
      k = Math.max(
        MIN_ZOOM,
        Math.min(MAX_ZOOM, camera.targetK * Math.exp(-event.deltaY * 0.0016)),
      );
    camera.targetK = k;
    camera.targetX = p.x - w.x * k;
    camera.targetY = p.y - w.y * k;
    if (!canAnimate()) {
      snapCamera();
      render();
    }
    options.onCamera?.(k);
    requestFrame();
  };
  const onUp = (event: PointerEvent) => finish(event, true);
  const onCancel = (event: PointerEvent) => finish(event, false);
  const onVisibility = () => {
    visible = !document.hidden;
    if (canAnimate()) requestFrame();
    else stopFrame();
  };
  const onMotion = () => {
    reduced = Boolean(reduceQuery?.matches);
    if (reduced) settle();
    if (!canAnimate()) {
      snapCamera();
      render();
      stopFrame();
    }
    requestFrame();
  };
  const onFocus = (event: FocusEvent) => {
    const id = (event.target as HTMLButtonElement).dataset?.node;
    scheduleIntent(id);
    const node = id ? byId().get(id) : undefined;
    if (node && measurable) {
      const x = node.x * camera.k + camera.targetX,
        y = node.y * camera.k + camera.targetY;
      if (
        x < node.radius ||
        x > width - node.radius ||
        y < node.radius ||
        y > height - node.radius
      ) {
        camera.targetX += width / 2 - x;
        camera.targetY += height / 2 - y;
        if (!canAnimate()) snapCamera();
        render();
        requestFrame();
      }
    }
  };
  const onKey = (event: KeyboardEvent) => {
    const id = (event.target as HTMLButtonElement).dataset?.node;
    const node = id ? byId().get(id) : undefined;
    if (
      !node ||
      !["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key)
    )
      return;
    event.preventDefault();
    const amount = 12 / camera.k;
    if (event.key === "ArrowUp") node.y -= amount;
    if (event.key === "ArrowDown") node.y += amount;
    if (event.key === "ArrowLeft") node.x -= amount;
    if (event.key === "ArrowRight") node.x += amount;
    node.vx = node.vy = 0;
    energy = Infinity;
    render();
    requestFrame();
  };
  const onLeave = () => clearIntent();
  const observer = new ResizeObserver(resize);
  observer.observe(stage);
  const intersection = new IntersectionObserver((entries) => {
    intersecting = Boolean(entries[0]?.isIntersecting);
    if (canAnimate()) requestFrame();
    else stopFrame();
  });
  intersection.observe(stage);
  stage.style.touchAction = "none";
  stage.addEventListener("pointerdown", onDown);
  stage.addEventListener("pointermove", onMove);
  stage.addEventListener("pointerleave", onLeave);
  stage.addEventListener("focusin", onFocus);
  stage.addEventListener("keydown", onKey);
  stage.addEventListener("pointerup", onUp);
  stage.addEventListener("pointercancel", onCancel);
  stage.addEventListener("lostpointercapture", onCancel);
  stage.addEventListener("wheel", onWheel, { passive: false });
  document.addEventListener("visibilitychange", onVisibility);
  reduceQuery?.addEventListener("change", onMotion);
  syncScene(initial);
  resize();
  return {
    update(next) {
      syncScene(next);
      focus();
      requestFrame();
    },
    select(id) {
      selected = id;
      render();
    },
    focusTopic(id) {
      requestedTopic = id;
      focus();
    },
    zoomBy(factor) {
      userCamera = true;
      const prior = camera.targetK,
        k = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, prior * factor));
      camera.targetK = k;
      camera.targetX = width / 2 - ((width / 2 - camera.targetX) / prior) * k;
      camera.targetY = height / 2 - ((height / 2 - camera.targetY) / prior) * k;
      if (!canAnimate()) snapCamera();
      options.onCamera?.(k);
      render();
      requestFrame();
    },
    fit() {
      requestedTopic = undefined;
      userCamera = false;
      fitNodes(nodes);
    },
    reset() {
      userCamera = false;
      const resetNodes = new Map<string, RenderNode>();
      for (const node of [...scene.nodes].sort(
        (a, b) => a.depth - b.depth || a.id.localeCompare(b.id),
      )) {
        const rendered = byId().get(node.id);
        if (!rendered) continue;
        const position = initialPosition(node, scene, resetNodes);
        rendered.x = rendered.homeX = position.x;
        rendered.y = rendered.homeY = position.y;
        rendered.vx = rendered.vy = 0;
        resetNodes.set(node.id, rendered);
      }
      selected = undefined;
      requestedTopic = undefined;
      if (reduced) settle();
      else if (motion !== "paused") energy = Infinity;
      fitNodes(nodes);
      render();
    },
    shake() {
      if (motion === "paused" || reduced) return;
      for (const node of nodes) {
        node.vx += ((node.id.charCodeAt(0) % 7) - 3) * 1.5;
        node.vy += ((node.id.charCodeAt(node.id.length - 1) % 7) - 3) * 1.5;
      }
      energy = Infinity;
      requestFrame();
    },
    setMotion(next) {
      motion = next;
      if (next === "paused") {
        zeroVelocities();
        snapCamera();
        stopFrame();
        render();
      } else {
        zeroVelocities();
        energy = Infinity;
        requestFrame();
      }
    },
    snapshot() {
      return {
        nodes: nodes.map(({ button, label, lines, source, born, ...node }) => ({
          ...node,
        })),
        camera: { x: camera.x, y: camera.y, k: camera.k },
        running: Boolean(raf),
        motion,
        dragging: drag?.id ?? null,
      };
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      clearIntent();
      stopFrame();
      observer.disconnect();
      intersection.disconnect();
      stage.removeEventListener("pointerdown", onDown);
      stage.removeEventListener("pointermove", onMove);
      stage.removeEventListener("pointerleave", onLeave);
      stage.removeEventListener("focusin", onFocus);
      stage.removeEventListener("keydown", onKey);
      stage.removeEventListener("pointerup", onUp);
      stage.removeEventListener("pointercancel", onCancel);
      stage.removeEventListener("lostpointercapture", onCancel);
      stage.removeEventListener("wheel", onWheel);
      document.removeEventListener("visibilitychange", onVisibility);
      reduceQuery?.removeEventListener("change", onMotion);
      stage.replaceChildren();
    },
  };
}
