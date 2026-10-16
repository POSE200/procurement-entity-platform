<script setup lang="ts">
/**
 * 局部关系子图（AntV G6 v5）。只渲染当前查询返回的 TopK 节点，不一次加载全图。
 * 渲染完成后回传 render_ms，供关系页计算“前端发起至完全渲染”的耗时。
 */
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { Graph } from '@antv/g6'
import type { GraphEdge, GraphNode } from '@/types/api'

const props = defineProps<{ nodes: GraphNode[]; edges: GraphEdge[] }>()
const emit = defineEmits<{ rendered: [ms: number]; select: [id: string] }>()
const el = ref<HTMLDivElement>()
const graph = shallowRef<Graph | null>(null)

const COLOR: Record<GraphNode['kind'], { fill: string; stroke: string; size: number }> = {
  buyer: { fill: '#2f4a6b', stroke: '#1f3450', size: 46 },
  focus: { fill: '#b23a2a', stroke: '#86291d', size: 46 },
  supplier: { fill: '#fffdf9', stroke: '#3f6190', size: 26 },
  project: { fill: '#efece4', stroke: '#7a746a', size: 20 },
}

function short(s: string, n = 12) {
  return s.length > n ? s.slice(0, n) + '…' : s
}

async function draw() {
  if (!el.value) return
  const t0 = performance.now()
  graph.value?.destroy()
  graph.value = null
  if (!props.nodes.length) { emit('rendered', performance.now() - t0); return }
  const maxW = Math.max(1, ...props.nodes.map((n) => n.weight ?? 1))
  const centers = props.nodes.filter((n) => n.kind === 'buyer' || n.kind === 'focus')
  const g = new Graph({
    container: el.value,
    autoFit: 'view',
    padding: 24,
    animation: false,
    data: {
      nodes: props.nodes.map((n) => ({ id: n.id, data: { ...n } })),
      edges: props.edges
        .filter((e) => e.source !== e.target)
        .map((e, i) => ({ id: `e${i}`, source: e.source, target: e.target, data: { ...e } })),
    },
    node: {
      style: (d) => {
        const n = d.data as unknown as GraphNode
        const c = COLOR[n.kind]
        const size = n.kind === 'supplier' ? 18 + 18 * ((n.weight ?? 1) / maxW) : c.size
        return {
          size,
          fill: c.fill,
          stroke: c.stroke,
          lineWidth: 1.5,
          labelText: short(n.label, n.kind === 'buyer' || n.kind === 'focus' ? 14 : 12),
          labelFill: '#1f1d1a',
          labelFontSize: 11,
          labelPlacement: 'bottom',
          labelBackground: true,
          labelBackgroundFill: '#f7f5f0',
          labelBackgroundOpacity: 0.85,
          cursor: 'pointer',
        }
      },
    },
    edge: {
      style: (d) => {
        const e = d.data as unknown as GraphEdge
        const peer = !centers.some((c) => c.id === e.source)
        return {
          stroke: peer ? '#b8a88a' : '#97a8bf',
          lineWidth: 1 + 2.5 * Math.min(1, (e.weight ?? 1) / maxW),
          lineDash: peer ? [4, 3] : undefined,
          labelText: e.label,
          labelFontSize: 10,
          labelFill: '#7a746a',
          labelBackground: true,
          labelBackgroundFill: '#fffdf9',
        }
      },
    },
    layout: centers.length === 1
      ? { type: 'radial', focusNode: centers[0].id, unitRadius: 150, preventOverlap: true, nodeSize: 60, linkDistance: 180 }
      : { type: 'd3-force', link: { distance: 160 }, manyBody: { strength: -420 }, collide: { radius: 40 } },
    behaviors: ['drag-canvas', 'zoom-canvas', 'drag-element', 'hover-activate'],
  })
  g.on('node:click', (evt) => {
    const id = (evt as unknown as { target?: { id?: string } }).target?.id
    if (id) emit('select', id)
  })
  graph.value = g
  await g.render()
  emit('rendered', performance.now() - t0)
}

let ro: ResizeObserver | null = null
onMounted(() => {
  draw()
  ro = new ResizeObserver(() => {
    const g = graph.value
    if (g && el.value) { g.resize(el.value.clientWidth, el.value.clientHeight); g.fitView() }
  })
  if (el.value) ro.observe(el.value)
})
watch(() => [props.nodes, props.edges], draw)
onBeforeUnmount(() => { ro?.disconnect(); graph.value?.destroy() })
</script>

<template>
  <div class="gp">
    <div ref="el" class="canvas" role="img" :aria-label="`关系子图，${nodes.length} 个节点，${edges.length} 条边；表格中有同等信息`" />
    <div class="legend">
      <span><i style="background: #2f4a6b" />采购单位</span>
      <span><i style="background: #b23a2a" />焦点供应商</span>
      <span><i style="background: #fffdf9; border-color: #3f6190" />关联主体（大小=次数）</span>
      <span><i style="background: #efece4; border-color: #7a746a" />项目</span>
      <span class="muted">虚线：两主体共同出现</span>
    </div>
    <div v-if="!nodes.length" class="empty hint">无可展示的关系</div>
  </div>
</template>

<style scoped>
.gp { position: relative; border: 1px solid var(--rule-2); background:
  radial-gradient(circle, #e5e0d5 1px, transparent 1px) 0 0 / 18px 18px, var(--card); }
.canvas { width: 100%; height: 460px; }
.legend { position: absolute; left: 10px; bottom: 8px; display: flex; flex-wrap: wrap; gap: 12px; font-size: 11px; color: var(--ink-2);
  background: rgba(255, 253, 249, 0.9); padding: 4px 8px; border: 1px solid var(--rule-2); }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 50%; border: 1px solid transparent; margin-right: 4px; vertical-align: -1px; }
.empty { position: absolute; inset: 0; display: grid; place-items: center; }
</style>
