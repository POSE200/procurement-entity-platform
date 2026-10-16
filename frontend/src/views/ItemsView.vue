<script setup lang="ts">
defineOptions({ name: 'ItemsView' })
import { computed, onActivated, onDeactivated, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'
import Cell from '@/components/Cell.vue'
import EvidenceDrawer from '@/components/EvidenceDrawer.vue'
import { api } from '@/api/client'
import { downloadFile } from '@/utils/download'
import type { Item, ItemQuery } from '@/types/api'
import { csvCell, date } from '@/utils/format'
import { ALL_RESULTS, rememberResultScope, resolveResultScope, resultScopeName } from '@/utils/resultScope'

const q = reactive<ItemQuery>({ q: '', buyer: '', supplier: '', brand: '', date_from: '', date_to: '', page: 1, page_size: 50, sort: '' })
const route = useRoute()
const router = useRouter()
const range = ref<[string, string] | null>(null)
const rows = ref<Item[]>([])
const total = ref(0)
const loading = ref(false)
const exporting = ref(false)
const error = ref('')
const drawer = reactive({ open: false, annId: '', item: null as Item | null })
let loadToken = 0
const jobs = ref<Awaited<ReturnType<typeof api.jobs>>>([])
const selectedJob = computed(() => jobs.value.find(j => j.job_id === q.job_id))

const scopeLabel = computed(() => {
  if (q.announcement_id) return '当前公告结果'
  return resultScopeName(q.job_id || '', jobs.value)
})
const scoped = computed(() => !!q.job_id || !!q.announcement_id)

async function load() {
  const token = ++loadToken
  loading.value = true
  error.value = ''
  try {
    const res = await api.items({ ...q })
    if (token !== loadToken) return
    rows.value = res.rows
    total.value = res.total
  } catch (e) {
    if (token !== loadToken) return
    rows.value = []
    total.value = 0
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

function search() {
  q.page = 1
  q.date_from = range.value?.[0] ?? ''
  q.date_to = range.value?.[1] ?? ''
  load()
}
function reset() {
  Object.assign(q, { q: '', buyer: '', supplier: '', brand: '', date_from: '', date_to: '', page: 1, sort: '' })
  range.value = null
  load()
}
async function applyRouteScope() {
  if (route.path !== '/items') return
  try { jobs.value = await api.jobs() } catch (e) { error.value = (e as Error).message; return }
  const choice = resolveResultScope(route.query.job, jobs.value)
  if (!route.query.job && !route.query.announcement) { await router.replace({ path: '/items', query: { job: choice } }); return }
  const jobId = choice === ALL_RESULTS ? '' : choice
  const announcementId = String(route.query.announcement ?? '')
  q.job_id = route.query.announcement && !route.query.job ? undefined : jobId || undefined
  q.announcement_id = announcementId || undefined
  q.page = 1
  load()
}
function clearScope() {
  pickJob(ALL_RESULTS)
}
function pickJob(id: string) {
  rememberResultScope(id)
  router.replace({ path: '/items', query: { job: id } })
}
function onSort({ prop, order }: { prop: string; order: 'ascending' | 'descending' | null }) {
  q.sort = order ? (order === 'descending' ? '-' : '') + prop : ''
  q.page = 1
  load()
}
function openEvidence(row: Item) {
  drawer.annId = row.announcement_id
  drawer.item = row
  drawer.open = true
}

const COLS: [keyof Item, string][] = [
  ['item_name', '标的名称'], ['category', '品目'], ['brand', '品牌'], ['model', '规格型号'], ['unit_price', '单价(元)'],
  ['quantity', '数量'], ['unit', '单位'], ['total_price', '总价(元)'], ['supplier', '中标供应商'], ['buyer', '采购单位'],
  ['publish_time', '公告日期'], ['announcement_id', '公告ID'], ['package_no', '包号'],
]
async function exportCsv() {
  exporting.value = true
  try {
    const all = await api.exportItems({ ...q, page: 1, page_size: total.value })
    const lines = [COLS.map((c) => c[1]).join(',')]
    for (const r of all) lines.push(COLS.map(([k]) => csvCell(k === 'publish_time' ? (r[k] ? date(r[k] as string) : '') : r[k])).join(','))
    // 带 BOM，Excel 打开中文不乱码；空值导出为空单元格而不是 0
    const blob = new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `任务一七字段_${q.announcement_id || q.job_id || '全部'}_${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
    URL.revokeObjectURL(a.href)
    ElMessage.success(`已导出 ${all.length} 条`)
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    exporting.value = false
  }
}

async function downloadStandard() {
  exporting.value = true
  try {
    const scope = q.announcement_id ? { announcement_id: q.announcement_id } : q.job_id ? { job_id: q.job_id } : {}
    await downloadFile(api.standardExportUrl(scope), '提取结果_标准样例表.xlsx')
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    exporting.value = false
  }
}

async function saveJson() {
  exporting.value = true
  try {
    const all = await api.exportItems({ ...q, page: 1, page_size: total.value })
    const payload = {
      task: '任务一·标的物七字段',
      scope: scopeLabel.value,
      job_id: q.job_id ?? null,
      announcement_id: q.announcement_id ?? null,
      saved_at: new Date().toISOString(),
      fields: ['标的名称', '品目', '品牌', '规格型号', '单价', '数量', '总价'],
      items: all,
    }
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `任务一七字段_${q.announcement_id || q.job_id || '全部'}_${new Date().toISOString().slice(0, 10)}.json`
    a.click()
    URL.revokeObjectURL(a.href)
    ElMessage.success(`已保存 ${all.length} 条任务一七字段结果`)
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    exporting.value = false
  }
}

// KeepAlive 缓存的页面离开时关闭抽屉，避免遮罩残留在其他页面上
onDeactivated(() => (drawer.open = false))
watch(() => [route.query.job, route.query.announcement], applyRouteScope)
onMounted(applyRouteScope)
// ItemsView 被 KeepAlive 缓存时不会重新 mounted；每次回来都刷新，避免显示上一个任务的旧结果。
onActivated(applyRouteScope)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1><span class="no">叁</span>标的物检索</h1>
        <div class="sub">检索与导出所选任务的标的物明细。</div>
      </div>
      <div class="row">
        <el-select :model-value="q.job_id || ALL_RESULTS" style="width: 390px" aria-label="选择结果任务" @update:model-value="pickJob">
          <el-option :value="ALL_RESULTS" label="全部已入库数据（跨任务合并去重）" />
          <el-option v-for="job in jobs" :key="job.job_id" :value="job.job_id" :label="job.display_name" />
        </el-select>
        <el-button :loading="exporting" :disabled="loading || !total" @click="saveJson">保存当前结果（JSON）</el-button>
        <el-button :loading="exporting" :disabled="loading || !total" @click="exportCsv">导出当前结果（CSV）</el-button>
        <el-button type="primary" :loading="exporting" :disabled="loading || (selectedJob ? !selectedJob.stored : !total && !q.announcement_id)" @click="downloadStandard">下载所选范围 XLSX</el-button>
      </div>
    </div>

    <div class="task-context" :class="{ focused: scoped }">
      <div>
        <span class="tag ind">任务一</span>
        <b>{{ scopeLabel }}</b>
        <span v-if="q.job_id" class="mono"> · 任务 ID {{ q.job_id }}</span>
        <span v-if="q.announcement_id" class="mono"> · 公告 ID {{ q.announcement_id }}</span>
        <span v-if="!scoped" class="hint"> · 全部已入库公告</span>
      </div>
      <el-button v-if="scoped" text type="primary" @click="clearScope">查看全部结果</el-button>
    </div>

    <p class="hint">下载范围：{{ scopeLabel }}<template v-if="selectedJob"> · 已入库 {{ selectedJob.stored }} / {{ selectedJob.total }} 篇 · 空值占位 {{ selectedJob.skipped }} 篇</template> · XLSX：总表、标的表、投标人表</p>
    <section class="sheet task-fields">
      <div class="row">
        <b class="serif">任务一七字段</b>
        <span class="tag ind">公告 ID / 任务 ID</span>
        <span class="grow" />
        <span class="hint">点击任意行可查看该文件的原文证据</span>
      </div>
      <div class="field-tags">
        <span v-for="field in ['标的名称', '品目', '品牌', '规格型号', '单价', '数量', '总价']" :key="field" class="field-tag">{{ field }}</span>
      </div>
    </section>

    <section class="sheet filters">
      <el-input v-model="q.q" placeholder="标的名称 / 型号 / 公告ID / 品目 / 项目名" clearable @keyup.enter="search" />
      <el-input v-model="q.buyer" placeholder="采购单位" clearable @keyup.enter="search" />
      <el-input v-model="q.supplier" placeholder="中标供应商" clearable @keyup.enter="search" />
      <el-input v-model="q.brand" placeholder="品牌" clearable @keyup.enter="search" />
      <el-date-picker v-model="range" type="daterange" value-format="YYYY-MM-DD" start-placeholder="公告起" end-placeholder="公告止" unlink-panels />
      <div class="row">
        <el-button type="primary" @click="search">检索</el-button>
        <el-button @click="reset">重置</el-button>
      </div>
    </section>

    <div v-if="error" class="banner err" style="margin-top: 12px">{{ error }}</div>

    <section class="sheet" style="margin-top: 16px; padding-top: 12px">
      <div class="row" style="margin-bottom: 8px">
        <span class="serif">共 <b>{{ total.toLocaleString('zh-CN') }}</b> 条标的物</span>
        <span class="grow" />
        <span class="hint">每行首列为任务一样本标识</span>
      </div>
      <el-table v-loading="loading" :data="rows" border height="calc(100vh - 380px)" style="min-height: 380px" row-key="item_id"
        highlight-current-row @row-click="openEvidence" @sort-change="onSort">
        <el-table-column label="任务一·样本标识" width="245" fixed="left">
          <template #default="{ row }">
            <div class="sample-cell">
              <span class="tag ind">任务一</span>
              <RouterLink :to="`/announcements/${row.announcement_id}`" class="mono">{{ row.announcement_id }}</RouterLink>
              <span class="muted">最近入库任务：{{ row.job_id || '无任务 ID' }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="item_name" label="标的名称" min-width="200" sortable="custom" show-overflow-tooltip>
          <template #default="{ row }"><Cell :value="row.item_name" /></template>
        </el-table-column>
        <el-table-column prop="category" label="品目" min-width="160" show-overflow-tooltip>
          <template #default="{ row }"><Cell :value="row.category" /></template>
        </el-table-column>
        <el-table-column prop="brand" label="品牌" width="120" sortable="custom" show-overflow-tooltip>
          <template #default="{ row }"><Cell :value="row.brand" /></template>
        </el-table-column>
        <el-table-column prop="model" label="规格型号" min-width="150" show-overflow-tooltip>
          <template #default="{ row }"><Cell :value="row.model" /></template>
        </el-table-column>
        <el-table-column prop="unit_price" label="单价(元)" width="130" align="right" sortable="custom">
          <template #default="{ row }"><Cell :value="row.unit_price" kind="money" /></template>
        </el-table-column>
        <el-table-column prop="quantity" label="数量" width="100" align="right" sortable="custom">
          <template #default="{ row }"><Cell :value="row.quantity" kind="count" /><span v-if="row.unit && row.quantity !== null" class="muted"> {{ row.unit }}</span></template>
        </el-table-column>
        <el-table-column prop="total_price" label="总价(元)" width="140" align="right" sortable="custom">
          <template #default="{ row }"><Cell :value="row.total_price" kind="money" /></template>
        </el-table-column>
        <el-table-column prop="supplier" label="中标供应商" min-width="180" show-overflow-tooltip>
          <template #default="{ row }"><Cell :value="row.supplier" /></template>
        </el-table-column>
        <el-table-column prop="buyer" label="采购单位" min-width="160" show-overflow-tooltip>
          <template #default="{ row }"><Cell :value="row.buyer" /></template>
        </el-table-column>
        <el-table-column prop="publish_time" label="公告日期" width="110" sortable="custom">
          <template #default="{ row }"><Cell :value="row.publish_time" kind="date" /></template>
        </el-table-column>
      </el-table>
      <div class="row" style="justify-content: flex-end; margin-top: 12px">
        <el-pagination v-model:current-page="q.page" v-model:page-size="q.page_size" :total="total" :page-sizes="[20, 50, 100, 200]"
          layout="total, sizes, prev, pager, next, jumper" background @current-change="load" @size-change="() => { q.page = 1; load() }" />
      </div>
    </section>

    <EvidenceDrawer v-model="drawer.open" :announcement-id="drawer.annId" :focus-item="drawer.item" />
  </div>
</template>

<style scoped>
.task-context { margin: -8px 0 12px; padding: 9px 12px; border: 1px solid var(--rule); background: var(--paper-2); display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.task-context.focused { border-color: var(--indigo-2); background: var(--indigo-soft); }
.task-fields { margin-bottom: 16px; padding: 12px 16px; }
.field-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.field-tag { padding: 4px 10px; border: 1px solid #b9c4d8; background: #f4f6f9; color: var(--indigo); font-family: var(--serif); font-size: 13px; }
.sample-cell { display: flex; flex-direction: column; align-items: flex-start; gap: 2px; line-height: 1.35; }
.sample-cell .tag { line-height: 17px; }
.filters { display: grid; grid-template-columns: 2fr 1.4fr 1.4fr 1fr 1.6fr auto; gap: 10px; align-items: center; }
@media (max-width: 1200px) { .filters { grid-template-columns: 1fr 1fr 1fr; } }
</style>
