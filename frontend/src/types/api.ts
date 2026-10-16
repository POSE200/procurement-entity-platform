/**
 * 前后端契约（规划第 8.2 节）。后端实现业务接口时以此为准；字段缺失一律为 null，不以 0 代替。
 * 金额单位统一为“元”，由后端完成万元换算。
 */

export type DataMode = 'live' | 'sample'

export interface ProviderInfo {
  provider: string
  model: string
  base_url: string
  allowed: boolean
  key_configured: boolean
}

export interface ProviderHealth {
  ok: boolean
  configured: boolean
  reason?: string
  detail?: string
  response?: string
}

export interface ProviderSlot {
  base_url: string
  model: string
  key_configured: boolean
  key_hint: string | null
}

export interface LlmConfig {
  provider: 'qwen' | 'deepseek'
  max_concurrency: number
  timeout_seconds: number
  providers: Record<'qwen' | 'deepseek', ProviderSlot>
}

export interface LlmConfigUpdate {
  provider: 'qwen' | 'deepseek'
  base_url?: string
  model?: string
  /** 留空表示不修改 */
  api_key?: string
  make_default?: boolean
  max_concurrency?: number
  timeout_seconds?: number
}

export interface Stats {
  announcements: number
  items: number
  organizations: number
  revision: number
}

export interface ReadyzInfo {
  status: string
  scope: string
  provider_configured: boolean
  parser_initialized: boolean
  neo4j: { configured: boolean; reason?: string }
}

// ---------- 数据集与任务 ----------
  export interface InventoryEntry {
    announcement_id: string
    html_present: boolean
    attachment_present: boolean
    status: 'paired' | 'html_only' | 'attachment_only' | 'duplicate' | 'invalid'
    message?: string | null
    processing_status?: 'queued' | 'running' | 'succeeded' | 'failed' | 'skipped' | null
    processing_job_id?: string | null
    processing_message?: string | null
    processing_stage?: string | null
  }

export interface DatasetInventory {
  dataset_id: string
  created_at: string
  html_count: number
  attachment_count: number
  paired: number
  html_only: number
    attachment_only: number
    conflicts: number
    pending_paired?: number
    pending_html_only?: number
    processing?: number
    processed?: number
    entries: InventoryEntry[]
  }

export type StageName = 'parse' | 'extract' | 'review' | 'validate' | 'store' | 'graph'
export type StageStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'skipped'

export interface JobStage {
  name: StageName
  status: StageStatus
  done: number
  failed: number
  skipped?: number
  total: number
  elapsed_ms: number | null
}

export interface JobFailure {
  announcement_id: string
  stage: StageName
  error_code: string
  message: string
  retryable: boolean
}

export interface JobSummary {
  job_id: string
  display_name: string
  dataset_id: string
  provider: string
  model: string
  created_at: string
  total: number
  succeeded: number
  skipped: number
  stored: number
  failed: number
  active: number
  is_official: boolean
}

export interface Job {
  display_name: string
  job_id: string
  dataset_id: string
  status: 'queued' | 'running' | 'succeeded' | 'partial' | 'failed'
  provider: string
  model: string
  created_at: string
  updated_at: string
  total: number
  succeeded: number
  failed: number
  skipped?: number
  stored?: number
  placeholders?: { announcement_id: string; message: string | null }[]
  needs_review: number
  /** 墙钟耗时（秒）；各阶段 elapsed_ms 是并发任务累计值 */
  wall_seconds?: number
  per_minute?: number | null
  eta_seconds?: number | null
  stages: JobStage[]
  failures: JobFailure[]
}

// ---------- 准确率评测 ----------
export interface Prf {
  label: string
  tp: number
  fp: number
  fn: number
  tn?: number
  accuracy?: number | null
  precision: number | null
  recall: number | null
  f1: number | null
}

export interface EvalDiff {
  field: string
  item: string | null
  pred: unknown
  gold: unknown
}

export interface EvalDoc {
  announcement_id: string
  fields_f1: number | null
  fields_accuracy: number | null
  buyer_ok: boolean
  amount_ok: boolean
  pred_items: number
  gold_items: number
  item_exact: boolean
  diffs: EvalDiff[]
  diff_count: number
  pred_buyer: string | null
  gold_buyer: string | null
  pred_amount: number | null
  gold_amount: number | null
}

export interface EvalReport {
  job_id: string | null
  provider: string | null
  model: string | null
  created_at: string
  evaluated: number
  requested: number
  gold_source: string
  summary: {
    fields_micro: Prf
    fields_macro_f1: number | null
    rows: Prf
    winners: Prf
    bidders: Prf
    buyer_accuracy: number | null
    amount_accuracy: number | null
    doc_exact_rate: number | null
  }
  fields: Prf[]
  worst: EvalDoc[]
}

// ---------- 标的物 ----------
export interface Item {
  item_id: string
  announcement_id: string
  job_id?: string | null
  package_no: string | null
  item_name: string | null
  category: string | null
  brand: string | null
  model: string | null
  unit_price: number | null
  quantity: number | null
  unit: string | null
  total_price: number | null
  supplier: string | null
  buyer?: string | null
  project_name?: string | null
  publish_time?: string | null
}

export interface ItemQuery {
  q?: string
  announcement_id?: string
  job_id?: string
  buyer?: string
  supplier?: string
  brand?: string
  date_from?: string
  date_to?: string
  page: number
  page_size: number
  sort?: string
}

export interface Page<T> {
  total: number
  page: number
  page_size: number
  rows: T[]
}

// ---------- 公告 ----------
export interface Package {
  package_no: string | null
  package_name: string | null
  winner: string | null
  amount?: number | null
}

export interface Bidder {
  package_no: string | null
  name: string
  role: 'winner' | 'participant'
  total_score?: number | null
  rank?: number | null
  amount?: number | null
}

export interface AnnouncementSummary {
  announcement_id: string
  title: string
  publish_time: string | null
  project_no: string | null
  buyer: string | null
  project_name: string | null
  total_amount: number | null
  packages: Package[]
  bidders: Bidder[]
  item_count: number
  has_attachment: boolean
  gold_source?: string | null
}

export interface AnnouncementDetail extends AnnouncementSummary {
  summary_text: string
  body_text: string
  review_note?: string | null
  annotated_at?: string | null
  items: Item[]
}

export interface Evidence {
  evidence_id: string
  announcement_id: string
  source_type: 'html' | 'attachment'
  source_name: string
  field_path: string
  quote: string
  context: string
  review_status: 'unreviewed' | 'confirmed' | 'rejected'
}

// ---------- 五场景 ----------
export type ScenarioId = 1 | 2 | 3 | 4 | 5
export type Scope = 'project' | 'package'

export interface ScenarioRequest {
  job_id?: string
  dataset_id?: string
  buyer_id?: string
  supplier_ids?: string[]
  date_range?: [string, string] | null
  scope: Scope
  top_k: number
  /** 场景 2：仅统计未中标参与方 */
  participant_only?: boolean
}

export interface ScenarioColumn {
  key: string
  label: string
  kind?: 'text' | 'count' | 'money' | 'list'
}

export interface GraphNode {
  id: string
  label: string
  kind: 'buyer' | 'supplier' | 'focus' | 'project'
  weight?: number
}

export interface GraphEdge {
  source: string
  target: string
  label?: string
  weight?: number
}

export interface ScenarioTable {
  title: string
  columns: ScenarioColumn[]
  rows: Record<string, unknown>[]
}

export interface ScenarioResponse {
  scenario: ScenarioId
  definition: string
  columns: ScenarioColumn[]
  rows: Record<string, unknown>[]
  /** 次级统计表，例如场景 2 的“高频共同参与组合” */
  secondary?: ScenarioTable | null
  nodes: GraphNode[]
  edges: GraphEdge[]
  project_count: number
  package_count: number
  known_amount: number | null
  unknown_count: number
  evidence_refs: Record<string, string[]>
  source_revision: string
  query_ms: number
  notes?: string[]
}

export interface Organization {
  org_id: string
  name: string
  as_buyer: number
  as_winner: number
  as_participant: number
}

// ---------- 用户画像 ----------
export type ProfileKind = 'buyer' | 'supplier'

export interface ProfileRequest {
  job_id?: string
  org: string
  date_range?: [string, string] | null
  scope: Scope
}

export interface ProfileMetric {
  label: string
  value: number | string | null
  kind: 'count' | 'money' | 'num' | 'pct' | 'text'
}

export interface ProfileEntity {
  _key: string
  name: string
  count: number
  amount?: number | null
  share?: number
  bids?: number
  beat?: number
  lost?: number
}

export interface ProfileResponse {
  kind: ProfileKind
  org: string
  org_id: string
  definition: string
  project_count: number
  package_count: number
  known_amount: number | null
  unknown_count: number
  metrics: ProfileMetric[]
  tags: string[]
  /** 采购单位画像：主要中标供应商；供应商画像：主要客户（采购单位） */
  top_partners: ProfileEntity[]
  /** 采购单位画像：高频投标主体；供应商画像：主要竞争对手 */
  top_bidders: ProfileEntity[]
  categories: { name: string; count: number }[]
  brands: { name: string; count: number }[]
  trend: { month: string; count: number; amount: number | null }[]
  evidence_refs: Record<string, string[]>
  source_revision: string
  query_ms: number
}

// ---------- 任务二总览 ----------
export interface Task2Model {
  product_suppliers?: number
  edges_buyer_product_supplier?: number
  announcements: number
  projects: number
  packages: number
  buyers: number
  winners: number
  participants: number
  bidders: number
  edges_buyer_winner: number
  edges_buyer_participant: number
  edges_co_bid: number
  bid_records: number
  bid_with_score: number
  bid_with_amount: number
}

export interface Task2Brief {
  rows: number
  project_count: number
  known_amount: number | null
  top: string | null
  top_count: number | null
  secondary_rows: number
}

export interface Task2Overview {
  model: Task2Model
  suggestions: { buyer: string | null; winner: string | null; pair4: string[]; pair5: string[] }
  results: {
    s1?: Task2Brief; s2?: Task2Brief; s3?: Task2Brief; s4?: Task2Brief; s5?: Task2Brief
    buyer_profile?: { tags: string[]; metrics: ProfileMetric[] }
    supplier_profile?: { tags: string[]; metrics: ProfileMetric[] }
  }
  source_revision: string
  query_ms: number
}

export interface Task1Overview {
  job_id: string | null
  merge_migration?: { running: boolean; total: number; done: number; failed: number; rules: string }
  coverage: {
    announcements: number
    with_attachment: number
    attachment_used: number
    image_announcements: number
    attachment_kinds: Record<string, number>
    items_total: number
    items_from_html: number
    items_from_attachment: number
    items_unattributed: number
    html_covered: boolean
    attachment_covered: boolean
    covered_categories: number
    announcements_items_from_html: number
    announcements_items_from_attachment: number
    html_announcement_rate: number | null
    attachment_announcement_rate: number | null
  }
  extraction: {
    available: boolean
    field_annotation_available: boolean
    items: number
    predicted_items: number
    annotated_fields: number
    extracted_fields: number
    rate: number | null
    score_points: number
    max_points: number
    average_points: number | null
    field_annotation_rate: number | null
    fields: { key: string; label: string; annotated: number; extracted: number; rate: number | null }[]
    bands: { key: string; label: string; items: number; points: number; rate: number | null }[]
    complete_items: number
    complete_rate: number | null
    gold_aware?: {
      available: boolean; items: number; null_ok_slots: number; score_points: number; max_points: number
      rate: number | null; average_points: number | null
      bands: { key: string; items: number; points: number; rate: number | null }[]
    }
  }
  accuracy: {
    evaluated: number
    gold_source: string
    accuracy: number | null
    precision: number | null
    recall: number | null
    score: number | null
    tp?: number
    fp?: number
    fn?: number
    tn?: number
    accuracy_no_tn?: number | null
    fields: { label: string; tp: number; fp: number; fn: number; tn?: number; precision: number | null;
              recall: number | null; accuracy: number | null; score: number | null }[]
    worst: EvalDoc[]
  } | null
  speed: {
    jobs: { job_id: string; model: string | null; announcements: number; succeeded: number; failed: number;
            wall_seconds: number; per_announcement_s: number }[]
    total_announcements: number
    total_seconds: number
    per_announcement_s: number | null
  }
  failures: {
    dataset_total: number | null
    paired: number | null
    html_only: number | null
    unresolved: number
    by_code: { error_code: string | null; n: number }[]
  }
  model: string | null
  source_revision: string
  query_ms: number
}
