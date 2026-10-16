<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, appState, setDatasetId } from '@/api/client'
import type { DatasetInventory, InventoryEntry } from '@/types/api'
import { downloadFile } from '@/utils/download'

defineOptions({ name: 'UploadView' })

const router = useRouter()
const route = useRoute()
const saved = (key: string) => {
  try { return localStorage.getItem(key) ?? '' } catch { return '' }
}
const source = ref<'local' | 'upload'>('upload')
const inputKind = ref<'batch' | 'single'>(saved('ict.uploadKind') === 'single' ? 'single' : 'batch')
const htmlPath = ref(saved('ict.uploadHtmlPath'))
const attPath = ref(saved('ict.uploadAttachmentPath'))
if (inputKind.value === 'single' && /\.zip$/i.test(htmlPath.value)) htmlPath.value = ''
if (inputKind.value === 'batch' && /\.html?$/i.test(htmlPath.value)) htmlPath.value = ''
const limit = ref<number | undefined>(Number(saved('ict.uploadLimit')) || 20)
const modelReady = ref<boolean | null>(null)
const isSingle = computed(() => inputKind.value === 'single')
const inv = ref<DatasetInventory | null>(null)
let inventoryPoll: number | undefined
let inventoryRequest = 0
let pageActive = true

async function refreshInventory() {
  if (!appState.datasetId) {
    inv.value = null
    return
  }
  const datasetId = appState.datasetId
  const request = ++inventoryRequest
  try {
    const result = await api.inventory(datasetId)
    if (request === inventoryRequest && appState.datasetId === datasetId) inv.value = result
  } catch {
    // 保留当前清单，下一次刷新继续读取。
  }
}

const entries = computed(() => inv.value?.entries ?? [])
const processingCount = computed(() => entries.value.filter((e) => e.processing_status === 'queued' || e.processing_status === 'running').length)
const settled = (e: InventoryEntry) => e.processing_status === 'succeeded' || e.processing_status === 'skipped'
const processedCount = computed(() => entries.value.filter(settled).length)
const isProcessing = (e: InventoryEntry) => e.processing_status === 'queued' || e.processing_status === 'running'
const remainingEntries = computed(() => entries.value.filter((e) => !settled(e)))
const waitingEntries = computed(() => remainingEntries.value.filter((e) => !isProcessing(e)))
const pendingHtmlCount = computed(() => waitingEntries.value.filter((e) => e.html_present).length)
const pendingAttachmentCount = computed(() => waitingEntries.value.filter((e) => e.attachment_present).length)
const pendingPaired = computed(() => waitingEntries.value.filter((e) => e.status === 'paired').length)
const pendingHtmlOnly = computed(() => waitingEntries.value.filter((e) => e.status === 'html_only').length)
const pendingAttachmentOnly = computed(() => waitingEntries.value.filter((e) => e.status === 'attachment_only').length)
const pendingConflicts = computed(() => waitingEntries.value.filter((e) => e.status === 'duplicate' || e.status === 'invalid').length)
const processableCount = computed(() => pendingPaired.value + pendingHtmlOnly.value)
const hasProcessing = computed(() => processingCount.value > 0)
const downloading = ref(false)
async function downloadStandard() {
  if (!inv.value) return
  downloading.value = true
  try {
    await downloadFile(api.standardExportUrl({ dataset_id: inv.value.dataset_id }), `提取结果_标准样例表_${inv.value.dataset_id}.xlsx`)
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    downloading.value = false
  }
}

function stopInventoryPolling() {
  if (inventoryPoll !== undefined) {
    window.clearInterval(inventoryPoll)
    inventoryPoll = undefined
  }
}
function startInventoryPolling() {
  stopInventoryPolling()
  if (!pageActive || !hasProcessing.value) return
  inventoryPoll = window.setInterval(async () => {
    await refreshInventory()
    if (!hasProcessing.value) stopInventoryPolling()
  }, 1500)
}

onMounted(async () => {
  try {
    const d = await api.localDefaults()
    if (!isSingle.value) {
      htmlPath.value ||= d.html_path ?? ''
      attPath.value ||= d.attachment_path ?? ''
    }
    modelReady.value = (await api.readyz()).provider_configured
    await refreshInventory()
    // 恢复已有数据集时，旧的选择记录只代表上一次导入，不再占用上方选择区。
    if (inv.value) resetSelection()
    startInventoryPolling()
  } catch { /* 后端不可用时由各操作报错 */ }
})
onActivated(async () => {
  pageActive = true
  await refreshInventory()
  startInventoryPolling()
})
onDeactivated(() => { pageActive = false; stopInventoryPolling() })
function onVisible() {
  if (pageActive && document.visibilityState === 'visible') refreshInventory().then(startInventoryPolling)
}
onMounted(() => document.addEventListener('visibilitychange', onVisible))
onBeforeUnmount(() => { stopInventoryPolling(); document.removeEventListener('visibilitychange', onVisible) })
watch([htmlPath, attPath, inputKind, source, limit], () => {
  try {
    localStorage.setItem('ict.uploadHtmlPath', htmlPath.value)
    localStorage.setItem('ict.uploadAttachmentPath', attPath.value)
    localStorage.setItem('ict.uploadKind', inputKind.value)
    localStorage.setItem('ict.uploadSource', source.value)
    if (limit.value) localStorage.setItem('ict.uploadLimit', String(limit.value))
    else localStorage.removeItem('ict.uploadLimit')
  } catch { /* ignore */ }
})
async function useLocal() {
  if (!htmlPath.value.trim()) return
  uploading.value = true
  error.value = ''
  try {
    inv.value = await api.localDataset(htmlPath.value.trim(), attPath.value.trim() || null, inputKind.value)
    setDatasetId(inv.value.dataset_id)
    resetSelection()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    uploading.value = false
  }
}
const htmlZip = ref<File | null>(null)
const attZip = ref<File | null>(null)
// 导入成功后重置原生 file input，避免上方一直显示上一批已导入文件。
const uploadInputKey = ref(0)
const uploading = ref(false)
const starting = ref(false)
const error = ref('')
const filter = ref<'all' | 'processing' | InventoryEntry['status']>('all')

function resetSelection() {
  htmlPath.value = ''
  attPath.value = ''
  htmlZip.value = null
  attZip.value = null
  uploadInputKey.value += 1
}

watch(inputKind, (kind) => {
  // 切换输入类型后，避免把另一种模式里已选择的文件误提交。
  htmlZip.value = null
  attZip.value = null
  error.value = ''
  if (kind === 'single' && /\.zip$/i.test(htmlPath.value)) htmlPath.value = ''
  if (kind === 'batch' && /\.html?$/i.test(htmlPath.value)) htmlPath.value = ''
})

const MAX_BYTES = 8 * 1024 ** 3
function pick(e: Event, which: 'html' | 'att') {
  const f = (e.target as HTMLInputElement).files?.[0] ?? null
  const valid = which === 'html' && isSingle.value ? /\.(html?|HTML?)$/ : /\.zip$/i
  if (f && !valid.test(f.name)) {
    ElMessage.error(which === 'html' && isSingle.value ? '请选择 .html 或 .htm 文件' : '请选择 .zip 文件')
    return
  }
  if (f && f.size > MAX_BYTES) { ElMessage.error('文件超过 8 GB 上限'); return }
  if (which === 'html') htmlZip.value = f
  else attZip.value = f
}
const size = (f: File | null) => (f ? (f.size / 1024 ** 2).toFixed(1) + ' MB' : '')

async function upload() {
  if (!htmlZip.value) return
  uploading.value = true
  error.value = ''
  try {
    inv.value = await api.uploadDataset(htmlZip.value, attZip.value, inputKind.value)
    setDatasetId(inv.value.dataset_id)
    resetSelection()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    uploading.value = false
  }
}

async function start() {
  if (!inv.value) return
  starting.value = true
  try {
    const { job_id } = await api.startJob(inv.value.dataset_id, limit.value || undefined)
    await refreshInventory()
    startInventoryPolling()
    router.push(`/jobs/${job_id}`)
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    starting.value = false
  }
}

const STATUS: Record<InventoryEntry['status'], { t: string; c: string }> = {
  paired: { t: '已配对', c: 'win' }, html_only: { t: '仅公告', c: 'ind' }, attachment_only: { t: '仅附件', c: 'lose' },
  duplicate: { t: '重复ID冲突', c: 'lose' }, invalid: { t: '无效', c: 'lose' },
}
function entryStatus(entry: InventoryEntry) {
  if (entry.processing_status === 'queued' || entry.processing_status === 'running') return { t: '处理中', c: 'ind' }
  if (entry.processing_status === 'failed') return { t: '处理失败', c: 'lose' }
  if (entry.processing_status === 'succeeded') return { t: '已完成', c: 'win' }
  return STATUS[entry.status]
}
const rows = computed(() => remainingEntries.value.filter((e) => {
  if (filter.value === 'all') return true
  if (filter.value === 'processing') return isProcessing(e)
  if (isProcessing(e)) return false
  if (filter.value === 'duplicate') return e.status === 'duplicate' || e.status === 'invalid'
  return e.status === filter.value
}))

watch(() => [route.path, route.query.dataset], async () => {
  if (route.path !== '/upload' || typeof route.query.dataset !== 'string') return
  setDatasetId(route.query.dataset)
  await refreshInventory()
  startInventoryPolling()
}, { immediate: true })
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1><span class="no">壹</span>数据集导入</h1>
        <div class="sub">选择公告 HTML 包与附件包，按公告 ID 配对并生成校验清单</div>
      </div>
    </div>

    <div class="grid">
      <section class="sheet">
        <div v-if="modelReady === false" class="banner err" style="margin-bottom: 12px">
          <div>尚未配置模型 API，处理前请先到 <RouterLink to="/model">模型配置</RouterLink> 页填写 API Key。</div>
        </div>
        <div class="row" style="margin-bottom: 12px">
          <h2 class="sheet-title" style="margin: 0">选择数据</h2>
          <span class="grow" />
          <el-radio-group v-model="source" size="small">
            <el-radio-button value="upload">从浏览器上传</el-radio-button>
          </el-radio-group>
        </div>
        <div class="row" style="margin-bottom: 12px">
          <span class="hint">输入类型</span>
          <el-radio-group v-model="inputKind" size="small">
            <el-radio-button value="batch">批量 ZIP</el-radio-button>
            <el-radio-button value="single">单篇 HTML + 附件</el-radio-button>
          </el-radio-group>
        </div>
        <template v-if="source === 'local'">
          <el-form label-position="top" class="path-form" @submit.prevent>
            <el-form-item :label="isSingle ? '公告 HTML 文件' : '公告 HTML 包'"><el-input v-model="htmlPath" :placeholder="isSingle ? '例如 C:\\Users\\…\\t20260202_26139731.html' : '例如 C:\\Users\\…\\赛题五.基准测试数据_html.zip'" /></el-form-item>
            <el-form-item :label="isSingle ? '对应附件压缩包（可选）' : '附件包（可选）'"><el-input v-model="attPath" :placeholder="isSingle ? '例如 C:\\Users\\…\\t20260202_26139731.zip' : '例如 C:\\Users\\…\\赛题五.基准测试数据_file.zip'" /></el-form-item>
          </el-form>
          <div class="row" style="margin-top: 4px">
            <el-button type="primary" :disabled="!htmlPath.trim()" :loading="uploading" @click="useLocal">读取并生成校验清单</el-button>
            <span class="hint">{{ isSingle ? 'HTML 和附件压缩包按公告 ID 配对；附件包可直接是该公告的内部 ZIP。' : '直接读取本机 ZIP，数 GB 的附件包也能即时导入。' }}</span>
          </div>
          <div v-if="error" class="banner err" style="margin-top: 12px">{{ error }}</div>
        </template>
        <template v-else>
        <label class="drop" :class="{ has: htmlZip }">
          <input :key="`html-${uploadInputKey}`" type="file" :accept="isSingle ? '.html,.htm' : '.zip'" @change="pick($event, 'html')" />
          <span class="drop-k">{{ isSingle ? '公告 HTML 文件' : '公告 HTML 包' }}<em>必填</em></span>
          <span v-if="htmlZip" class="drop-v">{{ htmlZip.name }} · {{ size(htmlZip) }}</span>
          <span v-else class="drop-v muted">点击选择，例如 {{ isSingle ? 't20260202_26139731.html' : '赛题五.基准测试数据_html.zip' }}</span>
        </label>
        <label class="drop" :class="{ has: attZip }">
          <input :key="`att-${uploadInputKey}`" type="file" accept=".zip" @change="pick($event, 'att')" />
          <span class="drop-k">{{ isSingle ? '对应附件压缩包' : '附件包' }}<em class="opt">可选</em></span>
          <span v-if="attZip" class="drop-v">{{ attZip.name }} · {{ size(attZip) }}</span>
          <span v-else class="drop-v muted">{{ isSingle ? '例如 t20260202_26139731.zip' : '每篇公告对应一个 &lt;公告ID&gt;.zip' }}</span>
        </label>
        <div class="row" style="margin-top: 14px">
          <el-button type="primary" :disabled="!htmlZip" :loading="uploading" @click="upload">上传并生成校验清单</el-button>
          <span class="hint">{{ isSingle ? '附件 ZIP 会按 HTML 文件名中的公告 ID 配对。' : '支持带目录前缀的文件名；重复 ID 会标为冲突。' }}</span>
        </div>
        <div v-if="error" class="banner err" style="margin-top: 12px">{{ error }}</div>
        </template>
      </section>

      <section class="sheet">
        <h2 class="sheet-title">导入规则</h2>
        <ol class="rules">
          <li>公告 ID 取自文件名（如 t20260202_26139731），附件包内同名 zip 视为该公告附件。</li>
          <li>单篇模式可直接选择一个 HTML 和对应附件 ZIP；平台会自动整理成同样的配对清单。</li>
          <li>缺附件：公告照常处理，附件状态标记为“未提供”。</li>
          <li>仅有附件而无公告：列入清单，不参与抽取。</li>
          <li>同一 ID 出现多次：标为冲突并跳过。</li>
        </ol>
      </section>
    </div>

    <section v-if="inv" class="sheet">
      <div class="row" style="margin-bottom: 14px">
        <h2 class="sheet-title" style="margin: 0">校验清单 <span class="mono muted">{{ inv.dataset_id }}</span></h2>
        <span class="grow" /><el-button size="small" @click="refreshInventory().then(startInventoryPolling)">刷新清单</el-button>
      </div>
      <div v-if="processableCount" class="banner info" style="margin-bottom: 10px">
        数据已导入，下面是本数据集的待处理清单；上方选择框已清空，可以继续导入其他数据。
      </div>
      <div class="stats" style="margin-bottom: 14px">
        <div class="stat"><div class="k">待处理 HTML</div><div class="v">{{ pendingHtmlCount }}</div></div>
        <div class="stat"><div class="k">待处理附件包</div><div class="v">{{ pendingAttachmentCount }}</div></div>
        <div class="stat"><div class="k">待处理配对</div><div class="v">{{ pendingPaired }}</div></div>
        <div class="stat"><div class="k">处理中</div><div class="v">{{ processingCount }}</div></div>
        <div class="stat"><div class="k">已完成</div><div class="v">{{ processedCount }}</div></div>
        <div class="stat"><div class="k">冲突</div><div class="v">{{ pendingConflicts }}</div></div>
      </div>
      <div class="row" style="margin-bottom: 10px">
        <el-radio-group v-model="filter" size="small">
          <el-radio-button value="all">全部（{{ remainingEntries.length }}）</el-radio-button>
          <el-radio-button value="paired">待处理配对（{{ pendingPaired }}）</el-radio-button>
          <el-radio-button value="processing">处理中（{{ processingCount }}）</el-radio-button>
          <el-radio-button value="html_only">仅公告（{{ pendingHtmlOnly }}）</el-radio-button>
          <el-radio-button value="attachment_only">仅附件（{{ pendingAttachmentOnly }}）</el-radio-button>
          <el-radio-button value="duplicate">冲突（{{ pendingConflicts }}）</el-radio-button>
        </el-radio-group>
        <span class="grow" />
        <span class="hint">本次处理</span>
        <el-select v-model="limit" size="default" style="width: 150px">
          <el-option :value="5" label="前 5 篇（试跑）" />
          <el-option :value="20" label="前 20 篇" />
          <el-option :value="100" label="前 100 篇" />
          <el-option :value="undefined" :label="`全部 ${processableCount} 篇`" />
        </el-select>
        <el-button type="primary" :loading="starting" :disabled="processableCount === 0" @click="start">开始处理</el-button>
        <el-button :loading="downloading" :disabled="processedCount === 0" @click="downloadStandard">下载提取结果（标准样例表）</el-button>
      </div>
      <div v-if="processingCount" class="banner info" style="margin-bottom: 10px">
        处理中 {{ processingCount }} 篇，前往 <RouterLink to="/jobs">任务监控</RouterLink> 查看进度。
      </div>
      <div v-else-if="processedCount && processableCount === 0" class="banner info" style="margin-bottom: 10px">
        本数据集处理完成，可到 <RouterLink to="/items">标的物检索</RouterLink> 查看任务一七字段结果。
      </div>
      <el-table :data="rows" height="420" border>
        <el-table-column prop="announcement_id" label="公告 ID" width="220" />
        <el-table-column label="公告" width="80"><template #default="{ row }">{{ row.html_present ? '有' : '无' }}</template></el-table-column>
        <el-table-column label="附件" width="80"><template #default="{ row }">{{ row.attachment_present ? '有' : '无' }}</template></el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }"><span class="tag" :class="entryStatus(row).c">{{ entryStatus(row).t }}</span></template>
        </el-table-column>
        <el-table-column label="说明">
          <template #default="{ row }">
            <span v-if="row.processing_status === 'queued' || row.processing_status === 'running'">
              任务处理中<span v-if="row.processing_job_id" class="mono muted"> · {{ row.processing_job_id }}</span>
            </span>
            <span v-else>{{ row.processing_message || row.message || '—' }}</span>
          </template>
        </el-table-column>
      </el-table>
    </section>
  </div>
</template>

<style scoped>
.grid { display: grid; grid-template-columns: 3fr 2fr; gap: 16px; margin-bottom: 16px; }
.grid .sheet + .sheet { margin-top: 0; }
.drop { display: flex; flex-direction: column; gap: 4px; border: 1px dashed var(--rule); padding: 16px; cursor: pointer; background: var(--paper); }
.drop + .drop { margin-top: 10px; }
.drop:hover, .drop:focus-within { border-color: var(--indigo); }
.drop.has { border-style: solid; border-color: var(--indigo); background: var(--indigo-soft); }
.drop input { position: absolute; opacity: 0; width: 1px; height: 1px; }
.drop-k { font-family: var(--serif); font-weight: 600; }
.drop-k em { font-style: normal; font-size: 12px; color: var(--cinnabar); margin-left: 8px; font-family: var(--sans); font-weight: 400; }
.drop-k em.opt { color: var(--ink-3); }
.drop-v { font-size: 13px; }
.path-form :deep(.el-form-item) { margin-bottom: 12px; }
.path-form :deep(.el-form-item__label) { margin-bottom: 4px; line-height: 20px; }
.rules { margin: 0; padding-left: 20px; line-height: 2; color: var(--ink-2); font-size: 13px; }
@media (max-width: 1000px) { .grid { grid-template-columns: 1fr; } }
</style>
