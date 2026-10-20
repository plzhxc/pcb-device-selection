import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type PointerEvent, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { ArrowUpRight, Expand, Focus, Hand, Minus, Move, Plus, RotateCcw, X } from 'lucide-react';
import { readSetting, saveSetting } from './types';
import { placeGraph, type DiagramGraph, type PlacedNode, type Point } from './diagramGraph';
import './diagrams.css';

type Camera = Point & { scale: number };
type Offsets = Record<string, Point>;
type Gesture = { mode: 'pan' | 'node' | 'pinch'; start: Point; camera: Camera; node?: string; offset?: Point; moved: boolean; distance?: number; midpoint?: Point };
const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value));

function loadOffsets(key: string): Offsets {
  try {
    const value = JSON.parse(readSetting('diagram-layout:' + key, '{}'));
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter(([, p]) => p && typeof p === 'object' && Number.isFinite((p as Point).x) && Number.isFinite((p as Point).y))) as Offsets;
  } catch { return {}; }
}
function extent(nodes: PlacedNode[]) {
  const left = Math.min(...nodes.map(n => n.x)), top = Math.min(...nodes.map(n => n.y));
  return { left, top, width: Math.max(...nodes.map(n => n.x + n.width)) - left, height: Math.max(...nodes.map(n => n.y + n.height)) - top };
}
function joinedPath(points: Point[]) {
  if (!points.length) return '';
  let path = `M${points[0].x},${points[0].y}`;
  for (let i = 1; i < points.length - 1; i++) {
    const a = points[i - 1], b = points[i], c = points[i + 1];
    const first = Math.hypot(b.x - a.x, b.y - a.y), second = Math.hypot(c.x - b.x, c.y - b.y);
    if (!first || !second) { path += ` L${b.x},${b.y}`; continue; }
    const radius = Math.min(12, first / 2, second / 2);
    path += ` L${b.x - (b.x - a.x) / first * radius},${b.y - (b.y - a.y) / first * radius} Q${b.x},${b.y} ${b.x + (c.x - b.x) / second * radius},${b.y + (c.y - b.y) / second * radius}`;
  }
  const end = points.at(-1)!;
  return path + ` L${end.x},${end.y}`;
}
function connection(a: PlacedNode, b: PlacedNode, horizontalOnly = false) {
  const ac = { x: a.x + a.width / 2, y: a.y + a.height / 2 }, bc = { x: b.x + b.width / 2, y: b.y + b.height / 2 };
  if (a.id === b.id) return { path: `M${a.x + a.width},${ac.y} C${a.x + a.width + 65},${ac.y - 90} ${a.x + a.width + 65},${ac.y + 90} ${a.x + a.width},${ac.y + 12}`, label: { x: a.x + a.width + 48, y: ac.y } };
  const horizontal = horizontalOnly || Math.abs(bc.x - ac.x) / (a.width + b.width) >= Math.abs(bc.y - ac.y) / (a.height + b.height);
  if (horizontal) {
    const sign = bc.x >= ac.x ? 1 : -1, start = { x: ac.x + sign * a.width / 2, y: ac.y }, end = { x: bc.x - sign * b.width / 2, y: bc.y };
    const bend = Math.max(36, Math.abs(end.x - start.x) / 2);
    return { path: `M${start.x},${start.y} C${start.x + sign * bend},${start.y} ${end.x - sign * bend},${end.y} ${end.x},${end.y}`, label: { x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 } };
  }
  const sign = bc.y >= ac.y ? 1 : -1, start = { x: ac.x, y: ac.y + sign * a.height / 2 }, end = { x: bc.x, y: bc.y - sign * b.height / 2 };
  const bend = Math.max(36, Math.abs(end.y - start.y) / 2);
  return { path: `M${start.x},${start.y} C${start.x},${start.y + sign * bend} ${end.x},${end.y - sign * bend} ${end.x},${end.y}`, label: { x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 } };
}

export function DiagramCanvas({ graph, title, storageKey, tabs, onDiscuss, onToggle, overview=false }: {
  graph: DiagramGraph; title: string; storageKey: string; tabs?: ReactNode; onDiscuss?: (id: string) => void; onToggle?: (id: string) => void; overview?: boolean;
}) {
  const [size, setSize] = useState({ width: 640, height: 460 });
  const compactLayout=graph.layout==='directed'&&!graph.direction&&graph.nodes.length<=8;
  const serialized = JSON.stringify(compactLayout?{...graph,direction:size.width<840?'TB':'LR'}:graph);
  const [fontsReady, setFontsReady] = useState(false);
  useEffect(() => { let alive = true; void document.fonts.ready.then(() => { if (alive) setFontsReady(true); }); return () => { alive = false; }; }, []);
  const placed = useMemo(() => placeGraph(JSON.parse(serialized) as DiagramGraph), [serialized, fontsReady]);
  const [offsets, setOffsets] = useState<Offsets>(() => loadOffsets(storageKey));
  const offsetsRef = useRef(offsets); offsetsRef.current = offsets;
  const nodes = placed.nodes.map(n => ({ ...n, x: n.x + (offsets[n.id]?.x ?? 0), y: n.y + (offsets[n.id]?.y ?? 0) }));
  const byId = new Map(nodes.map(n => [n.id, n]));
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = selectedId ? byId.get(selectedId) : undefined;
  const [expanded, setExpanded] = useState(false), [dragging, setDragging] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null), viewport = useRef<HTMLDivElement>(null);
  const [camera, setCamera] = useState<Camera>({ x: 0, y: 0, scale: 1 });
  const cameraRef = useRef(camera); cameraRef.current = camera;
  const pointers = useRef(new Map<number, Point>()), gesture = useRef<Gesture | null>(null);
  const marker = 'diagram-arrow-' + useId().replace(/:/g, '');
  const initialized = useRef('');
  useLayoutEffect(() => {
    const next = loadOffsets(storageKey); offsetsRef.current = next; setOffsets(next); setSelectedId(null);
  }, [storageKey]);
  function updateCamera(next: Camera) { cameraRef.current = next; setCamera(next); }
  function fit(reset = false, readable = false) {
    if (!nodes.length) return;
    const target = reset ? placed.nodes : nodes, box = extent(target);
    const width = viewport.current?.clientWidth ?? size.width, height = viewport.current?.clientHeight ?? size.height;
    const whole = clamp(Math.min((width - 64) / Math.max(1, box.width), (height - 64) / Math.max(1, box.height)), .01, 1.1);
    const scale = readable ? Math.max(.7, whole) : whole;
    if (scale > whole) {
      const priority = graph.layout === 'sequence' ? target[0] : target.map(n => ({ node: n, degree: graph.edges.filter(e => e.source === n.id || e.target === n.id).length })).sort((a, b) => b.degree - a.degree)[0]?.node ?? target[0];
      updateCamera({ scale, x: width / 2 - (priority.x + priority.width / 2) * scale, y: height / 2 - (priority.y + priority.height / 2) * scale });
    } else updateCamera({ scale, x: (width - box.width * scale) / 2 - box.left * scale, y: (height - box.height * scale) / 2 - box.top * scale });
  }
  useLayoutEffect(() => {
    const element = viewport.current; if (!element) return;
    const observer = new ResizeObserver(() => setSize({ width: element.clientWidth, height: element.clientHeight }));
    observer.observe(element); return () => observer.disconnect();
  }, [expanded]);
  useLayoutEffect(() => {
    // Polling the same document must not reset a user's camera or node layout.
    const signature = serialized + ':' + fontsReady + ':' + expanded + ':' + size.width + ':' + size.height;
    if (initialized.current !== signature && size.width > 0 && size.height > 0) { initialized.current = signature; fit(false, !overview&&nodes.length>8); }
  }, [serialized, fontsReady, expanded, size.width, size.height]);
  useEffect(() => {
    if (!expanded) return;
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.showModal();
    return () => { dialog.current?.close(); previous?.focus({ preventScroll: true }); };
  }, [expanded]);
  function zoomTo(scale: number, anchor = { x: size.width / 2, y: size.height / 2 }) {
    const old = cameraRef.current, next = clamp(scale, .01, 2.5);
    updateCamera({ scale: next, x: anchor.x - (anchor.x - old.x) * next / old.scale, y: anchor.y - (anchor.y - old.y) * next / old.scale });
  }
  function reset() {
    offsetsRef.current = {}; setOffsets({}); saveSetting('diagram-layout:' + storageKey, '{}'); fit(true);
  }
  function point(event: { clientX: number; clientY: number }): Point {
    const box = viewport.current!.getBoundingClientRect(); return { x: event.clientX - box.left, y: event.clientY - box.top };
  }
  function selectNode(id: string) {
    const node = byId.get(id);
    if (node?.toggleId && onToggle) onToggle(node.toggleId); else setSelectedId(id);
  }
  function begin(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0 || !nodes.length) return;
    const current = point(event); pointers.current.set(event.pointerId, current); event.currentTarget.setPointerCapture(event.pointerId);
    if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      gesture.current = { mode: 'pinch', start: current, camera: cameraRef.current, moved: true, distance: Math.hypot(a.x - b.x, a.y - b.y), midpoint: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 } };
    } else if (pointers.current.size === 1) {
      const node = (event.target as Element).closest('[data-diagram-node]')?.getAttribute('data-diagram-node') ?? undefined;
      gesture.current = { mode: node ? 'node' : 'pan', start: current, camera: cameraRef.current, node, offset: node ? offsetsRef.current[node] ?? { x: 0, y: 0 } : undefined, moved: false };
    }
    event.preventDefault();
  }
  function move(event: PointerEvent<HTMLDivElement>) {
    if (!pointers.current.has(event.pointerId) || !gesture.current) return;
    const current = point(event); pointers.current.set(event.pointerId, current); const g = gesture.current;
    if (g.mode === 'pinch' && pointers.current.size >= 2) {
      const [a, b] = [...pointers.current.values()], mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      const next = clamp(g.camera.scale * Math.hypot(a.x - b.x, a.y - b.y) / Math.max(1, g.distance!), .01, 2.5);
      updateCamera({ scale: next, x: mid.x - (g.midpoint!.x - g.camera.x) * next / g.camera.scale, y: mid.y - (g.midpoint!.y - g.camera.y) * next / g.camera.scale }); return;
    }
    const dx = current.x - g.start.x, dy = current.y - g.start.y;
    if (!g.moved && Math.hypot(dx, dy) < 4) return;
    g.moved = true; setDragging(true);
    if (g.mode === 'node' && g.node) {
      const next = { ...offsetsRef.current, [g.node]: { x: g.offset!.x + dx / g.camera.scale, y: g.offset!.y + dy / g.camera.scale } };
      offsetsRef.current = next; setOffsets(next);
    } else updateCamera({ ...g.camera, x: g.camera.x + dx, y: g.camera.y + dy });
  }
  function end(event: PointerEvent<HTMLDivElement>) {
    const g = gesture.current;
    if (!pointers.current.has(event.pointerId)) return;
    pointers.current.delete(event.pointerId);
    if (event.type !== 'pointercancel' && g?.mode === 'node' && !g.moved && g.node) selectNode(g.node);
    if (!pointers.current.size) {
      saveSetting('diagram-layout:' + storageKey, JSON.stringify(offsetsRef.current));
      gesture.current = null; setDragging(false);
    } else {
      gesture.current = { mode: 'pan', start: [...pointers.current.values()][0], camera: cameraRef.current, moved: true };
    }
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  }
  useEffect(() => {
    const element = viewport.current; if (!element) return;
    const wheel = (event: WheelEvent) => {
      if (!event.ctrlKey && !event.metaKey) return; // Normal scrolling still scrolls the document.
      event.preventDefault(); zoomTo(cameraRef.current.scale * Math.exp(-event.deltaY * .006), point(event));
    };
    element.addEventListener('wheel', wheel, { passive: false });
    return () => element.removeEventListener('wheel', wheel);
  }, [expanded, size.width, size.height]);

  const natural = placed.nodes.length ? extent(placed.nodes) : { width: 640, height: 350 };
  const naturalHeight = clamp((natural.height + 64) * Math.min(1, (size.width - 40) / Math.max(1, natural.width)), 360, size.width < 480 ? 620 : 860);
  const bounds = nodes.length ? extent(nodes) : { left: 0, top: 0, width: 640, height: 400 };
  const minimapBox = { x: bounds.left - 20, y: bounds.top - 20, width: bounds.width + 40, height: bounds.height + 40 };

  function navigate(event: PointerEvent<SVGSVGElement>) {
    const svg = event.currentTarget, matrix = svg.getScreenCTM(); if (!matrix) return;
    const p = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
    updateCamera({ ...cameraRef.current, x: size.width / 2 - p.x * cameraRef.current.scale, y: size.height / 2 - p.y * cameraRef.current.scale });
  }
  const surface = <div className={`diagram-surface ${expanded ? 'is-expanded' : ''}`} data-diagram-title={title}>
    <div className="diagram-toolbar">
      {tabs ?? <div className="diagram-title"><Move size={14}/><strong>{title}</strong></div>}
      <div className="diagram-actions">
        <button type="button" aria-label="缩小设计图" title="缩小" onClick={() => zoomTo(camera.scale / 1.2)} disabled={camera.scale <= .01}><Minus size={14}/></button>
        <button type="button" className="diagram-scale" aria-label="以实际大小查看设计图" title="以100%查看" onClick={() => zoomTo(1)}>{Math.round(camera.scale * 100)}%</button>
        <button type="button" aria-label="放大设计图" title="放大" onClick={() => zoomTo(camera.scale * 1.2)} disabled={camera.scale >= 2.5}><Plus size={14}/></button>
        <span className="diagram-tool-divider"/>
        <button type="button" aria-label="适应画布" title="显示全部节点" onClick={() => fit()}><Focus size={15}/></button>
        <button type="button" aria-label="重置节点布局" title="恢复自动布局" onClick={reset}><RotateCcw size={14}/></button>
        <button type="button" aria-label={expanded ? '关闭图形全屏' : '全屏查看设计图'} title={expanded ? '退出全屏' : '全屏查看'} onClick={() => setExpanded(v => !v)}>{expanded ? <X size={16}/> : <Expand size={14}/>}</button>
      </div>
    </div>
    <div ref={viewport} style={expanded ? undefined : { height: naturalHeight }} className={`diagram-viewport graph-stage ${dragging ? 'is-dragging' : ''}`} aria-label={title + '交互画布'} tabIndex={0}
      onPointerDown={begin} onPointerMove={move} onPointerUp={end} onPointerCancel={end}
      onKeyDown={event => {
        if (event.target !== event.currentTarget) return;
        if (event.key === '+' || event.key === '=') { event.preventDefault(); zoomTo(camera.scale * 1.2); }
        if (event.key === '-') { event.preventDefault(); zoomTo(camera.scale / 1.2); }
        if (event.key === '0') { event.preventDefault(); fit(); }
        const delta = ({ ArrowLeft: [40, 0], ArrowRight: [-40, 0], ArrowUp: [0, 40], ArrowDown: [0, -40] } as Record<string, number[]>)[event.key];
        if (delta) { event.preventDefault(); updateCamera({ ...camera, x: camera.x + delta[0], y: camera.y + delta[1] }); }
      }}>
      <svg role="group" aria-label={title} width={size.width} height={size.height} viewBox={`0 0 ${size.width} ${size.height}`}>
        <defs><marker id={marker} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M1 1 L9 5 L1 9" fill="none" stroke="context-stroke" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></marker></defs>
        <g className="diagram-world" transform={`translate(${camera.x},${camera.y}) scale(${camera.scale})`} data-camera-scale={camera.scale}>
          {placed.edges.map(edge => {
            const from = byId.get(edge.source), to = byId.get(edge.target); if (!from || !to) return null;
            const custom = graph.layout !== 'directed' || offsets[edge.source] || offsets[edge.target];
            const updated = connection(from, to, graph.layout === 'mindmap'), path = custom ? updated.path : joinedPath(edge.points), label = custom ? updated.label : edge.labelPoint ?? updated.label;
            const linked = selectedId === edge.source || selectedId === edge.target;
            return <g key={edge.id} className={`diagram-edge ${edge.kind ?? ''} ${linked ? 'is-linked' : ''}`}>
              <path d={path} markerEnd={edge.arrow ? `url(#${marker})` : undefined}/>
              {edge.label && <g className="diagram-edge-label" transform={`translate(${label.x},${label.y})`}><rect x={-(edge.label.length * 11 + 16) / 2} y="-10" width={edge.label.length * 11 + 16} height="20" rx="5"/><text textAnchor="middle" dominantBaseline="central">{edge.label}</text></g>}
            </g>;
          })}
          {nodes.map(node => {
            const titleTop = 19 + (node.badgeLines.length ? node.badgeLines.length * 17 + 4 : 0);
            return <g key={node.id} role="button" tabIndex={0} aria-label={'查看 ' + node.title} data-diagram-node={node.id} data-node-width={node.width} data-node-height={node.height}
              className={`diagram-node ${node.tone ?? ''} ${node.compact ? 'compact' : ''} ${selectedId === node.id ? 'is-selected' : ''}`} transform={`translate(${node.x},${node.y})`}
              onKeyDown={event => {
                if (['Enter', ' '].includes(event.key)) { event.preventDefault(); event.stopPropagation(); selectNode(node.id); }
                if (event.key === 'Escape') setSelectedId(null);
                const delta = ({ ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] } as Record<string, number[]>)[event.key];
                if (delta) {
                  event.preventDefault(); event.stopPropagation(); const old = offsetsRef.current[node.id] ?? { x: 0, y: 0 }, step = event.shiftKey ? 32 : 8;
                  const next = { ...offsetsRef.current, [node.id]: { x: old.x + delta[0] * step, y: old.y + delta[1] * step } };
                  offsetsRef.current = next; setOffsets(next); saveSetting('diagram-layout:' + storageKey, JSON.stringify(next));
                }
              }}>
              <title>{node.title}{node.detail ? '\n' + node.detail : ''}</title>
              <rect className="diagram-node-box" width={node.width} height={node.height} rx={node.tone === 'root' ? 15 : 10}/>
              <path className="diagram-node-accent" d={`M1 14 L1 ${node.height - 14}`}/>
              {node.badgeLines.length > 0 && <text className="diagram-node-badge" x="18" y="23">{node.badgeLines.map((line, i) => <tspan key={i} x="18" dy={i ? 17 : 0}>{line}</tspan>)}</text>}
              <text className="diagram-node-title" x="18" y={titleTop + 10}>{node.titleLines.map((line, i) => <tspan key={i} x="18" dy={i ? 21 : 0}>{line}</tspan>)}</text>
              {!!node.previewLines.length && <text className="diagram-node-preview" x="18" y={titleTop + node.titleLines.length * 21 + 15}>{node.previewLines.map((line, i) => <tspan key={i} x="18" dy={i ? 18 : 0}>{line}</tspan>)}</text>}
            </g>;
          })}
        </g>
      </svg>
      {!nodes.length && <div className="diagram-no-data"><Move size={25}/><strong>图形会随需求逐步形成</strong><span>已有内容保存后会同步到这里</span></div>}
    </div>
    <div className="diagram-footer"><div className="diagram-help"><span><Hand size={12}/>拖动空白平移</span><span>拖动节点调整</span><span>Ctrl + 滚轮缩放 · 支持双指缩放</span></div>
      {nodes.length > 0 && <div className="diagram-minimap"><span>全图导航 · {nodes.length} 节点</span><svg aria-label="图形导航" viewBox={`${minimapBox.x} ${minimapBox.y} ${minimapBox.width} ${minimapBox.height}`} onPointerDown={event => { event.stopPropagation(); event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId); navigate(event); }} onPointerMove={event => { if (event.currentTarget.hasPointerCapture(event.pointerId)) { event.stopPropagation(); navigate(event); } }} onPointerUp={event => { event.stopPropagation(); if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId); }}>
        {nodes.map(n => <rect key={n.id} x={n.x} y={n.y} width={n.width} height={n.height} rx="5" className={n.tone === 'root' ? 'mini-root' : ''}/>)}
        <rect className="mini-camera" x={-camera.x / camera.scale} y={-camera.y / camera.scale} width={size.width / camera.scale} height={size.height / camera.scale}/>
      </svg></div>}
    </div>
    {selected && <aside className="diagram-inspector" aria-label="图形节点详情"><header><strong>{selected.title}</strong><button type="button" aria-label="关闭节点详情" onClick={() => setSelectedId(null)}><X size={15}/></button></header>
      <p>{selected.detail || selected.preview || selected.badge || '当前项目的需求节点。'}</p>
      {onDiscuss && (selected.actionId||selected.requirementIds?.[0]) && <button type="button" className="diagram-discuss" onClick={() => onDiscuss(selected.actionId||selected.requirementIds![0])}>{selected.actionLabel||'在对话中讨论'}<ArrowUpRight size={13}/></button>}
    </aside>}
  </div>;
  return <section className="diagram-card interactive-diagram" aria-label={title + '图形'}>
    {expanded ? <><div className="diagram-fullscreen-placeholder">{title}已在全屏中打开</div>{createPortal(<dialog ref={dialog} className="diagram-fullscreen-dialog" aria-label={title + '全屏'} onCancel={event => { event.preventDefault(); setExpanded(false); }}>{surface}</dialog>, document.body)}</> : surface}
  </section>;
}
