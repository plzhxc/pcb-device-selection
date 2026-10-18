import { useState } from 'react';
import { GitBranch, Layers3, Workflow } from 'lucide-react';
import { DiagramCanvas } from './DiagramCanvas';
import { documentGraph, flowGraph, mindmapGraph, moduleGraph } from './diagramGraph';
import type { Convergence, Document } from './types';

type Kind = 'mindmap' | 'framework' | 'flow';
const labels = { mindmap: '需求思维导图', framework: '功能框架草案', flow: '工作流程草案' };

export function Diagrams({ document: doc, moduleLinks = [], storageKey = doc.title, onSelect }: {
  document: Document; moduleLinks?: Convergence['module_links']; storageKey?: string; onSelect: (id: string) => void;
}) {
  const [kind, setKind] = useState<Kind>('mindmap');
  const [expanded, setExpanded] = useState<string[]>([]);
  const graph = kind === 'mindmap' ? mindmapGraph(doc, expanded) : kind === 'framework' ? moduleGraph(doc, moduleLinks) : flowGraph(doc);
  const tabs = <div className="diagram-tabs" role="tablist" aria-label="设计图类型">
    {([['mindmap', '思维导图', GitBranch], ['framework', '功能框架', Layers3], ['flow', '工作流程', Workflow]] as const).map(([key, label, Icon]) => <button type="button" role="tab" aria-selected={kind === key} className={kind === key ? 'active' : ''} onClick={() => setKind(key)} key={key}><Icon size={13}/>{label}</button>)}
  </div>;
  return <DiagramCanvas key={storageKey} graph={graph} title={labels[kind]} storageKey={storageKey + ':overview:' + kind} tabs={tabs} onDiscuss={onSelect} onToggle={id => setExpanded(old => old.includes(id) ? old.filter(key => key !== id) : [...old, id])}/>;
}

export function DocumentDiagram({ source, document: doc, storageKey, onSelect }: {
  source: string; document: Document; storageKey: string; onSelect?: (id: string) => void;
}) {
  const graph = documentGraph(source, doc);
  if (!graph) return <pre><code>{source}</code></pre>;
  const flow = graph.nodes.every(n => /^Step\d+$/.test(n.id));
  return <DiagramCanvas key={storageKey + ':' + (flow ? 'flow' : 'modules')} graph={graph} title={flow ? '工作流程草案' : '系统框图草案'} storageKey={storageKey + ':document:' + (flow ? 'flow' : 'modules')} onDiscuss={onSelect}/>;
}
