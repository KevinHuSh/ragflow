import { ElementDatum, Graph, IElementEvent } from '@antv/g6';
import isEmpty from 'lodash/isEmpty';
import { useCallback, useEffect, useId, useMemo, useRef } from 'react';

import { useIsDarkTheme } from '@/components/theme-provider';
import { cn } from '@/lib/utils';
import styles from '@/pages/dataset/knowledge-graph/index.module.less';

interface MindMapNode {
  id: string;
  depth?: number;
  entity_type?: string;
  description?: string;
  branchIndex?: number;
  side?: 'left' | 'right';
  isRoot?: boolean;
}

interface MindMapEdge {
  source: string;
  target: string;
  description?: string;
}

interface IProps {
  data: { nodes: MindMapNode[]; edges: MindMapEdge[] };
  show: boolean;
  rootId?: string;
}

const BRANCH_COLORS = [
  '#2563EB',
  '#059669',
  '#D97706',
  '#DC2626',
  '#7C3AED',
  '#0891B2',
  '#DB2777',
  '#65A30D',
];

function nodeMeta(node: any): Partial<MindMapNode> {
  return node?.data?.data ?? node?.data ?? node ?? {};
}

function nodeSize(node: {
  id?: string;
  depth?: number;
  isRoot?: boolean;
}): [number, number] {
  const meta = nodeMeta(node);
  const labelLen = (node.id ?? meta.id ?? '').length;
  const depth = node.depth ?? meta.depth ?? 1;
  if (node.isRoot || meta.isRoot || depth === 0) {
    return [Math.max(180, Math.min(labelLen * 10 + 56, 320)), 64];
  }
  return [Math.max(120, Math.min(labelLen * 8 + 36, 260)), 40];
}

function branchColor(node: Partial<MindMapNode>, isDark: boolean) {
  const meta = nodeMeta(node);
  const isRoot = node.isRoot ?? meta.isRoot;
  const depth = node.depth ?? meta.depth;
  if (isRoot || depth === 0) return isDark ? '#A78BFA' : '#6D28D9';
  const idx = Math.max(0, node.branchIndex ?? meta.branchIndex ?? 0);
  return BRANCH_COLORS[idx % BRANCH_COLORS.length];
}

function annotateMindMap(
  data: { nodes: MindMapNode[]; edges: MindMapEdge[] },
  rootId?: string,
) {
  const nodes = Array.isArray(data?.nodes) ? data.nodes : [];
  const edges = Array.isArray(data?.edges) ? data.edges : [];
  if (!nodes.length) return { nodes: [], edges: [] };

  const childMap = new Map<string, string[]>();
  const hasIncoming = new Set<string>();
  for (const edge of edges) {
    if (!childMap.has(edge.source)) childMap.set(edge.source, []);
    childMap.get(edge.source)!.push(edge.target);
    hasIncoming.add(edge.target);
  }

  const root =
    rootId ||
    nodes.find((node) => node.isRoot)?.id ||
    nodes.find((node) => !hasIncoming.has(node.id))?.id ||
    nodes[0]?.id;
  const depths = new Map<string, number>([[root, 0]]);
  const branchMeta = new Map<
    string,
    { branchIndex: number; side: 'left' | 'right' }
  >();
  const queue: string[] = [root];
  const rootChildren = childMap.get(root) ?? [];
  rootChildren.forEach((child, index) => {
    branchMeta.set(child, {
      branchIndex: index,
      side: index % 2 === 0 ? 'right' : 'left',
    });
  });

  while (queue.length) {
    const current = queue.shift()!;
    const depth = depths.get(current) ?? 0;
    const parentBranch = branchMeta.get(current);
    for (const child of childMap.get(current) ?? []) {
      if (!depths.has(child)) {
        depths.set(child, depth + 1);
        queue.push(child);
      }
      if (!branchMeta.has(child)) {
        branchMeta.set(
          child,
          parentBranch ?? { branchIndex: 0, side: 'right' },
        );
      }
    }
  }

  return {
    nodes: nodes.map((node) => {
      const meta = branchMeta.get(node.id);
      return {
        ...node,
        depth: depths.get(node.id) ?? 0,
        branchIndex: meta?.branchIndex ?? 0,
        side: meta?.side ?? 'right',
        isRoot: node.id === root || node.isRoot,
        size: nodeSize({ ...node, depth: depths.get(node.id) ?? 0 }),
      };
    }),
    edges,
  };
}

const MindMapGraph = ({ data, show, rootId }: IProps) => {
  const tooltipId = useId();
  const containerRef = useRef<HTMLDivElement>(null);
  const graphRef = useRef<Graph | null>(null);
  const isDark = useIsDarkTheme();

  const graphData = useMemo(
    () => annotateMindMap(data, rootId),
    [data, rootId],
  );

  const render = useCallback(() => {
    if (!containerRef.current) return;

    const graph = new Graph({
      container: containerRef.current!,
      autoFit: 'view',
      autoResize: true,
      behaviors: [
        'drag-element',
        'drag-canvas',
        'zoom-canvas',
        'collapse-expand',
        { type: 'hover-activate', degree: 1 },
      ],
      plugins: [
        {
          type: 'tooltip',
          enterable: true,
          getContent: (_e: IElementEvent, items: ElementDatum) => {
            if (!Array.isArray(items)) return undefined;
            return items
              .flatMap((item) => [
                `<div id="${tooltipId}" role="tooltip" aria-label="${item?.id}">`,
                `<h3 class="font-medium">${item?.id}</h3>`,
                item?.entity_type
                  ? `<div class="text-xs"><b>Type:</b> ${item.entity_type}</div>`
                  : '',
                item?.description
                  ? `<p class="text-xs whitespace-pre-wrap">${item.description}</p>`
                  : '',
                '</div>',
              ])
              .join('');
          },
        },
      ],
      layout: {
        type: 'mindmap',
        direction: 'H',
        getId: (d: any) => d.id,
        getSide: (child: any, index: number) =>
          nodeMeta(child)?.side ?? (index % 2 === 0 ? 'right' : 'left'),
        getHeight: (d: any) => nodeSize(d)[1],
        getWidth: (d: any) => nodeSize(d)[0],
        getVGap: (d: any) => (nodeMeta(d)?.isRoot ? 28 : 14),
        getHGap: (d: any) => (nodeMeta(d)?.isRoot ? 90 : 54),
        getSubTreeSep: () => 22,
      },
      node: {
        type: 'rect',
        style: {
          size: (d: any) => d.size ?? nodeSize(d),
          radius: (d: any) => (d.isRoot ? 14 : 8),
          fill: (d: any) => {
            const color = branchColor(d as MindMapNode, isDark);
            if (d.isRoot) return color;
            return isDark ? `${color}33` : `${color}1F`;
          },
          stroke: (d: any) => branchColor(d as MindMapNode, isDark),
          lineWidth: (d: any) => (d.isRoot ? 2.2 : 1.4),
          labelText: (d: any) => (d.id as string) ?? '',
          labelFill: (d: any) =>
            d.isRoot
              ? '#fff'
              : isDark
                ? 'rgba(255,255,255,0.95)'
                : 'rgba(15,23,42,0.95)',
          labelFontSize: (d: any) => (d.isRoot ? 16 : 13),
          labelFontWeight: (d: any) => (d.isRoot ? 700 : 500),
          labelTextAlign: 'center',
          labelTextBaseline: 'middle',
          labelPlacement: 'center',
          labelWordWrap: true,
          labelMaxLines: 2,
          labelMaxWidth: (d: any) =>
            Array.isArray(d.size) ? Math.max(88, d.size[0] - 24) : 220,
          shadowBlur: (d: any) => (d.isRoot ? 12 : 0),
          shadowColor: (d: any) =>
            d.isRoot ? branchColor(d as MindMapNode, isDark) : 'transparent',
        },
      },
      edge: {
        type: 'cubic-horizontal',
        style: {
          stroke: (edge: any) => {
            const target = graphData.nodes.find(
              (node) => node.id === (edge?.target ?? edge?.data?.target),
            );
            return target
              ? branchColor(target as MindMapNode, isDark)
              : isDark
                ? 'rgba(255,255,255,0.45)'
                : 'rgba(0,0,0,0.35)';
          },
          lineWidth: (edge: any) => {
            const target = graphData.nodes.find(
              (node) => node.id === (edge?.target ?? edge?.data?.target),
            );
            return target?.depth === 1 ? 2.4 : 1.4;
          },
          endArrow: false,
        },
      },
    });

    if (graphRef.current) {
      graphRef.current.destroy();
    }
    graphRef.current = graph;

    graph.setData({
      nodes: graphData.nodes.map((node) => ({
        ...node,
        data: { ...node },
      })) as any,
      edges: graphData.edges as any,
    });
    graph.render();
  }, [graphData, isDark, tooltipId]);

  useEffect(() => {
    if (show && !isEmpty(graphData.nodes)) {
      render();
    }
  }, [graphData, render, show]);

  useEffect(() => {
    return () => {
      graphRef.current?.destroy();
      graphRef.current = null;
    };
  }, []);

  return (
    <div
      ref={containerRef}
      className={cn(styles.forceContainer, 'size-full', !show && 'hidden')}
      aria-haspopup="true"
      aria-describedby={tooltipId}
    />
  );
};

export default MindMapGraph;
