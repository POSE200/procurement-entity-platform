<script setup lang="ts">
defineOptions({ name: 'ProfileView' })
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Cell from '@/components/Cell.vue'
import EvidenceDrawer from '@/components/EvidenceDrawer.vue'
import GraphPanel from '@/components/GraphPanel.vue'
import OrgPicker from '@/components/OrgPicker.vue'
import ResultScope from '@/components/ResultScope.vue'
import { useResultScope } from '@/utils/useResultScope'
import { api } from '@/api/client'
import type { GraphEdge, GraphNode, ProfileKind, ProfileMetric, ProfileResponse, Scope } from '@/types/api'
import { moneyWan, num } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const { jobId: scopeJobId, jobs: scopeJobs, loading: scopeLoading, total: scopeTotal, error: scopeError, version: scopeVersion, pick: pickScope } = useResultScope()
let runToken = 0
let lastScope: string | null = null
onBeforeUnmount(() => ++runToken)

const form = reactive({
  kind: (route.query.kind === 'supplier' ? 'supplier' : 'buyer') as ProfileKind,
  org: String(route.query.org ?? ''),
  range: null as [string, string] | null,
  scope: 'project' as Scope,
})
const res = ref<ProfileResponse | null>(null)
const loading = ref(false)
const error = ref('')
const drawer = reactive({ open: false, ids: [] as string[], title: '' })

async function run() {
  if (!form.org || scopeLoading.value || scopeError.value) return
  const token = ++runToken
  loading.value = true
  error.value = ''
  try {
    const result = await api.profile(form.kind, { org: form.org, date_range: form.range, scope: form.scope, job_id: scopeJobId.value })
    if (token !== runToken) return
    res.value = result
    router.replace({ path: '/profile', query: { kind: form.kind, org: form.org, job: scopeJobId.value || '__all__' } })
  } catch (e) {
    if (token !== runToken) return
    res.value = null
    error.value = (e as Error).message
  } finally {
    if (token === runToken) loading.value = false
  }
}

function fmt(m: ProfileMetric): string {
  if (m.value === null || m.value === undefined) return '未披露'
  if (m.kind === 'money') return moneyWan(m.value as number)
  if (m.kind === 'pct') return `${((m.value as number) * 100).toFixed(1)}%`
  if (m.kind === 'num' || m.kind === 'count') return num(m.value as number)
  return String(m.value)
}

const isBuyer = computed(() => res.value?.kind === 'buyer')
const partnerTitle = computed(() => (isBuyer.value ? '主要中标供应商（长期/大量合作）' : '主要客户（采购单位）'))
const bidderTitle = computed(() => (isBuyer.value ? '高频投标主体' : '主要竞争对手'))
const trendMax = computed(() => Math.max(1, ...(res.value?.trend ?? []).map((t) => t.count)))
const catMax = computed(() => Math.max(1, ...(res.value?.categories ?? []).map((t) => t.count)))

// 画像关系子图：中心主体 + 合作方 + 竞争/投标主体
const graph = computed(() => {
  const r = res.value
  if (!r) return { nodes: [] as GraphNode[], edges: [] as GraphEdge[] }
  const nodes: GraphNode[] = [{ id: r.org_id, label: r.org, kind: 'focus' }]
  const edges: GraphEdge[] = []
  const seen = new Set([r.org_id])
  for (const p of r.top_partners.slice(0, 6)) {
    if (seen.has(p._key)) continue
    seen.add(p._key)
    nodes.push({ id: p._key, label: p.name, kind: isBuyer.value ? 'supplier' : 'buyer', weight: p.count })
    edges.push({ source: r.org_id, target: p._key, label: isBuyer.value ? `合作 ${p.count}` : `中标 ${p.count}`, weight: p.count })
  }
  for (const b of r.top_bidders.slice(0, 6)) {
    if (seen.has(b._key)) continue
    seen.add(b._key)
    nodes.push({ id: b._key, label: b.name, kind: 'supplier', weight: b.count })
    edges.push({ source: r.org_id, target: b._key, label: isBuyer.value ? `投标 ${b.count}` : `交手 ${b.count}`, weight: b.count })
  }
  return { nodes, edges }
})

function evidence(key: string, title: string) {
  const ids = res.value?.evidence_refs[key] ?? []
  if (!ids.length) return
  Object.assign(drawer, { ids, title, open: true })
}
function jump(name: string, kind: ProfileKind) {
  form.kind = kind
  form.org = name
  run()
}

watch(() => form.kind, (_k, old) => {
  if (old && !route.query.org) form.org = ''
  res.value = null
})
watch(() => route.query, (q) => {
  if (route.path === '/profile' && !scopeLoading.value && q.org && (q.org !== form.org || q.kind !== form.kind)) {
    form.kind = q.kind === 'supplier' ? 'supplier' : 'buyer'
    form.org = String(q.org)
    run()
  }
})
watch(scopeJobId, () => { ++runToken; loading.value = false; res.value = null; drawer.open = false })
watch(scopeVersion, () => {
  const current = scopeJobId.value || '__all__'
  if (lastScope !== null && lastScope !== current) form.org = ''
  lastScope = current
  if (route.query.org) form.org = String(route.query.org)
  error.value = ''
  if (form.org) run()
})
</script>
<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1><span class="no">陆</span>用户画像</h1>
        <div class="sub">查看采购单位、供应商的合作关系与投标表现。</div>
      </div>
    </div>

    <ResultScope :job-id="scopeJobId" :jobs="scopeJobs" :loading="scopeLoading" :total="scopeTotal" :error="scopeError" @change="pickScope" />

    <section class="sheet query">
      <div>
        <label class="lbl">画像对象</label>
        <el-radio-group v-model="form.kind">
          <el-radio-button value="buyer">采购单位</el-radio-button>
          <el-radio-button value="supplier">供应商 / 投标主体</el-radio-button>
        </el-radio-group>
      </div>
      <div class="q-main">
        <label class="lbl">{{ form.kind === 'buyer' ? '采购单位' : '投标主体' }}</label>
        <OrgPicker :job-id="scopeJobId" :key="form.kind" v-model="form.org" :role="form.kind === 'buyer' ? 'buyer' : 'bidder'" placeholder="输入名称检索" />
      </div>
      <div>
        <label class="lbl">公告时间</label>
        <el-date-picker v-model="form.range" type="daterange" value-format="YYYY-MM-DD" start-placeholder="不限" end-placeholder="不限" unlink-panels style="width: 100%" />
      </div>
      <div>
        <label class="lbl">统计单位</label>
        <el-radio-group v-model="form.scope">
          <el-radio-button value="project">项目级</el-radio-button>
          <el-radio-button value="package">同包级</el-radio-button>
        </el-radio-group>
      </div>
      <div><el-button type="primary" :loading="loading" :disabled="!form.org" @click="run">生成画像</el-button></div>
    </section>

    <div v-if="error" class="banner err" style="margin-top: 12px">{{ error }}</div>
    <template v-if="res && !scopeLoading && !scopeError">
      <div class="banner info" style="margin-top: 14px">
        <div><b>{{ res.org }}</b>：{{ res.definition }}
          <span v-for="t in res.tags" :key="t" class="ptag">{{ t }}</span>
        </div>
      </div>

      <div class="stats metrics" style="margin-top: 14px">
        <div v-for="m in res.metrics" :key="m.label" class="stat">
          <div class="k">{{ m.label }}</div>
          <div class="v" :class="{ miss: m.value === null }">{{ fmt(m) }}</div>
        </div>
      </div>

      <div class="grid2">
        <section class="sheet">
          <h2 class="sheet-title">{{ partnerTitle }}</h2>
          <el-table :data="res.top_partners" border max-height="340" empty-text="无" row-class-name="clickable"
            @row-click="(r: any) => evidence(r._key, `统计依据 · ${r.name}`)">
            <el-table-column :label="isBuyer ? '中标供应商' : '采购单位'" min-width="200" show-overflow-tooltip>
              <template #default="{ row }">
                <a href="#" @click.prevent.stop="jump(row.name, isBuyer ? 'supplier' : 'buyer')">{{ row.name }}</a>
              </template>
            </el-table-column>
            <el-table-column v-if="!isBuyer" label="投标次数" align="right" width="90"><template #default="{ row }"><Cell :value="row.bids" kind="count" /></template></el-table-column>
            <el-table-column :label="isBuyer ? '合作次数' : '中标次数'" align="right" width="90"><template #default="{ row }"><Cell :value="row.count" kind="count" /></template></el-table-column>
            <el-table-column label="已归属金额" align="right" min-width="130"><template #default="{ row }"><Cell :value="row.amount" kind="money" /></template></el-table-column>
            <el-table-column v-if="isBuyer" label="份额" align="right" width="80"><template #default="{ row }">{{ (row.share * 100).toFixed(0) }}%</template></el-table-column>
          </el-table>
        </section>
        <section class="sheet">
          <h2 class="sheet-title">{{ bidderTitle }}</h2>
          <el-table :data="res.top_bidders" border max-height="340" empty-text="无" row-class-name="clickable"
            @row-click="(r: any) => evidence(r._key, `统计依据 · ${r.name}`)">
            <el-table-column label="投标主体" min-width="200" show-overflow-tooltip>
              <template #default="{ row }"><a href="#" @click.prevent.stop="jump(row.name, 'supplier')">{{ row.name }}</a></template>
            </el-table-column>
            <el-table-column :label="isBuyer ? '参与次数' : '交手次数'" align="right" width="90"><template #default="{ row }"><Cell :value="row.count" kind="count" /></template></el-table-column>
            <template v-if="!isBuyer">
              <el-table-column label="本方胜" align="right" width="80"><template #default="{ row }"><Cell :value="row.beat" kind="count" /></template></el-table-column>
              <el-table-column label="对方胜" align="right" width="80"><template #default="{ row }"><Cell :value="row.lost" kind="count" /></template></el-table-column>
            </template>
          </el-table>
        </section>
      </div>
      <div class="grid2">
        <section class="sheet">
          <h2 class="sheet-title">画像关系子图 <span class="hint">节点 {{ graph.nodes.length }} · 边 {{ graph.edges.length }}</span></h2>
          <GraphPanel :nodes="graph.nodes" :edges="graph.edges" @select="(id: string) => evidence(id, '统计依据')" />
        </section>
        <section class="sheet">
          <h2 class="sheet-title">{{ isBuyer ? '月度采购趋势' : '月度中标趋势' }}</h2>
          <div v-if="!res.trend.length" class="hint">无日期信息</div>
          <div v-for="t in res.trend" :key="t.month" class="bar-row">
            <span class="bar-k mono">{{ t.month }}</span>
            <span class="bar"><i :style="{ width: (t.count / trendMax) * 100 + '%' }" /></span>
            <span class="bar-v">{{ t.count }} 个 · {{ moneyWan(t.amount) }}</span>
          </div>
          <h2 class="sheet-title" style="margin-top: 18px">{{ isBuyer ? '常采品目' : '中标品目' }}</h2>
          <div v-if="!res.categories.length" class="hint">标的明细未披露品目</div>
          <div v-for="c in res.categories" :key="c.name" class="bar-row">
            <span class="bar-k" :title="c.name">{{ c.name }}</span>
            <span class="bar"><i class="alt" :style="{ width: (c.count / catMax) * 100 + '%' }" /></span>
            <span class="bar-v">{{ c.count }} 条</span>
          </div>
          <h2 class="sheet-title" style="margin-top: 18px">产品品牌</h2>
          <div class="hint" v-if="!res.brands.length">未披露品牌</div>
          <span v-for="b in res.brands" :key="b.name" class="ptag soft">{{ b.name }} · {{ b.count }}</span>
        </section>
      </div>
      
    </template>
    <div v-else-if="!loading && !error" class="banner info" style="margin-top: 14px">选择画像对象后点击“生成画像”。</div>

    <EvidenceDrawer v-model="drawer.open" :announcement-ids="drawer.ids" :title="drawer.title" />
  </div>
</template>

<style scoped>
.query { display: grid; grid-template-columns: auto minmax(260px, 2fr) 1.4fr auto auto; gap: 14px; align-items: end; }
.lbl { display: block; font-size: 12px; color: var(--ink-3); margin-bottom: 6px; }
.metrics { grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); }
.metrics .stat { border-bottom: 1px solid var(--rule-2); }
.metrics .v { font-size: 19px; }
.metrics .v.miss { color: var(--ink-3); font-size: 14px; font-style: italic; }
.grid2 { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; margin-top: 16px; }
.grid2 .sheet + .sheet { margin-top: 0; }
.ptag { display: inline-block; margin-left: 8px; padding: 1px 8px; font-size: 12px; border: 1px solid var(--cinnabar); color: var(--cinnabar); border-radius: 2px; }
.ptag.soft { margin: 0 6px 6px 0; border-color: var(--rule); color: var(--ink-2); }
.bar-row { display: grid; grid-template-columns: 110px 1fr 150px; gap: 10px; align-items: center; font-size: 12px; margin: 4px 0; }
.bar-k { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ink-2); }
.bar { height: 10px; background: var(--indigo-soft); }
.bar i { display: block; height: 100%; background: var(--indigo); }
.bar i.alt { background: var(--cinnabar); opacity: 0.75; }
.bar-v { color: var(--ink-3); text-align: right; font-variant-numeric: tabular-nums; }
:deep(.clickable) { cursor: pointer; }
@media (max-width: 1200px) { .query { grid-template-columns: 1fr 1fr; } .grid2 { grid-template-columns: 1fr; } }
</style>


