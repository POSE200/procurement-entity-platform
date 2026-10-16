import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import type { JobSummary } from '@/types/api'
import { ALL_RESULTS, rememberResultScope, resolveResultScope } from './resultScope'

export function useResultScope() {
  const route = useRoute(), router = useRouter(), page = route.name
  const jobs = ref<JobSummary[]>([]), loading = ref(true), error = ref(''), version = ref(0)
  const total = ref(0)
  const jobId = computed(() => route.query.job && route.query.job !== ALL_RESULTS ? String(route.query.job) : undefined)
  let request = 0
  watch(() => [route.name, route.query.job], async () => {
    if (route.name !== page) return
    const current = ++request
    loading.value = true
    error.value = ''
    try {
      const [list, stats] = await Promise.all([api.jobs(), api.stats()])
      if (current !== request || route.name !== page) return
      jobs.value = list; total.value = stats.announcements
      const selected = resolveResultScope(route.query.job, list)
      if (!route.query.job) {
        await router.replace({ path: route.path, query: { ...route.query, job: selected } })
        return
      }
      if (selected !== ALL_RESULTS && !list.some(j => j.job_id === selected)) throw new Error('任务不存在，请重新选择任务范围')
      loading.value = false
      version.value++
    } catch (e) { if (current === request) { error.value = (e as Error).message; loading.value = false } }
  }, { immediate: true })
  function pick(id: string) {
    rememberResultScope(id)
    const { buyer, one, many, org, ...query } = route.query
    router.replace({ path: route.path, query: { ...query, job: id } })
  }
  return { jobs, jobId, loading, error, version, total, pick }
}
