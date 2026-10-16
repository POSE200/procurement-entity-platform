<script setup lang="ts">
defineOptions({ name: 'Task2View' })
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import ResultScope from '@/components/ResultScope.vue'
import { useResultScope } from '@/utils/useResultScope'
import { api } from '@/api/client'
import type { ProfileMetric, Task2Brief, Task2Overview } from '@/types/api'
import { moneyWan, ms, num } from '@/utils/format'

const router = useRouter()
const { jobId: scopeJobId, jobs: scopeJobs, loading: scopeLoading, total: scopeTotal, error: scopeError, version: scopeVersion, pick: pickScope } = useResultScope()
let loadToken = 0
const data = ref<Task2Overview | null>(null)
const loading = ref(false)
const error = ref('')

async function load() {
  if (scopeLoading.value || scopeError.value) return
  const token = ++loadToken
  loading.value = true
  error.value = ''
  try {
    const result = await api.task2Overview(scopeJobId.value)
    if (token === loadToken) data.value = result
  } catch (e) {
    if (token !== loadToken) return
    data.value = null
    error.value = (e as Error).message
  } finally {
    if (token === loadToken) loading.value = false
  }
}
watch(scopeJobId, () => { ++loadToken; data.value = null })
watch(scopeVersion, load)

function fmt(m: ProfileMetric): string {
  if (m.value === null || m.value === undefined) return '未披露'
  if (m.kind === 'money') return moneyWan(m.value as number)
  if (m.kind === 'pct') return `${((m.value as number) * 100).toFixed(1)}%`
  if (m.kind === 'num' || m.kind === 'count') return num(m.value as number)
  return String(m.value)
}

interface Req {
  no: string
  title: string
  text: string
  impl: string
  target: string | null
  summary: string
  ok: boolean
  go: () => void
}

const reqs = computed<Req[]>(() => {
  const d = data.value
  if (!d) return []
  const sg = d.suggestions
  const r = d.results
  const b = (x?: Task2Brief) => x && x.rows > 0
  const rel = (tab: number, q: Record<string, string | string[]>) => () => router.push({ path: '/relations', query: { tab: String(tab), ...q, job: scopeJobId.value || '__all__' } })
  const prof = (kind: string, org: string | null) => () => org && router.push({ path: '/profile', query: { kind, org, job: scopeJobId.value || '__all__' } })
  const bp = r.buyer_profile, spf = r.supplier_profile
  return [
    {
      no: '总', title: '用户画像建模',
      text: '基于招采标讯公告中的竞标与得分数据……完成指定场景的用户画像',
      impl: '「陆 用户画像」：采购单位画像 + 供应商/投标主体画像（指标卡、标签、合作方、竞争对手、趋势、品目、品牌、关系子图）',
      target: [sg.buyer, sg.winner].filter(Boolean).join(' ／ ') || null,
      summary: bp && spf
        ? `采购单位标签：${bp.tags.join('、') || '无'}；供应商标签：${spf.tags.join('、') || '无'}；${spf.metrics.slice(0, 3).map((m) => `${m.label} ${fmt(m)}`).join('，')}`
        : '无数据',
      ok: !!(bp && spf), go: prof('buyer', sg.buyer),
    },
    {
      no: '1', title: '采购单位 → 长期/大量合作供应商',
      text: '指定任意采购单位，查询并输出与其存在长期/大量合作关系的中标供应商、产品供应商信息，如合作次数、交易总金额等',
      impl: '「伍 关系分析 · 一」：分别展示中标供应商、产品供应商（品牌）的合作次数与交易金额',
      target: sg.buyer,
      summary: r.s1 ? `${r.s1.rows} 家中标供应商，${r.s1.project_count} 个项目，已知金额 ${moneyWan(r.s1.known_amount)}；产品品牌 ${r.s1.secondary_rows} 个` : '无数据',
      ok: !!b(r.s1), go: rel(1, { buyer: sg.buyer ?? '' }),
    },
    {
      no: '2', title: '采购单位 → TOP5 投标主体与协同组合',
      text: '指定任意采购单位，查询并输出其历史招标项目中高频（TOP5）参与投标的主体，以及高频协同参与投标的主体组合',
      impl: '「伍 关系分析 · 二」：投标主体参与次数排名（图谱展示 TOP5）+ 两两共同参与组合表',
      target: sg.buyer,
      summary: r.s2 ? `${r.s2.rows} 个投标主体，第一名「${r.s2.top}」参与 ${r.s2.top_count} 次；协同组合 ${r.s2.secondary_rows} 组` : '无数据',
      ok: !!b(r.s2), go: rel(2, { buyer: sg.buyer ?? '' }),
    },
    {
      no: '3', title: '中标供应商 → TOP5 共同竞标主体',
      text: '指定任意中标供应商，查询并输出其历史项目中高频（TOP5）共同竞标的主体及全部参与投标主体信息，如频次、总金额等',
      impl: '「伍 关系分析 · 三」：全部历史参与项目（含中标、未中标）的共同竞标主体，含频次、共同项目总金额、对方报价、双方平均得分（图谱展示 TOP5）',
      target: sg.winner,
      summary: r.s3 ? `${r.s3.rows} 个共同竞标主体，${r.s3.project_count} 个参与项目共 ${moneyWan(r.s3.known_amount)}；第一名「${r.s3.top}」${r.s3.top_count} 次` : '无数据',
      ok: !!b(r.s3), go: rel(3, { one: sg.winner ?? '' }),
    },
    {
      no: '4', title: '多家中标供应商 → 共同合作采购单位',
      text: '指定任意两个或多个中标供应商，查询并输出与上述主体均存在合作关系的采购单位、合作频次及合作金额等',
      impl: '「伍 关系分析 · 四」：采购单位交集，逐家合作次数与已归属金额，去重项目数与总金额',
      target: sg.pair4.join(' ＋ ') || null,
      summary: r.s4 ? `${r.s4.rows} 个共同采购单位，${r.s4.project_count} 个项目，已知金额 ${moneyWan(r.s4.known_amount)}` : '无数据',
      ok: !!b(r.s4), go: rel(4, { many: sg.pair4 }),
    },
    {
      no: '5', title: '多家中标供应商 → 共同竞标项目与结果',
      text: '指定任意两个或多个中标供应商，查询并输出其历史共同参与竞标的项目信息与竞标结果，包含项目数量、项目总金额等核心指标',
      impl: '「伍 关系分析 · 五」：共同项目逐家结果/得分/排名/报价、中标方，汇总表给出各家中标次数与占比',
      target: sg.pair5.join(' ＋ ') || null,
      summary: r.s5 ? `共同竞标 ${r.s5.project_count} 个项目，总金额 ${moneyWan(r.s5.known_amount)}` : '无数据',
      ok: !!b(r.s5), go: rel(5, { many: sg.pair5 }),
    },
  ]
})
const allOk = computed(() => reqs.value.length > 0 && reqs.value.every((x) => x.ok))
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1><span class="no">★</span>任务二 · 用户画像建模与关系分析</h1>
        <div class="sub">查看所选任务的关系模型、统计结果和主体画像。</div>
      </div>
      <el-button :loading="loading" @click="load">重新计算</el-button>
    </div>

    <ResultScope :job-id="scopeJobId" :jobs="scopeJobs" :loading="scopeLoading" :total="scopeTotal" :error="scopeError" @change="pickScope" />

    <div v-if="error" class="banner err">{{ error }}</div>

    <template v-if="data && !scopeLoading && !scopeError">
      <div class="banner" :class="allOk ? 'ok' : 'warn'">
        <b>{{ allOk ? '关系模型已生成' : '部分场景暂无关联记录' }}</b>
        <span class="hint" style="margin-left: 8px">计算耗时 {{ ms(data.query_ms) }}</span>
      </div>

      <section class="sheet" style="margin-top: 14px">
        <h2 class="sheet-title">采购单位 - 中标供应商 - 投标参与方 关系模型</h2>
        <div class="model">
          <div class="node buyer"><div class="n">{{ num(data.model.buyers) }}</div><div class="k">采购单位</div></div>
          <div class="edge"><div class="n">{{ num(data.model.edges_buyer_winner) }}</div><div class="k">合作（中标）关系 →</div></div>
          <div class="node winner"><div class="n">{{ num(data.model.winners) }}</div><div class="k">中标供应商</div></div>
          <div class="edge"><div class="n">{{ num(data.model.edges_co_bid) }}</div><div class="k">↔ 共同竞标关系</div></div>
          <div class="node part"><div class="n">{{ num(data.model.participants) }}</div><div class="k">投标参与方（未中标）</div></div>
        </div>
        <div class="stats" style="margin-top: 12px">
          <div class="stat"><div class="k">公告</div><div class="v">{{ num(data.model.announcements) }}</div></div>
          <div class="stat"><div class="k">业务项目（去重）</div><div class="v">{{ num(data.model.projects) }}</div></div>
          <div class="stat"><div class="k">采购包</div><div class="v">{{ num(data.model.packages) }}</div></div>
          <div class="stat"><div class="k">产品供应商（品牌）</div><div class="v">{{ num(data.model.product_suppliers ?? 0) }}</div></div>
          <div class="stat"><div class="k">采购单位—产品供应商关系</div><div class="v">{{ num(data.model.edges_buyer_product_supplier ?? 0) }}</div></div>
          <div class="stat"><div class="k">采购单位—参与方关系</div><div class="v">{{ num(data.model.edges_buyer_participant) }}</div></div>
          <div class="stat"><div class="k">竞标记录</div><div class="v">{{ num(data.model.bid_records) }}</div></div>
          <div class="stat"><div class="k">其中带综合得分</div><div class="v">{{ num(data.model.bid_with_score) }}</div></div>
          <div class="stat"><div class="k">其中带投标报价</div><div class="v">{{ num(data.model.bid_with_amount) }}</div></div>
        </div>

      </section>

      <section class="sheet" style="margin-top: 16px">
        <h2 class="sheet-title">关系与画像查询</h2>
        <table class="req">
          <thead><tr><th style="width: 46px">#</th><th>查询场景</th><th>查询对象</th><th>结果概览</th><th style="width: 80px"></th></tr></thead>
          <tbody>
            <tr v-for="q in reqs" :key="q.no">
              <td><span class="badge" :class="{ ok: q.ok }">{{ q.ok ? '✓' : '—' }}</span> <b>{{ q.no }}</b></td>
              <td><div class="t">{{ q.title }}</div></td>
              <td class="tgt">{{ q.target ?? '—' }}</td>
              <td class="sum">{{ q.summary }}</td>
              <td><el-button size="small" type="primary" :disabled="!q.target" @click="q.go">打开</el-button></td>
            </tr>
          </tbody>
        </table>

      </section>
    </template>
    <div v-else-if="loading" class="banner info">正在计算……</div>
  </div>
</template>

<style scoped>
.page-head { display: flex; justify-content: space-between; align-items: flex-end; }
.banner.ok { background: #e9f3ea; border: 1px solid #b9d7bc; color: #24502a; padding: 10px 14px; }
.model { display: grid; grid-template-columns: 1fr 1.1fr 1fr 1.1fr 1fr; align-items: center; gap: 8px; }
.node { border: 2px solid var(--indigo); padding: 14px; text-align: center; background: var(--card); }
.node.buyer { border-color: var(--cinnabar); }
.node.part { border-style: dashed; }
.node .n, .edge .n { font-family: var(--serif); font-size: 24px; font-variant-numeric: tabular-nums; }
.node .k { font-size: 13px; color: var(--ink-2); margin-top: 2px; }
.edge { text-align: center; border-bottom: 2px solid var(--rule); padding-bottom: 6px; }
.edge .n { font-size: 18px; }
.edge .k { font-size: 12px; color: var(--ink-3); }
.req { width: 100%; border-collapse: collapse; font-size: 13px; }
.req th { text-align: left; font-weight: 600; color: var(--ink-3); font-size: 12px; border-bottom: 1px solid var(--ink); padding: 8px; }
.req td { border-bottom: 1px solid var(--rule-2); padding: 10px 8px; vertical-align: top; line-height: 1.6; }
.req .t { font-weight: 600; margin-bottom: 2px; }
.req .impl { color: var(--ink-2); width: 26%; }
.req .tgt { width: 15%; color: var(--indigo); }
.req .sum { width: 24%; }
.badge { display: inline-grid; place-items: center; width: 18px; height: 18px; border-radius: 50%; background: var(--rule); color: #fff; font-size: 11px; }
.badge.ok { background: #3d7a45; }
@media (max-width: 1200px) { .model { grid-template-columns: 1fr; } }
</style>
