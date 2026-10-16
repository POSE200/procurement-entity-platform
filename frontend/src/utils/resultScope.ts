import type { JobSummary } from '@/types/api'
export const ALL_RESULTS = '__all__'
const KEY = 'ict.selectedResultJob'
export function rememberResultScope(id: string) {
  try { localStorage.setItem(KEY, id) } catch { /* storage unavailable */ }
}
export function resolveResultScope(raw: unknown, jobs: JobSummary[]): string {
  if (raw != null && String(raw)) return String(raw)
  let saved = ''
  try { saved = localStorage.getItem(KEY) || '' } catch { /* storage unavailable */ }
  if (saved === ALL_RESULTS || jobs.some(j => j.job_id === saved)) return saved
  return jobs.find(j => j.is_official)?.job_id || jobs[0]?.job_id || ALL_RESULTS
}
export function resultScopeName(id: string, jobs: JobSummary[]): string {
  return jobs.find(j => j.job_id === id)?.display_name || (id && id !== ALL_RESULTS ? id : '全部已入库数据（跨任务合并去重）')
}
