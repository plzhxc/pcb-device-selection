import { graphlib, layout } from '@dagrejs/dagre';
import { categories, type Category, type Convergence, type Document } from './types';

export interface DiagramNode {
  id: string; title: string; detail?: string; badge?: string; preview?: string;
  requirementIds?: string[]; tone?: string; compact?: boolean; toggleId?: string;
}
export interface DiagramEdge { id: string; source: string; target: string; label?: string; kind?: string; arrow?: boolean }
export interface DiagramGraph { nodes: DiagramNode[]; edges: DiagramEdge[]; layout: 'mindmap' | 'directed' | 'sequence'; direction?: 'LR' | 'TB'; }
export interface Point { x: number; y: number }
export interface PlacedNode extends DiagramNode, Point { width: number; height: number; titleLines: string[]; previewLines: string[]; badgeLines: string[] }
export interface PlacedEdge extends DiagramEdge { points: Point[]; labelPoint?: Point }
export interface GraphLayout { nodes: PlacedNode[]; edges: PlacedEdge[] }

const fontFamily = '"Inter", "Noto Sans SC", "Microsoft YaHei", sans-serif';
let measureContext: CanvasRenderingContext2D | null;
function measure(text: string, size: number, weight = 400) {
  measureContext ??= document.createElement('canvas').getContext('2d');
  if (!measureContext) return Array.from(text).length * size;
  measureContext.font = `${weight} ${size}px ${fontFamily}`;
  return measureContext.measureText(text).width;
}

// Match the SVG font exactly. Titles wrap completely; only explicitly marked
// previews are shortened. Full requirements stay available in the inspector.
function wrap(text: string, width: number, size: number, weight = 400): string[] {
  const lines: string[] = [];
  for (const paragraph of text.replace(/\r/g, '').split('\n')) {
    let line = '';
    for (const token of paragraph.match(/[A-Za-z0-9][A-Za-z0-9_./:+%\-]*|[\s\S]/g) ?? []) {
      if (line && measure(line + token, size, weight) > width) { lines.push(line); line = ''; }
      if (measure(token, size, weight) <= width) line += token;
      else for (const char of Array.from(token)) {
        if (line && measure(line + char, size, weight) > width) { lines.push(line); line = ''; }
        line += char;
      }
    }
    if (line || !paragraph) lines.push(line);
  }
  return lines.length ? lines : [''];
}

function sizeNode(node: DiagramNode): PlacedNode {
  const width = node.compact ? 206 : node.tone === 'root' ? 224 : 224;
  const titleLines = wrap(node.title, width - 36, node.compact ? 14 : 15, 500);
  const badgeLines = node.badge ? wrap(node.badge, width - 36, 11) : [];
  const previewLines = node.preview ? wrap(node.preview, width - 36, 12) : [];
  if (previewLines.length > 2) {
    previewLines.length = 2;
    let last = previewLines[1];
    while (last && measure(last + '…', 12) > width - 36) last = last.slice(0, -1);
    previewLines[1] = last + '…';
  }
  return { ...node, x: 0, y: 0, width,
    height: 30 + badgeLines.length * 17 + (badgeLines.length ? 4 : 0) + titleLines.length * 21 + (previewLines.length ? 9 + previewLines.length * 18 : 0),
    titleLines, previewLines, badgeLines };
}

export function placeGraph(graph: DiagramGraph): GraphLayout {
  const nodes = graph.nodes.map(sizeNode);
  if (graph.layout === 'mindmap') {
    const root = nodes.find(n => n.id === 'root');
    if (!root) return { nodes, edges: [] };
    root.x = 0; root.y = -root.height / 2;
    const byId = new Map(nodes.map(n => [n.id, n]));
    const groups = graph.edges.filter(e => e.source === 'root').map(e => byId.get(e.target)!).filter(Boolean);
    for (const side of [1]) {
      const branches = groups;
      const branchRows = branches.map(branch => {
        const children = graph.edges.filter(e => e.source === branch.id).map(e => byId.get(e.target)!).filter(Boolean);
        return { branch, children, height: Math.max(branch.height, children.reduce((s, n) => s + n.height, 0) + Math.max(0, children.length - 1) * 14) };
      });
      let top = -(branchRows.reduce((s, r) => s + r.height, 0) + Math.max(0, branchRows.length - 1) * 30) / 2;
      for (const { branch, children, height } of branchRows) {
        branch.x = side ? root.width + 56 : -branch.width - 56;
        branch.y = top + (height - branch.height) / 2;
        let childY = top;
        for (const child of children) {
          child.x = side ? branch.x + branch.width + 42 : branch.x - child.width - 42;
          child.y = childY; childY += child.height + 14;
        }
        top += height + 30;
      }
    }
    return { nodes, edges: graph.edges.map(e => ({ ...e, points: [] })) };
  }
  if (graph.layout === 'sequence') {
    let top = 0;
    for (let start = 0; start < nodes.length; start += 3) {
      const row = nodes.slice(start, start + 3), reverse = Math.floor(start / 3) % 2 === 1;
      const rowHeight = Math.max(...row.map(n => n.height));
      row.forEach((node, i) => { node.x = (reverse ? 2 - i : i) * 294; node.y = top + (rowHeight - node.height) / 2; });
      top += rowHeight + 62;
    }
    return { nodes, edges: graph.edges.map(e => ({ ...e, points: [] })) };
  }
  const g = new graphlib.Graph({ multigraph: true });
  g.setGraph({ rankdir: graph.direction ?? 'LR', nodesep: 26, ranksep: 68, edgesep: 18, marginx: 24, marginy: 24 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const node of nodes) g.setNode(node.id, { width: node.width, height: node.height });
  const validIds = new Set(nodes.map(n => n.id));
  const edges = graph.edges.filter(e => validIds.has(e.source) && validIds.has(e.target));
  for (const edge of edges) g.setEdge(edge.source, edge.target, { width: edge.label ? measure(edge.label, 11) + 16 : 0, height: edge.label ? 22 : 0, labelpos: 'c' }, edge.id);
  layout(g);
  for (const node of nodes) {
    const positioned = g.node(node.id); node.x = positioned.x - node.width / 2; node.y = positioned.y - node.height / 2;
  }
  return { nodes, edges: edges.map(edge => {
    const placed = g.edge({ v: edge.source, w: edge.target, name: edge.id });
    return { ...edge, points: placed.points ?? [], labelPoint: Number.isFinite(placed.x) ? { x: placed.x, y: placed.y } : undefined };
  }) };
}

export function mindmapGraph(doc: Document, expanded: string[] = []): DiagramGraph {
  const actual = doc.requirements.filter(r => r.state !== 'not_applicable');
  const nodes: DiagramNode[] = [{ id: 'root', title: doc.title, badge: '需求全景', preview: `${actual.length} 项实际需求`, tone: 'root' }];
  const edges: DiagramEdge[] = [];
  (Object.keys(categories) as Category[]).forEach((key, index) => {
    const rows = actual.filter(r => r.category === key), id = 'category-' + key, tone = 'branch-' + index;
    const expandable = rows.length > 3, isExpanded = expanded.includes(key);
    nodes.push({ id, title: categories[key], badge: `${rows.length} 项需求${expandable ? isExpanded ? ' · − 收起' : ' · ＋ 展开' : ''}`, tone, compact: true, toggleId: expandable ? key : undefined,
      detail: rows.map(r => `${r.name}\n${r.value}${r.unit && !r.value.includes(r.unit) ? ' ' + r.unit : ''}`).join('\n\n') || '随讨论逐步补充。' });
    edges.push({ id: 'root-' + key, source: 'root', target: id });
    for (const row of expandable && !isExpanded ? rows.slice(0, 3) : rows) {
      nodes.push({ id: row.id, title: row.name, compact: true, tone,
        detail: row.value + (row.unit && !row.value.includes(row.unit) ? ' ' + row.unit : ''), requirementIds: [row.id] });
      edges.push({ id: key + '-' + row.id, source: id, target: row.id });
    }
    if (expandable && !isExpanded) {
      const moreId = 'more-' + key;
      nodes.push({ id: moreId, title: `展开其余 ${rows.length - 3} 项需求`, compact: true, tone: 'muted', toggleId: key });
      edges.push({ id: 'more-edge-' + key, source: id, target: moreId });
    }
  });
  return { nodes, edges, layout: 'mindmap' };
}

export function moduleGraph(doc: Document, links: Convergence['module_links'] = []): DiagramGraph {
  const modules = (doc.modules ?? []).filter(m => m.state !== 'not_applicable');
  if (!modules.length) {
    const rows = doc.requirements.filter(r => r.category === 'functions' && r.state !== 'not_applicable');
    return { layout: 'directed', nodes: [{ id: 'system', title: doc.title, tone: 'root', badge: '系统目标' }, ...rows.map(r => ({ id: r.id, title: r.name, preview: r.value, detail: r.value, requirementIds: [r.id] }))],
      edges: rows.map(r => ({ id: 'function-' + r.id, source: 'system', target: r.id, arrow: true })) };
  }
  return { layout: 'directed', nodes: modules.map(m => ({ id: m.id, title: m.name, badge: `${m.type_name || '规范模块'} · ×${m.quantity}`,
    detail: `${[m.category_name, m.type_name].filter(Boolean).join(' / ')}\n\n${m.purpose}${m.boundary ? '\n\n范围与边界：' + m.boundary : ''}`, requirementIds: m.requirement_ids, tone: m.state === 'tbd' ? 'muted' : 'module' })),
    edges: links.map(e => ({ id: e.id, source: e.source, target: e.target, kind: e.kind, label: { power: '供电', data: '数据', control: '控制', clock: '时钟' }[e.kind], arrow: true })) };
}

export function flowGraph(doc: Document): DiagramGraph {
  return { layout: 'sequence', nodes: doc.flow.map((step, i) => ({ id: 'Step' + i, title: step.label, badge: `步骤 ${String(i + 1).padStart(2, '0')}`, tone: i === 0 ? 'root' : 'flow',
    detail: step.requirement_ids.map(id => doc.requirements.find(r => r.id === id)).filter(Boolean).map(r => `${r!.name}：${r!.value}${r!.unit && !r!.value.includes(r!.unit) ? ' ' + r!.unit : ''}`).join('\n\n'), requirementIds: step.requirement_ids })),
    edges: doc.flow.slice(1).map((_, i) => ({ id: 'step-edge-' + i, source: 'Step' + i, target: 'Step' + (i + 1), arrow: true })) };
}

// The document exporter emits these two flowchart forms. Read their actual
// nodes/edges instead of substituting a decorative, unrelated diagram.
export function documentGraph(source: string, doc?: Document): DiagramGraph | null {
  const nodes: DiagramNode[] = [], edges: DiagramEdge[] = [];
  for (const line of source.trim().split('\n').map(s => s.trim()).filter(Boolean)) {
    if (/^(?:flowchart|graph)\s+(?:LR|RL|TD|TB|BT)\s*;?$/.test(line) || line.startsWith('%%')) continue;
    const node = line.match(/^([\w-]+)\s*\["(.*)"\]\s*;?$/);
    if (node) {
      const module = doc?.modules?.find(m => m.id.replace(/-/g, '_') === node[1]);
      const step = node[1].match(/^Step(\d+)$/);
      const flow = step ? doc?.flow[Number(step[1])] : undefined;
      nodes.push({ id: node[1], title: module?.name ?? flow?.label ?? node[2],
        badge: module ? `${module.type_name || '规范模块'} · ×${module.quantity}` : step ? `步骤 ${String(Number(step[1]) + 1).padStart(2, '0')}` : undefined,
        detail: module ? `${module.purpose}\n\n范围与边界：${module.boundary}` : flow?.requirement_ids.map(id => doc?.requirements.find(r => r.id === id)).filter(Boolean).map(r => `${r!.name}：${r!.value}`).join('\n\n'),
        requirementIds: module?.requirement_ids ?? flow?.requirement_ids, tone: module ? 'module' : step && Number(step[1]) === 0 ? 'root' : 'flow' });
      continue;
    }
    const edge = line.match(/^([\w-]+)\s*-->\s*(?:\|([^|]*)\|\s*)?([\w-]+)\s*;?$/);
    if (edge) {
      const kind = ({ '供电': 'power', '数据': 'data', '控制': 'control', '时钟': 'clock' } as Record<string, string>)[edge[2]];
      edges.push({ id: 'document-edge-' + edges.length, source: edge[1], target: edge[3], label: edge[2], kind, arrow: true });
    } else return null;
  }
  return nodes.length ? { nodes, edges, layout: nodes.every(n => /^Step\d+$/.test(n.id)) ? 'sequence' : 'directed' } : null;
}
