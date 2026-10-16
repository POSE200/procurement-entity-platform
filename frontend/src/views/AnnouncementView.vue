<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Cell from '@/components/Cell.vue'
import EvidenceDrawer from '@/components/EvidenceDrawer.vue'
import ResultScope from '@/components/ResultScope.vue'
import { useResultScope } from '@/utils/useResultScope'
import { api } from '@/api/client'
import type { AnnouncementDetail, AnnouncementSummary, Item } from '@/types/api'
import { splitHighlight } from '@/utils/evidence'
import { dateTime, moneyWan, text } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const { jobId: scopeJobId, jobs: scopeJobs, loading: scopeLoading, total: scopeTotal, error: scopeError, version: scopeVersion, pick: pickScope } = useResultScope()
let loadToken = 0
let searchToken = 0
const id = computed(() => String(route.params.id ?? ''))
const d = ref<AnnouncementDetail | null>(null)
const error = ref('')
const loading = ref(false)
const kw = ref('')
const results = ref<AnnouncementSummary[]>([])
const tab = ref<'body' | 'summary'>('body')
const highlight = ref(true)
const drawer = ref(false)
const focus = ref<Item | null>(null)

async function load() {
  if (scopeLoading.value || scopeError.value) return
  const token = ++loadToken
  error.value = ''
  d.value = null
  if (!id.value) { searchList(); return }
  loading.value = true
  try {
    const result = await api.announcement(id.value, scopeJobId.value)
    if (token === loadToken) d.value = result
  } catch (e) { if (token === loadToken) error.value = (e as Error).message } finally { if (token === loadToken) loading.value = false }
}
async function searchList() {
  if (scopeLoading.value || scopeError.value) return
  const token = ++searchToken
  try {
    const result = await api.announcements(kw.value, scopeJobId.value)
    if (token === searchToken) results.value = result
  } catch (e) { if (token === searchToken) error.value = (e as Error).message }
}
watch(id, load)
watch(scopeJobId, () => { ++loadToken; ++searchToken; d.value = null; results.value = [] })
watch(scopeVersion, load)

// 高亮已抽取的主体与标的名称，方便对照原文核查
const terms = computed(() => {
  if (!d.value || !highlight.value) return []
  return [d.value.buyer, ...d.value.bidders.map((b) => b.name), ...d.value.items.map((i) => i.item_name)].filter((x): x is string => !!x)
})
const bodyParts = computed(() => (d.value ? splitHighlight(tab.value === 'body' ? d.value.body_text : d.value.summary_text, terms.value) : []))
const winners = computed(() => d.value?.bidders.filter((b) => b.role === 'winner') ?? [])
const participants = computed(() => d.value?.bidders.filter((b) => b.role === 'participant') ?? [])
const itemSum = computed(() => {
  const its = d.value?.items ?? []
  const vals = its.map((i) => i.total_price ?? (i.unit_price !== null && i.quantity !== null ? i.unit_price * i.quantity : null))
  return { known: vals.filter((v): v is number => v !== null).reduce((a, b) => a + b, 0), unknown: vals.filter((v) => v === null).length }
})
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1><span class="no">肆</span>公告详情</h1>
        <div class="sub">左侧是结构化结果，右侧是公告原文，已抽取的主体与标的名称会高亮</div>
      </div>
      <el-button v-if="id" @click="router.push('/announcements')">返回公告列表</el-button>
    </div>

    <ResultScope :job-id="scopeJobId" :jobs="scopeJobs" :loading="scopeLoading" :total="scopeTotal" :error="scopeError" @change="pickScope" />

    <div v-if="error" class="banner err">{{ error }}</div>

    <!-- 列表模式 -->
    <section v-if="!id" class="sheet">
      <div class="row" style="margin-bottom: 12px">
        <el-input v-model="kw" placeholder="按标题、公告 ID 或采购单位搜索" clearable style="max-width: 420px" @keyup.enter="searchList" @clear="searchList" />
        <el-button type="primary" @click="searchList">搜索</el-button>
        <span class="hint">最多显示 50 条</span>
      </div>
      <el-table :data="results" border height="calc(100vh - 330px)" @row-click="(r: AnnouncementSummary) => router.push(`/announcements/${r.announcement_id}`)">
        <el-table-column prop="title" label="公告标题" min-width="380" show-overflow-tooltip />
        <el-table-column prop="buyer" label="采购单位" min-width="200" show-overflow-tooltip />
        <el-table-column label="发布时间" width="150"><template #default="{ row }">{{ dateTime(row.publish_time) }}</template></el-table-column>
        <el-table-column label="总中标金额" width="150" align="right"><template #default="{ row }"><span class="num">{{ moneyWan(row.total_amount) }}</span></template></el-table-column>
        <el-table-column prop="item_count" label="标的" width="70" align="right" />
        <el-table-column label="附件" width="70"><template #default="{ row }">{{ row.has_attachment ? '有' : '无' }}</template></el-table-column>
      </el-table>
    </section>

    <div v-else-if="loading" class="hint">加载中…</div>

    <template v-else-if="d">
      <div class="doc-title">
        <h2 class="serif">{{ d.title }}</h2>
        <div class="meta">
          <span>公告 ID <b class="mono">{{ d.announcement_id }}</b></span>
          <span>项目编号 <b class="mono">{{ text(d.project_no) }}</b></span>
          <span>发布时间 <b>{{ dateTime(d.publish_time) }}</b></span>
          <span>附件 <b>{{ d.has_attachment ? '已提供' : '未提供' }}</b></span>
          <span v-if="d.gold_source">金额来源 <b>{{ d.gold_source }}</b></span>
        </div>
      </div>

      <div class="split">
        <div class="left">
          <section class="sheet">
            <h3 class="sheet-title">项目与采购包</h3>
            <dl class="kv">
              <dt>采购单位</dt><dd>{{ text(d.buyer) }}</dd>
              <dt>项目名称</dt><dd>{{ text(d.project_name) }}</dd>
              <dt>总中标金额</dt><dd class="num" style="text-align: left">{{ moneyWan(d.total_amount) }}</dd>
            </dl>
            <el-table :data="d.packages" border size="small" style="margin-top: 10px" empty-text="未拆分采购包">
              <el-table-column label="包号" width="90"><template #default="{ row }"><Cell :value="row.package_no" /></template></el-table-column>
              <el-table-column label="包名称" min-width="140"><template #default="{ row }"><Cell :value="row.package_name" /></template></el-table-column>
              <el-table-column label="中标供应商" min-width="180"><template #default="{ row }"><Cell :value="row.winner" /></template></el-table-column>
            </el-table>
          </section>

          <section class="sheet">
            <h3 class="sheet-title">投标主体</h3>
            <div class="hint" style="margin-bottom: 6px">中标 {{ winners.length }} · 其他参与方 {{ participants.length }}</div>
            <div class="chips">
              <span v-for="b in winners" :key="'w' + b.name + b.package_no" class="tag win">{{ b.package_no ? `包${b.package_no} · ` : '' }}{{ b.name }}</span>
              <span v-for="b in participants" :key="'p' + b.name + b.package_no" class="tag lose">{{ b.package_no ? `包${b.package_no} · ` : '' }}{{ b.name }}</span>
            </div>
          </section>

          <section class="sheet">
            <h3 class="sheet-title">标的物明细（{{ d.items.length }}）</h3>
            <el-table :data="d.items" border size="small" max-height="420" empty-text="原文未披露标的物明细"
              @row-click="(r: Item) => { focus = r; drawer = true }">
              <el-table-column label="名称" min-width="140" show-overflow-tooltip><template #default="{ row }"><Cell :value="row.item_name" /></template></el-table-column>
              <el-table-column label="品牌" width="100" show-overflow-tooltip><template #default="{ row }"><Cell :value="row.brand" /></template></el-table-column>
              <el-table-column label="型号" width="120" show-overflow-tooltip><template #default="{ row }"><Cell :value="row.model" /></template></el-table-column>
              <el-table-column label="单价" width="120" align="right"><template #default="{ row }"><Cell :value="row.unit_price" kind="money" /></template></el-table-column>
              <el-table-column label="数量" width="80" align="right"><template #default="{ row }"><Cell :value="row.quantity" kind="count" /></template></el-table-column>
              <el-table-column label="总价" width="130" align="right"><template #default="{ row }"><Cell :value="row.total_price" kind="money" /></template></el-table-column>
            </el-table>
            <p class="hint">明细可计算合计 {{ moneyWan(itemSum.known) }}，{{ itemSum.unknown }} 条缺少金额。</p>
          </section>

          <section v-if="d.review_note" class="sheet">
            <h3 class="sheet-title">复核说明</h3>
            <p class="note">{{ d.review_note }}</p>
          </section>
        </div>

        <section class="sheet right">
          <div class="row" style="margin-bottom: 10px">
            <el-radio-group v-model="tab" size="small">
              <el-radio-button value="body">正文</el-radio-button>
              <el-radio-button value="summary">公告概要</el-radio-button>
            </el-radio-group>
            <span class="grow" />
            <el-checkbox v-model="highlight">高亮已抽取实体</el-checkbox>
          </div>
          <div class="doc"><template v-for="(p, i) in bodyParts" :key="i"><mark v-if="p.hit">{{ p.t }}</mark><template v-else>{{ p.t }}</template></template></div>
        </section>
      </div>
      <EvidenceDrawer v-model="drawer" :announcement-id="d.announcement_id" :focus-item="focus" />
    </template>
  </div>
</template>

<style scoped>
.doc-title h2 { margin: 0 0 8px; font-size: 20px; line-height: 1.5; }
.meta { display: flex; flex-wrap: wrap; gap: 6px 20px; font-size: 13px; color: var(--ink-3); margin-bottom: 16px; }
.meta b { color: var(--ink); font-weight: 500; }
.split { display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr); gap: 16px; align-items: start; }
.left .sheet + .sheet { margin-top: 16px; }
.right { position: sticky; top: 84px; }
.doc { white-space: pre-wrap; font-size: 13px; line-height: 1.9; max-height: calc(100vh - 170px); overflow: auto; padding-right: 8px; color: var(--ink-2); }
.kv { display: grid; grid-template-columns: 88px 1fr; gap: 6px 12px; margin: 0; font-size: 13px; }
.kv dt { color: var(--ink-3); }
.kv dd { margin: 0; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.note { font-size: 13px; line-height: 1.8; color: var(--ink-2); margin: 0; white-space: pre-wrap; }
mark { background: #f1dfae; color: var(--ink); }
@media (max-width: 1100px) { .split { grid-template-columns: 1fr; } .right { position: static; } }
</style>
