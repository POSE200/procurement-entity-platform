<script setup lang="ts">
import { computed } from 'vue'
import type { JobSummary } from '@/types/api'
import { ALL_RESULTS, resultScopeName } from '@/utils/resultScope'
const props = defineProps<{ jobId?: string; jobs: JobSummary[]; loading?: boolean; total: number; error?: string }>()
const emit = defineEmits<{ change: [id: string] }>()
const selected = computed(() => props.jobs.find(j => j.job_id === props.jobId))
</script>
<template>
  <section class="sheet result-scope">
    <div class="row">
      <b>任务范围</b>
      <el-select :model-value="jobId || ALL_RESULTS" :loading="loading" aria-label="选择结果任务" style="width: 430px; max-width: 100%" @update:model-value="emit('change', $event)">
        <el-option :value="ALL_RESULTS" label="全部已入库数据（跨任务合并去重）" />
        <el-option v-for="job in jobs" :key="job.job_id" :value="job.job_id" :label="job.display_name" />
      </el-select>
      <span class="hint" v-if="!loading">{{ selected ? `已入库 ${selected.stored} / ${selected.total} 篇` : `全部 ${total} 篇（去重）` }}</span>
    </div>
    <p v-if="error" class="banner err">{{ error }}</p>
    <p v-else class="hint">当前范围：{{ resultScopeName(jobId || '', jobs) }}</p>
  </section>
</template>
<style scoped>
.result-scope { margin-bottom: 16px; }
.result-scope p { margin-bottom: 0; }
</style>
