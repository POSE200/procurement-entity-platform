<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { downloadFile } from '@/utils/download'
import { api } from '@/api/client'
import type { Job, JobStage } from '@/types/api'
import { dateTime, ms } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const jobId = ref(String(route.params.id ?? ''))
const input = ref(jobId.value)
const job = ref<Job | null>(null)
const error = ref('')
const selected = ref<string[]>([])
const retrying = ref(false)
const clearingJob = ref('')
let timer: number | undefined

const STAGE: Record<JobStage['name'], string> = { parse: '解析', extract: '模型抽取', review: '原文核验·定点重抽', validate: '校验', store: '入库', graph: '图谱投影' }
const STATUS: Record<string, string> = { queued: '排队中', running: '运行中', paused: '已暂停', succeeded: '已完成', partial: '部分完成', failed: '失败', pending: '待开始', skipped: '已跳过' }
const done = computed(() => !!job.value && ['succeeded', 'partial', 'failed'].includes(job.value.status))

const recent = ref<Awaited<ReturnType<typeof api.jobs>>>([])

async function poll() {
  if (!jobId.value) {
    try { recent.value = await api.jobs(); error.value = '' } catch (e) { error.value = (e as Error).message }
    return
  }
  try {
    job.value = await api.job(jobId.value)
    error.value = ''
  } catch (e) {
    error.value = (e as Error).message
  }
  clearTimeout(timer)
  // 首版使用轮询（规划 8.2）；任务结束后停止
  if (!done.value && !error.value) timer = window.setTimeout(poll, 2000)
  else if (done.value && job.value?.succeeded) ElMessage.success({ message: '处理完成，可到“标的物检索”“关系分析”查看结果', grouping: true })
}

function open() {
  const id = input.value.trim()
  if (!id) return
  router.replace(`/jobs/${encodeURIComponent(id)}`)
}

async function retry() {
  if (!job.value || !selected.value.length) return
  retrying.value = true
  try {
    job.value = await api.retryJob(job.value.job_id, selected.value)
    selected.value = []
    ElMessage.success('已提交重试')
    poll()
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    retrying.value = false
  }
}

const downloading = ref(false)
async function downloadStandard() {
  if (!job.value) return
  await downloadForJob(job.value.job_id)
}
async function downloadForJob(id: string) {
  downloading.value = true
  try {
    await downloadFile(api.standardExportUrl({ job_id: id }), `提取结果_${id}.xlsx`)
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    downloading.value = false
  }
}

async function clearJob(id: string, name: string) {
  try {
    await ElMessageBox.confirm(
      `清除任务“${name}”？\n任务 ID：${id}。\n仅清除此任务的监控记录和评测记录。其他任务、已入库结果、模型缓存和原始文件均保留。`,
      '清除此任务',
      { type: 'warning', confirmButtonText: '清除此任务', cancelButtonText: '取消', closeOnClickModal: false },
    )
  } catch { return }
  clearingJob.value = id
  try {
    await api.clearJob(id)
    try {
      if (localStorage.getItem('ict.selectedResultJob') === id) localStorage.removeItem('ict.selectedResultJob')
    } catch { /* storage unavailable */ }
    recent.value = await api.jobs()
    ElMessage.success('已清除此任务，已入库结果和模型缓存保留')
    if (jobId.value === id) await router.replace('/jobs')
  } catch (e) { ElMessage.error((e as Error).message) }
  finally { clearingJob.value = '' }
}

watch(() => route.params.id, (v) => { jobId.value = String(v ?? ''); input.value = jobId.value; job.value = null; poll() }, { immediate: true })
onBeforeUnmount(() => clearTimeout(timer))
const dur = (s: number) => (s >= 60 ? `${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒` : `${Math.round(s)} 秒`)
const stagesShown = computed(() => job.value?.stages.filter((s) => s.name !== 'graph') ?? [])
const pct = (s: JobStage) => (s.total ? Math.round(((s.done + s.failed + (s.skipped || 0)) / s.total) * 100) : 0)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1><span class="no">贰</span>任务监控</h1>
        <div class="sub">查看各阶段进度和失败明细，失败的公告可单独重试</div>
      </div>
      <div class="row">
        <el-input v-model="input" placeholder="输入任务 ID" style="width: 260px" @keyup.enter="open" />
        <el-button @click="open">查看</el-button>
      </div>
    </div>

    <div v-if="error" class="banner err">{{ error }}</div>
    <section v-else-if="!jobId" class="sheet">
      <div class="row" style="margin-bottom: 10px">
        <h2 class="sheet-title" style="margin: 0">最近任务</h2>
        <span class="grow" />
        <span class="hint">每个任务可单独查看、下载或清除记录。</span>
      </div>
      <el-table :data="recent" border empty-text="还没有任务，请先到“数据集导入”页开始处理"
        @row-click="(r: { job_id: string }) => router.push(`/jobs/${r.job_id}`)">
        <el-table-column prop="display_name" label="任务名称" min-width="290" />
        <el-table-column label="公告 / 入库" width="135"><template #default="{ row }">{{ row.total }} / {{ row.stored }}</template></el-table-column>
        <el-table-column prop="job_id" label="任务 ID" width="240" />
        <el-table-column label="操作" width="330" fixed="right"><template #default="{ row }">
          <el-button text type="primary" @click.stop="router.push({ path: '/items', query: { job: row.job_id } })">查看结果</el-button>
          <el-button text type="primary" :disabled="!row.stored" @click.stop="downloadForJob(row.job_id)">下载 XLSX</el-button>
          <el-button text type="danger" :disabled="!!row.active || !!clearingJob" :loading="clearingJob === row.job_id" @click.stop="clearJob(row.job_id, row.display_name)">清除此任务</el-button>
        </template></el-table-column>
        <el-table-column label="模型"><template #default="{ row }">{{ row.provider }} / {{ row.model }}</template></el-table-column>
        <el-table-column label="创建时间" width="180"><template #default="{ row }">{{ dateTime(row.created_at) }}</template></el-table-column>
      </el-table>
    </section>
    <template v-else-if="job">
      <div class="row">
        <h2 class="sheet-title">{{ job.display_name }}</h2><span class="grow" />
        <el-button type="danger" plain :disabled="!done" :loading="clearingJob === job.job_id" @click="clearJob(job.job_id, job.display_name)">清除此任务</el-button>
      </div>
      <div class="stats">
        <div class="stat"><div class="k">状态</div><div class="v">{{ STATUS[job.status] }}</div></div>
        <div class="stat"><div class="k">公告总数</div><div class="v">{{ job.total }}</div></div>
        <div class="stat"><div class="k">已完成</div><div class="v">{{ job.succeeded }}</div></div>
        <div v-if="job.skipped" class="stat"><div class="k">占位（0分）</div><div class="v">{{ job.skipped }}</div></div>
        <div v-if="job.skipped" class="stat"><div class="k">已入库</div><div class="v">{{ job.stored }}</div></div>
        <div class="stat"><div class="k">失败</div><div class="v">{{ job.failed }}</div></div>
        <div class="stat"><div class="k">待复核</div><div class="v">{{ job.needs_review }}</div></div>
        <div class="stat"><div class="k">模型</div><div class="v" style="font-size: 15px">{{ job.provider }} / {{ job.model || '未配置' }}</div></div>
      </div>
      <p v-if="job.placeholders?.length" class="banner">
        未解析公告（空值占位，0分）：
        <template v-for="(item, index) in job.placeholders" :key="item.announcement_id">
          <span v-if="index">、</span><RouterLink :to="`/announcements/${item.announcement_id}`">{{ item.announcement_id }}</RouterLink>
        </template>
      </p>
      <p class="hint">任务 <span class="mono">{{ job.job_id }}</span> · 数据集 <span class="mono">{{ job.dataset_id }}</span> · 创建 {{ dateTime(job.created_at) }}
        <template v-if="job.wall_seconds != null"> · 已用时 {{ dur(job.wall_seconds) }}</template>
        <template v-if="job.per_minute"> · {{ job.per_minute }} 篇/分钟</template>
        <template v-if="job.eta_seconds"> · 预计还需 {{ dur(job.eta_seconds) }}</template>
        <template v-if="done && job.succeeded"> · <RouterLink :to="{ path: '/task1', query: { job: job.job_id } }">查看任务一指标</RouterLink> · <RouterLink :to="`/items?job=${encodeURIComponent(job.job_id)}`">查看任务一七字段</RouterLink></template></p>
      <div v-if="job.succeeded" class="row" style="margin: 4px 0 12px">
        <el-button type="primary" :loading="downloading" @click="downloadStandard">下载提取结果（标准样例表 xlsx）</el-button>
        <span class="hint">总表 / 标的表 / 投标人表</span>
      </div>

      <section class="sheet">
        <h2 class="sheet-title">阶段进度</h2>
        <div class="stages">
          <div v-for="s in stagesShown" :key="s.name" class="stage" :class="s.status">
            <div class="row"><b class="serif">{{ STAGE[s.name] }}</b><span class="grow" /><span class="tag">{{ STATUS[s.status] }}</span></div>
            <el-progress :percentage="pct(s)" :stroke-width="6" :show-text="false" :status="s.failed ? 'exception' : s.status === 'succeeded' ? 'success' : undefined" />
            <div class="hint">完成 {{ s.done }} · 失败 {{ s.failed }}<template v-if="s.skipped"> · 跳过 {{ s.skipped }}</template> / {{ s.total }} · 累计 {{ ms(s.elapsed_ms) }}</div>
          </div>
        </div>
      </section>

      <section class="sheet">
        <div class="row" style="margin-bottom: 10px">
          <h2 class="sheet-title" style="margin: 0">失败明细（{{ job.failures.length }}）</h2>
          <span class="grow" />
          <el-button type="primary" :disabled="!selected.length" :loading="retrying" @click="retry">重试所选（{{ selected.length }}）</el-button>
        </div>
        <el-table :data="job.failures" height="360" border empty-text="暂无失败"
          @selection-change="(rows: Job['failures']) => (selected = rows.map((r) => r.announcement_id))">
          <el-table-column type="selection" width="44" :selectable="(r: Job['failures'][number]) => r.retryable" />
          <el-table-column label="公告 ID" width="220">
            <template #default="{ row }"><RouterLink :to="`/announcements/${row.announcement_id}`">{{ row.announcement_id }}</RouterLink></template>
          </el-table-column>
          <el-table-column label="阶段" width="100"><template #default="{ row }">{{ STAGE[row.stage as JobStage['name']] }}</template></el-table-column>
          <el-table-column prop="error_code" label="错误码" width="180" />
          <el-table-column prop="message" label="说明" />
          <el-table-column label="可重试" width="80"><template #default="{ row }">{{ row.retryable ? '是' : '否' }}</template></el-table-column>
        </el-table>
      </section>
    </template>
    <div v-else class="hint">加载中…</div>
  </div>
</template>

<style scoped>
.stages { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; }
.stage { border: 1px solid var(--rule-2); padding: 12px; display: flex; flex-direction: column; gap: 8px; background: var(--paper); }
.stage.running { border-color: var(--indigo); }
.stage.failed { border-color: #d7a39c; }
@media (max-width: 1100px) { .stages { grid-template-columns: repeat(2, 1fr); } }
</style>
