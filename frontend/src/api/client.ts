/**
 * 统一业务 API：所有数据都来自本机实时后端 /api/v1（模型抽取结果入库后的 SQL 事实库）。
 */
import { reactive } from 'vue'
import type {
  AnnouncementDetail, AnnouncementSummary, DatasetInventory, EvalReport, Item, ItemQuery, Job, JobSummary, LlmConfig, LlmConfigUpdate,
  Organization, Page, ProfileKind, ProfileRequest, ProfileResponse, ProviderHealth, ProviderInfo, ReadyzInfo, ScenarioId,
  ScenarioRequest, ScenarioResponse, Stats, Task1Overview, Task2Overview,
} from '@/types/api'
import { http, qs } from './http'

const DATASET_KEY = 'ict.datasetId' // 记住最近一次校验清单，切页/刷新后可恢复

export const appState = reactive({
  datasetId: (() => {
    try { return localStorage.getItem(DATASET_KEY) || null } catch { return null }
  })() as string | null,
})

export function setDatasetId(id: string | null) {
  appState.datasetId = id
  try {
    if (id) localStorage.setItem(DATASET_KEY, id)
    else localStorage.removeItem(DATASET_KEY)
  } catch { /* ignore */ }
}

export const api = {
  // ---- 模型诊断 ----
  provider: () => http.get<ProviderInfo>('/api/v1/llm/provider'),
  providerHealth: (provider?: string) => http.post<ProviderHealth>('/api/v1/llm/health' + qs({ provider }), undefined, 90000),
  readyz: async (): Promise<ReadyzInfo> => {
    // readyz 在未就绪时返回 503，但响应体仍有意义
    const res = await fetch('/readyz')
    return res.json()
  },

  llmConfig: () => http.get<LlmConfig>('/api/v1/llm/config'),
  saveLlmConfig: (body: LlmConfigUpdate) => http.put<LlmConfig>('/api/v1/llm/config', body),
  listModels: (provider: string, baseUrl?: string, apiKey?: string) =>
    http.post<{ provider: string; models: string[] }>('/api/v1/llm/models',
      { provider, base_url: baseUrl || undefined, api_key: apiKey || undefined }, 30000),
  evaluation: (jobId?: string) => http.get<EvalReport>('/api/v1/evaluation' + qs({ job_id: jobId }), 120000),
  stats: () => http.get<Stats>('/api/v1/stats'),

  // ---- 数据集与任务 ----
  uploadDataset: (html: File, attachments: File | null, kind: 'batch' | 'single' = 'batch') => {
    const fd = new FormData()
    fd.append(kind === 'single' ? 'html_file' : 'html_zip', html)
    if (attachments) fd.append(kind === 'single' ? 'attachment_file' : 'attachment_zip', attachments)
    return http.post<DatasetInventory>('/api/v1/datasets', fd, 3600000)
  },
  localDefaults: () => http.get<{ html_path: string | null; attachment_path: string | null }>('/api/v1/datasets/local-defaults'),
  localDataset: (htmlPath: string, attachmentPath: string | null, kind: 'batch' | 'single' = 'batch') =>
    http.post<DatasetInventory>('/api/v1/datasets/local', {
      html_path: htmlPath, attachment_path: attachmentPath || null, kind,
    }, 300000),
  inventory: (id: string) => http.get<DatasetInventory>(`/api/v1/datasets/${encodeURIComponent(id)}/inventory`),
  startJob: (datasetId: string, limit?: number) =>
    http.post<{ job_id: string }>(`/api/v1/datasets/${encodeURIComponent(datasetId)}/jobs`, limit ? { limit } : {}),
  jobs: () => http.get<JobSummary[]>('/api/v1/jobs'),
  clearRecords: () => http.delete<{
    cleared: Record<string, number>; datasets_preserved: boolean; source_files_preserved: boolean
  }>('/api/v1/records'),
  clearJob: (id: string) => http.delete<{ job_id: string; results_preserved: boolean; cache_preserved: boolean }>(`/api/v1/jobs/${encodeURIComponent(id)}`),
  job: (id: string) => http.get<Job>(`/api/v1/jobs/${encodeURIComponent(id)}`),
  retryJob: (id: string, announcementIds: string[]) =>
    http.post<Job>(`/api/v1/jobs/${encodeURIComponent(id)}/retry`, { announcement_ids: announcementIds }),

  // ---- 检索与详情 ----
  items: (q: ItemQuery) => http.get<Page<Item>>('/api/v1/items' + qs({ ...q })),
  /** 按赛题《输出标准样例表》下载提取结果（总表/标的表/投标人表），返回下载地址 */
  standardExportUrl: (scope: { job_id?: string; dataset_id?: string; announcement_id?: string } = {}) =>
    '/api/v1/export/standard' + qs(scope),
  exportItems: (q: ItemQuery) => http.get<Item[]>('/api/v1/items/export' + qs({ ...q, format: 'json' }), 120000),
  announcement: (id: string, jobId?: string) => http.get<AnnouncementDetail>(`/api/v1/announcements/${encodeURIComponent(id)}` + qs({ job_id: jobId })),
  announcements: (q: string, jobId?: string) => http.get<AnnouncementSummary[]>('/api/v1/announcements' + qs({ q, limit: 50, job_id: jobId })),
  task1Overview: (refresh = false, jobId?: string) => http.get<Task1Overview>(
    '/api/v1/task1/overview' + qs({ refresh: refresh ? 1 : undefined, job_id: jobId }), 180000),
  task2Overview: (jobId?: string) => http.get<Task2Overview>('/api/v1/task2/overview' + qs({ job_id: jobId }), 60000),
  profile: (kind: ProfileKind, req: ProfileRequest) => http.post<ProfileResponse>(`/api/v1/profiles/${kind}`, req),
  organizations: (q: string, role: 'buyer' | 'winner' | 'bidder', jobId?: string) =>
    http.get<Organization[]>('/api/v1/organizations' + qs({ q, role, job_id: jobId })),
  scenario: (id: ScenarioId, req: ScenarioRequest) =>
    http.post<ScenarioResponse>(`/api/v1/scenarios/${id}/query`, req),
}
