<script setup lang="ts">
defineOptions({ name: 'Task1View' })
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import type { EvalDoc, Task1Overview } from '@/types/api'
import { ms, num } from '@/utils/format'
import { downloadFile } from '@/utils/download'
import { ElMessage } from 'element-plus'
import { ALL_RESULTS, rememberResultScope, resolveResultScope, resultScopeName } from '@/utils/resultScope'

const data = ref<Task1Overview | null>(null)
const route = useRoute()
const router = useRouter()
const jobs = ref<Awaited<ReturnType<typeof api.jobs>>>([])
const jobId = ref(String(route.query.job ?? ''))
const loading = ref(false)
const error = ref('')
const ALL = ALL_RESULTS
const downloading = ref(false)
const scopeName = computed(() => resultScopeName(jobId.value, jobs.value))
const selectedJob = computed(() => jobs.value.find(j => j.job_id === jobId.value))
let requestVersion = 0

async function load(refresh = false) {
  const version = ++requestVersion
  loading.value = true
  error.value = ''
  try {
    if (!jobs.value.length) jobs.value = await api.jobs()
    const result = await api.task1Overview(refresh, jobId.value || undefined)
    if (version === requestVersion) data.value = result
  } catch (e) {
    if (version !== requestVersion) return
    data.value = null
    error.value = (e as Error).message
  } finally {
    if (version === requestVersion) loading.value = false
  }
}

function pickJob(value: string) {
  rememberResultScope(value)
  router.replace({ path: '/task1', query: { job: value } })
}

// 旧记录自动整理期间每 5 秒刷新一次，整理完自动停止
let timer: ReturnType<typeof setTimeout> | undefined
watch(data, (v) => {
  clearTimeout(timer)
  if (v?.merge_migration?.running) timer = setTimeout(() => load(), 5000)
})
onBeforeUnmount(() => clearTimeout(timer))

watch(() => route.fullPath, async () => {
  if (route.path !== '/task1') return
  try {
    jobs.value = await api.jobs()
    const choice = resolveResultScope(route.query.job, jobs.value)
    if (!route.query.job) { await router.replace({ path: '/task1', query: { job: choice } }); return }
    jobId.value = choice === ALL ? '' : choice
    data.value = null
    await load()
  } catch (e) { error.value = (e as Error).message }
}, { immediate: true })

async function downloadStandard() {
  downloading.value = true
  try {
    await downloadFile(api.standardExportUrl(jobId.value ? { job_id: jobId.value } : {}), `${scopeName.value}.xlsx`)
  } catch (e) { ElMessage.error((e as Error).message) }
  finally { downloading.value = false }
}

const pct = (v: number | null | undefined, d = 1) => (v == null ? '—' : `${(v * 100).toFixed(d)}%`)
// 得分 / 满分 → 百分比，保留两位小数
const ratio = (score: number | null | undefined, max: number | null | undefined) => (score == null || !max ? '—' : pct(score / max, 2))
const sec = (v: number | null | undefined) => (v == null ? '—' : `${v.toFixed(2)} 秒`)
const KIND: Record<string, string> = { pdf: 'PDF', docx: 'Word', xlsx: 'Excel', image: '图片（OCR/视觉）', other: '其他' }
const FIELD: Record<string, string> = {
  item_name: '标的名称', category: '品目', brand: '品牌', model: '规格型号',
  unit_price: '单价', quantity: '数量', total_price: '总价',
}
const OFFICIAL_POINTS: Record<string, number> = { '7': 10, '5_6': 5, '3_4': 3, '0_2': 0 }
const show = (value: unknown) => value == null || value === '' ? '（空）' : String(value)
const diffs = (row: EvalDoc) => row.diffs ?? []
const sourceRows = computed(() => {
  const c = data.value?.coverage
  if (!c) return []
  return [
      { key: 'html', label: '公告原文', covered: c.html_covered, rows: c.items_from_html,
      announcements: c.announcements_items_from_html, denominator: c.announcements,
      rate: c.html_announcement_rate },
    { key: 'attachment', label: '相关附件', covered: c.attachment_covered, rows: c.items_from_attachment,
      announcements: c.announcements_items_from_attachment, denominator: c.with_attachment,
      rate: c.attachment_announcement_rate },
  ]
})
const fieldMax = computed(() => Math.max(1, ...(data.value?.extraction.fields ?? []).map((f) => f.rate ?? 0)))
const kindList = computed(() => Object.entries(data.value?.coverage.attachment_kinds ?? {})
  .map(([key, count]) => ({ key, label: KIND[key] ?? key, count })))
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1><span class="no">一</span>任务一指标</h1>
        <div class="sub">按赛题要求展示覆盖率、提取率、提取准确性和平均处理速度。</div>
      </div>
      <div class="row">
        <el-select :model-value="jobId || ALL" style="width: 420px" aria-label="选择结果任务" @update:model-value="pickJob">
          <el-option :value="ALL" label="全部已入库数据（跨任务合并去重）" />
          <el-option v-for="job in jobs" :key="job.job_id" :value="job.job_id" :label="job.display_name" />
        </el-select>
        <el-button :loading="loading" @click="load(true)">重新计算</el-button>
        <el-button type="primary" :loading="downloading" :disabled="loading || !data?.coverage.announcements" @click="downloadStandard">下载所选范围 XLSX</el-button>
      </div>
    </div>

    <div v-if="error" class="banner err">{{ error }}</div>
    <div v-else-if="loading && !data" class="banner info">正在计算任务一指标……</div>

    <template v-if="data">
      <div v-if="data.merge_migration?.running" class="banner info">
        正在把旧记录按当前合并规则整理：{{ data.merge_migration.done }} / {{ data.merge_migration.total }} 篇，指标会随之自动更新。
      </div>
      <p class="hint source-note">{{ scopeName }} · {{ data.coverage.announcements }} 篇已入库公告 · 模型 {{ data.model ?? '—' }} · 计算耗时 {{ ms(data.query_ms) }}</p>

      <section class="sheet">
        <div class="row"><b>{{ scopeName }}</b><span class="grow" />
          <RouterLink :to="{ path: '/items', query: { job: jobId || ALL } }">查看该范围提取结果</RouterLink>
          <RouterLink v-if="jobId" :to="`/jobs/${jobId}`">查看该任务进度</RouterLink>
        </div>
        <p class="hint">任务 ID：{{ jobId || '全部任务' }} · 已入库 {{ data.coverage.announcements }} 篇<template v-if="selectedJob"> / 任务共 {{ selectedJob.total }} 篇 · 空值占位 {{ selectedJob.skipped }} 篇</template></p>
      </section>
      <div class="metric-grid">
        <section class="metric-card">
          <div class="metric-k">覆盖率</div>
          <div class="metric-v">{{ pct(data.coverage.covered_categories / 2) }}<small> · {{ data.coverage.covered_categories }}/2 类</small></div>
          <div class="metric-sub">
            <span :class="data.coverage.html_covered ? 'covered' : 'uncovered'">原文 {{ data.coverage.html_covered ? '已覆盖' : '未覆盖' }}</span>
            <span :class="data.coverage.attachment_covered ? 'covered' : 'uncovered'">附件 {{ data.coverage.attachment_covered ? '已覆盖' : '未覆盖' }}</span>
          </div>
          <div class="metric-foot">赛题按公告原文、相关附件两类评分，各 5 分</div>
        </section>

        <section class="metric-card">
          <div class="metric-k">提取率 · 七字段分档</div>
          <!-- 主数字：对照答案口径（原文本身没有的字段如实留空也算提取到）；无答案时回落到只数有值字段 -->
          <template v-if="data.extraction.gold_aware?.available">
            <div class="metric-v">{{ ratio(data.extraction.gold_aware.score_points, data.extraction.gold_aware.max_points) }}</div>
            <div class="metric-sub">
              得分 {{ num(data.extraction.gold_aware.score_points) }} / {{ num(data.extraction.gold_aware.max_points) }} 分 · {{ num(data.extraction.gold_aware.items) }} 条明细
            </div>
            <div class="metric-sub muted">非空字段：{{ ratio(data.extraction.score_points, data.extraction.max_points) }}</div>
          </template>
          <template v-else>
            <div class="metric-v">{{ data.extraction.available ? ratio(data.extraction.score_points, data.extraction.max_points) : '—' }}</div>
            <div v-if="data.extraction.available" class="metric-sub">
              得分 {{ num(data.extraction.score_points) }} / {{ num(data.extraction.max_points) }} 分 · {{ num(data.extraction.predicted_items) }} 条明细
            </div>
            <div v-else class="metric-sub">暂无提取明细</div>
          </template>
          <div class="metric-foot">七字段完整度评分</div>
        </section>

        <section class="metric-card">
          <div class="metric-k">提取准确性 · 加权结果</div>
          <div class="metric-v">{{ pct(data.accuracy?.score) }}</div>
          <div v-if="data.accuracy" class="metric-sub">A {{ pct(data.accuracy.accuracy) }} · P {{ pct(data.accuracy.precision) }} · R {{ pct(data.accuracy.recall) }}</div>
          <div v-else class="metric-sub">请导入复核答案</div>
          <div class="metric-foot">A × 0.4 + P × 0.3 + R × 0.3</div>
        </section>

        <section class="metric-card">
          <div class="metric-k">平均处理速度</div>
          <div class="metric-v">{{ sec(data.speed.per_announcement_s) }}<small> / 篇</small></div>
          <div class="metric-sub">累计 {{ num(data.speed.total_announcements) }} 篇 · {{ Math.round(data.speed.total_seconds / 60) }} 分钟</div>
          <div class="metric-foot">包含附件解析和模型抽取</div>
        </section>
      </div>

      <section class="sheet">
        <h2 class="sheet-title">一、覆盖率 · 公告原文与相关附件</h2>
        <div class="source-grid">
          <article v-for="source in sourceRows" :key="source.key" class="source-card">
            <div class="row">
              <b>{{ source.label }}</b>
              <span class="grow" />
              <span class="tag" :class="source.covered ? 'win' : 'lose'">{{ source.covered ? '已覆盖 · 5 分项' : '尚未验证覆盖' }}</span>
            </div>
            <div class="source-rate">{{ pct(source.rate) }}<small> 公告覆盖</small></div>
            <div class="source-count">{{ num(source.announcements) }}<small> 篇公告</small><span>·</span>{{ num(source.rows) }}<small> 条关联明细</small></div>
            <div class="hint">{{ source.key === 'attachment' ? `含附件公告 ${num(source.denominator)} 篇` : `已处理公告 ${num(source.denominator)} 篇` }}</div>
          </article>
        </div>
        <p class="hint section-note">读取附件 {{ num(data.coverage.attachment_used) }} / {{ num(data.coverage.with_attachment) }} 篇含附件公告；其中含图片/OCR 的公告 {{ num(data.coverage.image_announcements) }} 篇。</p>
        <div class="kind-list">
          <span v-for="kind in kindList" :key="kind.key" class="tag ind">{{ kind.label }} · {{ num(kind.count) }} 个</span>
          <span v-if="!kindList.length" class="hint">尚无附件读取记录</span>
        </div>
      </section>

      <section class="sheet">
        <h2 class="sheet-title">二、提取率与官方七字段评分档位</h2>
        <el-table :data="data.extraction.bands" border>
          <el-table-column prop="label" label="每条明细提取字段数" min-width="220" />
          <el-table-column label="赛题分档" min-width="110"><template #default="{ row }">{{ row.points ?? OFFICIAL_POINTS[row.key] ?? '—' }} 分</template></el-table-column>
          <template v-if="data.extraction.gold_aware?.available">
            <el-table-column label="明细数" align="right" min-width="110">
              <template #default="{ $index }">{{ num(data.extraction.gold_aware!.bands[$index]?.items) }}</template>
            </el-table-column>
            <el-table-column label="占比" align="right" min-width="110">
              <template #default="{ $index }">{{ pct(data.extraction.gold_aware!.bands[$index]?.rate) }}</template>
            </el-table-column>
            <el-table-column label="非空字段" align="right" min-width="130"><template #default="{ row }">{{ num(row.items) }}</template></el-table-column>
          </template>
          <template v-else>
            <el-table-column label="明细数" align="right" min-width="110"><template #default="{ row }">{{ num(row.items) }}</template></el-table-column>
            <el-table-column label="占比" align="right" min-width="110"><template #default="{ row }">{{ pct(row.rate) }}</template></el-table-column>
          </template>
        </el-table>
        <p v-if="data.extraction.gold_aware?.available" class="hint section-note">
          提取率 {{ ratio(data.extraction.gold_aware.score_points, data.extraction.gold_aware.max_points) }} · 空值字段匹配 {{ num(data.extraction.gold_aware.null_ok_slots) }} 个
        </p>

        <template v-if="data.extraction.field_annotation_available">
          <h3 class="sub-title">七字段分别统计（分母为该字段有标注的条数）</h3>
          <div v-for="field in data.extraction.fields" :key="field.key" class="bar-row">
            <span class="bar-k">{{ field.label }}</span>
            <span class="bar"><i :style="{ width: `${((field.rate ?? 0) / fieldMax) * 100}%` }" /></span>
            <span class="bar-v">{{ num(field.extracted) }} / {{ num(field.annotated) }} · {{ pct(field.rate) }}</span>
          </div>
        </template>
        <p v-else class="hint section-note">暂无答案标注字段数据</p>
      </section>

      <section class="sheet">
        <div class="row">
          <h2 class="sheet-title">三、提取准确性 · 人工复核对比</h2>
        </div>
        <template v-if="data.accuracy">
          <div class="accuracy-grid">
            <div class="accuracy-stat"><span>准确率 A</span><b>{{ pct(data.accuracy.accuracy) }}</b></div>
            <div class="accuracy-stat"><span>精确率 P</span><b>{{ pct(data.accuracy.precision) }}</b></div>
            <div class="accuracy-stat"><span>召回率 R</span><b>{{ pct(data.accuracy.recall) }}</b></div>
            <div class="accuracy-stat emphasized"><span>提取准确性</span><b>{{ pct(data.accuracy.score) }}</b></div>
          </div>
          <p class="hint section-note">加权准确性 = 0.4 × 准确率 + 0.3 × 精确率 + 0.3 × 召回率</p>
          <el-table :data="data.accuracy.fields" border>
            <el-table-column prop="label" label="字段" min-width="120" />
            <el-table-column label="准确率 A" align="right" min-width="105"><template #default="{ row }">{{ pct(row.accuracy) }}</template></el-table-column>
            <el-table-column label="精确率 P" align="right" min-width="105"><template #default="{ row }">{{ pct(row.precision) }}</template></el-table-column>
            <el-table-column label="召回率 R" align="right" min-width="105"><template #default="{ row }">{{ pct(row.recall) }}</template></el-table-column>
            <el-table-column label="加权准确性" align="right" min-width="125"><template #default="{ row }">{{ pct(row.score) }}</template></el-table-column>
            <el-table-column prop="tp" label="正确 TP" align="right" min-width="90" />
            <el-table-column prop="fp" label="有字段错/漏 FP" align="right" min-width="90" />
            <el-table-column prop="fn" label="无字段误填 FN" align="right" min-width="90" />
            <el-table-column prop="tn" label="无字段未填 TN" align="right" min-width="110" />
          </el-table>

          <h3 class="sub-title">逐篇差异复核</h3>
          <el-table :data="data.accuracy.worst" border row-key="announcement_id" empty-text="暂无可展示的逐篇评测结果">
            <el-table-column type="expand">
              <template #default="{ row }">
                <div class="diffs">
                  <div v-for="(diff, index) in diffs(row)" :key="index">
                    {{ diff.item ?? '（未命名明细）' }} · {{ FIELD[diff.field] ?? diff.field }}：抽取 {{ show(diff.pred) }}，答案 {{ show(diff.gold) }}
                  </div>
                  <div v-if="(row.diff_count ?? diffs(row).length) > diffs(row).length" class="hint">
                    仅显示前 {{ diffs(row).length }} 条，共 {{ row.diff_count }} 条字段差异
                  </div>
                  <div v-if="!diffs(row).length" class="hint">无字段差异</div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="公告 ID" min-width="220">
              <template #default="{ row }"><RouterLink :to="`/announcements/${row.announcement_id}`">{{ row.announcement_id }}</RouterLink></template>
            </el-table-column>
            <el-table-column label="字段准确率 A" align="right" min-width="130"><template #default="{ row }">{{ pct(row.fields_accuracy) }}</template></el-table-column>
            <el-table-column label="明细行（抽取 / 答案）" align="right" min-width="170"><template #default="{ row }">{{ row.pred_items }} / {{ row.gold_items }}</template></el-table-column>
            <el-table-column label="字段差异" align="right" min-width="100"><template #default="{ row }">{{ row.diff_count ?? diffs(row).length }} 处</template></el-table-column>
          </el-table>
        </template>
        <div v-else class="hint">未找到人工复核答案，准确性指标暂不可计算。</div>
      </section>

      <section class="sheet">
        <h2 class="sheet-title">四、平均处理速度</h2>
        <div class="speed-summary">
          <b>{{ sec(data.speed.per_announcement_s) }} / 篇</b>
          <span>合计 {{ num(data.speed.total_announcements) }} 篇，墙钟耗时 {{ (data.speed.total_seconds / 60).toFixed(1) }} 分钟</span>
        </div>
        <el-table :data="data.speed.jobs" border empty-text="还没有已完成的任务">
          <el-table-column label="任务" min-width="175"><template #default="{ row }"><span class="mono">{{ row.job_id }}</span></template></el-table-column>
          <el-table-column prop="model" label="模型" min-width="120" />
          <el-table-column label="公告数" align="right" min-width="90"><template #default="{ row }">{{ num(row.announcements) }}</template></el-table-column>
          <el-table-column label="总耗时" align="right" min-width="110"><template #default="{ row }">{{ (row.wall_seconds / 60).toFixed(1) }} 分钟</template></el-table-column>
          <el-table-column label="平均处理速度" align="right" min-width="125"><template #default="{ row }">{{ sec(row.per_announcement_s) }} / 篇</template></el-table-column>
        </el-table>
        <p class="hint section-note">本地计时从任务创建到最后一篇结果写入数据库，包含附件解析和模型调用；赛事最终以统一平台计时为准。</p>
      </section>
    </template>
  </div>
</template>

<style scoped>
.source-note { margin: 4px 0 12px; }
.metric-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.metric-card { min-width: 0; padding: 16px; background: var(--card); border: 1px solid var(--rule-2); }
.metric-k { color: var(--ink-3); font-size: 13px; }
.metric-v { margin-top: 8px; color: var(--ink); font: 30px/1.2 var(--serif, Georgia, serif); font-variant-numeric: tabular-nums; }
.metric-v small { color: var(--ink-3); font: inherit; font-size: 13px; line-height: 1.3; }
.metric-sub { display: flex; flex-wrap: wrap; gap: 8px 14px; min-height: 24px; margin-top: 8px; color: var(--ink-2); font-size: 12px; }
.metric-sub.muted { min-height: 0; margin-top: 4px; color: var(--ink-3, #8a8f98); font-size: 11px; }
.metric-foot { margin-top: 10px; color: var(--ink-3); font-size: 11px; line-height: 1.5; }
.covered { color: #28643a; }
.uncovered { color: var(--cinnabar); }
.sheet { margin-top: 14px; }
.section-note { margin: 0 0 12px; line-height: 1.7; }
.source-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.source-card { padding: 14px; border: 1px solid var(--rule-2); background: var(--card); }
.source-rate { margin-top: 14px; color: var(--ink); font: 22px/1.3 var(--serif, Georgia, serif); }
.source-rate small { color: var(--ink-3); font: 12px/1.3 var(--sans, sans-serif); }
.source-count { margin: 15px 0 5px; color: var(--ink); font: 23px/1.3 var(--serif, Georgia, serif); }
.source-count small { color: var(--ink-3); font: 12px/1.3 sans-serif; }
.source-count span { margin: 0 8px; color: var(--ink-3); }
.kind-list { display: flex; flex-wrap: wrap; gap: 6px; }
.sub-title { margin: 22px 0 10px; font-size: 14px; }
.bar-row { display: grid; grid-template-columns: 100px minmax(100px, 1fr) 200px; gap: 10px; align-items: center; margin: 8px 0; font-size: 12px; }
.bar-k { overflow: hidden; color: var(--ink-2); text-overflow: ellipsis; white-space: nowrap; }
.bar { height: 10px; background: var(--indigo-soft); }
.bar i { display: block; height: 100%; background: var(--indigo); }
.bar-v { color: var(--ink-3); text-align: right; font-variant-numeric: tabular-nums; }
.accuracy-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 12px; }
.accuracy-stat { display: flex; flex-direction: column; gap: 6px; padding: 12px; border: 1px solid var(--rule-2); color: var(--ink-3); font-size: 12px; }
.accuracy-stat b { color: var(--ink); font: 22px/1.2 var(--serif, Georgia, serif); }
.accuracy-stat.emphasized { background: var(--indigo-soft); }
.speed-summary { display: flex; align-items: baseline; gap: 12px; margin-bottom: 12px; color: var(--ink-3); font-size: 12px; }
.speed-summary b { color: var(--ink); font: 24px/1.2 var(--serif, Georgia, serif); }
.diffs { padding: 6px 16px; font-size: 13px; line-height: 1.9; color: var(--ink-2); }
@media (max-width: 1150px) { .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 700px) {
  .metric-grid, .source-grid, .accuracy-grid { grid-template-columns: 1fr; }
  .bar-row { grid-template-columns: 82px minmax(70px, 1fr) 145px; gap: 7px; }
  .speed-summary { align-items: flex-start; flex-direction: column; gap: 4px; }
}
</style>
