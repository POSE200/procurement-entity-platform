/**
 * 示例数据模式：读取 public/sample 下由 full_verified 生成的静态 JSON，在浏览器内完成检索与五场景统计。
 * 这些数据是人工复核候选答案，不是实时模型抽取结果；界面在该模式下全程显著标识。
 *
 * 统计口径（规划 2.1 节）：
 * - 业务项目 = 采购单位 + 项目编号（缺编号时退化为公告 ID），项目数按其去重，包数另列。
 * - 项目金额只取公告“总中标金额”，同一项目跨公告按公告去重求和；null 不当 0，计入 unknown_count。
 * - 一个项目多个中标方而金额无法按包拆分时，不把整项目金额重复计给每家，而计入“金额未能归属”。
 */
import type {
  AnnouncementDetail, AnnouncementSummary, GraphEdge, GraphNode, Item, ItemQuery,
  Organization, Page, ProfileEntity, ProfileKind, ProfileMetric, ProfileRequest, ProfileResponse,
  ScenarioId, ScenarioRequest, ScenarioResponse, Task2Brief, Task2Model, Task2Overview,
} from '@/types/api'
import { normOrg } from '@/utils/format'

const BASE = `${import.meta.env.BASE_URL}sample/`

interface Unit {
  key: string // 项目键或 项目键|包号
  projectKey: string
  buyer: string
  title: string
  date: string | null
  amount: number | null
  winners: Set<string>
  bidders: Set<string>
  announcementIds: string[]
  packageNos: string[]
  /** 每个投标主体在本单元的竞标明细；项目级多包时得分取最高、排名取最好、报价求和 */
  bids: Map<string, Bid>
}

interface Bid { score: number | null; rank: number | null; amount: number | null }

const numOrNull = (v: unknown): number | null => {
  if (v === null || v === undefined || v === '') return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

function addBid(u: Unit, n: string, b: { total_score?: number | null; rank?: number | null; amount?: number | null }) {
  const score = numOrNull(b.total_score), rank = numOrNull(b.rank), amount = numOrNull(b.amount)
  const cur = u.bids.get(n)
  if (!cur) { u.bids.set(n, { score, rank, amount }); return }
  if (score !== null && (cur.score === null || score > cur.score)) cur.score = score
  if (rank !== null && (cur.rank === null || rank < cur.rank)) cur.rank = rank
  if (amount !== null) cur.amount = (cur.amount ?? 0) + amount
}

interface Store {
  anns: AnnouncementSummary[]
  annById: Map<string, AnnouncementSummary>
  items: Item[]
  displayName: Map<string, string>
  projects: Map<string, Unit>
  packages: Map<string, Unit>
  orgs: Organization[]
}

let storePromise: Promise<Store> | null = null
let projectIndex: Map<string, Unit> | null = null

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(BASE + path)
  if (!res.ok) throw new Error(`示例数据缺失：${path}（请先运行 scripts/build_sample_data.py）`)
  return res.json() as Promise<T>
}

function projectKeyOf(a: AnnouncementSummary): string {
  return `${normOrg(a.buyer ?? '未知采购单位')}|${a.project_no ?? a.announcement_id}`
}

function addTo(map: Map<string, Unit>, key: string, projectKey: string, a: AnnouncementSummary, amount: number | null) {
  let u = map.get(key)
  if (!u) {
    u = {
      key, projectKey, buyer: normOrg(a.buyer ?? '未知采购单位'), title: a.project_name ?? a.title,
      date: a.publish_time, amount: null, winners: new Set(), bidders: new Set(), announcementIds: [], packageNos: [], bids: new Map(),
    }
    map.set(key, u)
  }
  if (!u.announcementIds.includes(a.announcement_id)) {
    u.announcementIds.push(a.announcement_id)
    if (amount !== null) u.amount = (u.amount ?? 0) + amount
  }
  if (a.publish_time && (!u.date || a.publish_time < u.date)) u.date = a.publish_time
  return u
}

async function load(): Promise<Store> {
  const [anns, items] = await Promise.all([
    getJson<AnnouncementSummary[]>('announcements.json'),
    getJson<Item[]>('items.json'),
  ])
  const annById = new Map(anns.map((a) => [a.announcement_id, a]))
  const displayName = new Map<string, string>()
  const projects = new Map<string, Unit>()
  const packages = new Map<string, Unit>()
  const orgStat = new Map<string, Organization>()
  const stat = (name: string) => {
    const n = normOrg(name)
    if (!displayName.has(n)) displayName.set(n, name)
    let o = orgStat.get(n)
    if (!o) orgStat.set(n, (o = { org_id: n, name, as_buyer: 0, as_winner: 0, as_participant: 0 }))
    return o
  }

  for (const a of anns) {
    const pk = projectKeyOf(a)
    if (a.buyer) stat(a.buyer).as_buyer++
    const proj = addTo(projects, pk, pk, a, a.total_amount)
    for (const p of a.packages) if (p.package_no && !proj.packageNos.includes(p.package_no)) proj.packageNos.push(p.package_no)
    const singlePkg = a.packages.length <= 1
    for (const b of a.bidders) {
      const n = normOrg(b.name)
      stat(b.name)[b.role === 'winner' ? 'as_winner' : 'as_participant']++
      proj.bidders.add(n)
      if (b.role === 'winner') proj.winners.add(n)
      addBid(proj, n, b)
      // 同包级：没有包号且公告只有一个包时视为该包；多包公告缺包号的投标人无法归包，跳过
      const pno = b.package_no ?? (singlePkg ? a.packages[0]?.package_no ?? '1' : null)
      if (pno === null) continue
      const pkgUnit = addTo(packages, `${pk}|${pno}`, pk, a, singlePkg ? a.total_amount : null)
      if (!pkgUnit.packageNos.includes(pno)) pkgUnit.packageNos.push(pno)
      pkgUnit.bidders.add(n)
      if (b.role === 'winner') pkgUnit.winners.add(n)
      addBid(pkgUnit, n, b)
    }
  }

  const itemsJoined = items.map((it) => {
    const a = annById.get(it.announcement_id)
    return { ...it, buyer: a?.buyer ?? null, project_name: a?.project_name ?? null, publish_time: a?.publish_time ?? null }
  })
  const orgs = [...orgStat.values()].sort((x, y) => y.as_buyer + y.as_winner - (x.as_buyer + x.as_winner))
  projectIndex = projects
  return { anns, annById, items: itemsJoined, displayName, projects, packages, orgs }
}

export function sampleStore(): Promise<Store> {
  if (!storePromise) storePromise = load().catch((e) => { storePromise = null; throw e })
  return storePromise
}

// ---------------- 标的物检索 ----------------
const includes = (v: string | null | undefined, q: string) => !!v && v.toLowerCase().includes(q)

export function filterItems(items: Item[], q: ItemQuery): Item[] {
  const kw = q.q?.trim().toLowerCase()
  const buyer = q.buyer?.trim().toLowerCase()
  const supplier = q.supplier?.trim().toLowerCase()
  const brand = q.brand?.trim().toLowerCase()
  return items.filter((it) => {
    if (kw && !(includes(it.item_name, kw) || includes(it.model, kw) || includes(it.category, kw) || includes(it.project_name, kw))) return false
    if (buyer && !includes(it.buyer, buyer)) return false
    if (supplier && !includes(it.supplier, supplier)) return false
    if (brand && !includes(it.brand, brand)) return false
    const d = it.publish_time?.slice(0, 10)
    if (q.date_from && (!d || d < q.date_from)) return false
    if (q.date_to && (!d || d > q.date_to)) return false
    return true
  })
}

export function sortItems(rows: Item[], sort?: string): Item[] {
  if (!sort) return rows
  const desc = sort.startsWith('-')
  const key = (desc ? sort.slice(1) : sort) as keyof Item
  // 空值始终排在最后，不参与大小比较
  return [...rows].sort((a, b) => {
    const x = a[key], y = b[key]
    if (x === null || x === undefined) return 1
    if (y === null || y === undefined) return -1
    const c = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y), 'zh')
    return desc ? -c : c
  })
}

export async function sampleItems(q: ItemQuery): Promise<Page<Item>> {
  const s = await sampleStore()
  const rows = sortItems(filterItems(s.items, q), q.sort)
  const start = (q.page - 1) * q.page_size
  return { total: rows.length, page: q.page, page_size: q.page_size, rows: rows.slice(start, start + q.page_size) }
}

export async function sampleAllItems(q: ItemQuery): Promise<Item[]> {
  const s = await sampleStore()
  return sortItems(filterItems(s.items, q), q.sort)
}

export async function sampleAnnouncement(id: string): Promise<AnnouncementDetail> {
  const s = await sampleStore()
  const a = s.annById.get(id)
  if (!a) throw new Error(`未找到公告 ${id}`)
  const extra = await getJson<{ summary_text: string; body_text: string; review_note?: string; annotated_at?: string }>(`ann/${id}.json`)
  return { ...a, ...extra, items: s.items.filter((it) => it.announcement_id === id) }
}

export async function sampleAnnouncements(q: string, limit = 50): Promise<AnnouncementSummary[]> {
  const s = await sampleStore()
  const kw = q.trim().toLowerCase()
  const list = kw ? s.anns.filter((a) => includes(a.title, kw) || includes(a.announcement_id, kw) || includes(a.buyer, kw)) : s.anns
  return list.slice(0, limit)
}

export async function sampleOrganizations(q: string, role: 'buyer' | 'winner' | 'bidder', limit = 30): Promise<Organization[]> {
  const s = await sampleStore()
  const kw = normOrg(q).toLowerCase()
  const val = (o: Organization) =>
    role === 'buyer' ? o.as_buyer : role === 'winner' ? o.as_winner : o.as_winner + o.as_participant
  return s.orgs
    .filter((o) => val(o) > 0)
    .filter((o) => !kw || o.org_id.toLowerCase().includes(kw))
    .sort((a, b) => val(b) - val(a))
    .slice(0, limit)
}

// ---------------- 五场景 ----------------
type Row = Record<string, unknown>
interface Acc { count: number; known: number | null; unknown: number; units: Set<string>; anns: Set<string> }

const newAcc = (): Acc => ({ count: 0, known: null, unknown: 0, units: new Set(), anns: new Set() })
function bump(acc: Acc, u: Unit, amount: number | null | undefined) {
  if (acc.units.has(u.key)) return
  acc.units.add(u.key)
  acc.count++
  u.announcementIds.forEach((id) => acc.anns.add(id))
  if (amount === null || amount === undefined) acc.unknown++
  else acc.known = (acc.known ?? 0) + amount
}

function unitsFor(s: Store, req: ScenarioRequest): Unit[] {
  const all = [...(req.scope === 'package' ? s.packages : s.projects).values()]
  const [from, to] = req.date_range ?? [null, null]
  return all.filter((u) => {
    const d = u.date?.slice(0, 10)
    if (from && (!d || d < from)) return false
    if (to && (!d || d > to)) return false
    return true
  })
}

/** 金额可归属于某中标方：该单元仅一个中标方且金额已知 */
const attributable = (u: Unit) => (u.winners.size === 1 ? u.amount : null)

function summarize(units: Unit[]) {
  const projectKeys = new Set(units.map((u) => u.projectKey))
  const pkgCount = units.reduce((n, u) => n + Math.max(1, u.packageNos.length), 0)
  // 金额按项目去重：同一项目在同包级下被拆成多个单元时只计一次
  const byProject = new Map<string, number | null>()
  // 同包级单元在多包公告中没有单独金额，因此按项目键回查项目级金额
  for (const u of units) if (!byProject.has(u.projectKey)) byProject.set(u.projectKey, projectIndex?.get(u.projectKey)?.amount ?? u.amount)
  let known: number | null = null
  let unknown = 0
  for (const v of byProject.values()) {
    if (v === null) unknown++
    else known = (known ?? 0) + v
  }
  return { project_count: projectKeys.size, package_count: pkgCount, known_amount: known, unknown_count: unknown }
}

function pairKey(a: string, b: string) {
  return a < b ? `${a}\u0001${b}` : `${b}\u0001${a}`
}

const SCOPE_LABEL = { project: '项目级', package: '同包级' } as const

function scenario1(s: Store, req: ScenarioRequest, units: Unit[]): Partial<ScenarioResponse> {
  const buyer = normOrg(req.buyer_id ?? '')
  const mine = units.filter((u) => u.buyer === buyer)
  const acc = new Map<string, Acc>()
  for (const u of mine) for (const w of u.winners) {
    if (!acc.has(w)) acc.set(w, newAcc())
    bump(acc.get(w)!, u, attributable(u))
  }
  const sorted = [...acc.entries()].sort((a, b) => b[1].count - a[1].count || (b[1].known ?? -1) - (a[1].known ?? -1))
  const rows: Row[] = sorted.map(([k, v], i) => ({
    _key: k, rank: i + 1, supplier: s.displayName.get(k) ?? k, count: v.count, known_amount: v.known, unknown: v.unknown,
  }))
  // 产品品牌：来自标的物明细，品牌文本不等于供应商法人
  const annIds = new Set(mine.flatMap((u) => u.announcementIds))
  const brands = new Map<string, { n: number; anns: Set<string>; sup: Set<string> }>()
  for (const it of s.items) {
    if (!annIds.has(it.announcement_id) || !it.brand) continue
    const b = brands.get(it.brand) ?? { n: 0, anns: new Set(), sup: new Set() }
    b.n++
    b.anns.add(it.announcement_id)
    if (it.supplier) b.sup.add(it.supplier)
    brands.set(it.brand, b)
  }
  const brandRows = [...brands.entries()].sort((a, b) => b[1].n - a[1].n).slice(0, 50).map(([k, v]) => ({
    _key: 'brand:' + k, brand: k, lines: v.n, projects: v.anns.size, suppliers: [...v.sup].join('、') || null,
  }))
  const top = rows.slice(0, req.top_k)
  const nodes: GraphNode[] = [{ id: buyer, label: s.displayName.get(buyer) ?? buyer, kind: 'buyer' },
    ...top.map((r) => ({ id: String(r._key), label: String(r.supplier), kind: 'supplier' as const, weight: Number(r.count) }))]
  const edges: GraphEdge[] = top.map((r) => ({ source: buyer, target: String(r._key), label: `${r.count} 次`, weight: Number(r.count) }))
  const refs: Record<string, string[]> = {}
  sorted.forEach(([k, v]) => (refs[k] = [...v.anns]))
  brands.forEach((v, k) => (refs['brand:' + k] = [...v.anns]))
  return {
    ...summarize(mine),
    definition: `采购单位「${s.displayName.get(buyer) ?? buyer}」在所选时间内的中标供应商，按${SCOPE_LABEL[req.scope]}合作次数降序。金额仅在该单元只有一个中标方时归属，其余计入“金额未归属”。`,
    columns: [
      { key: 'rank', label: '排名', kind: 'count' }, { key: 'supplier', label: '中标供应商' },
      { key: 'count', label: '合作次数', kind: 'count' }, { key: 'known_amount', label: '已归属交易金额', kind: 'money' },
      { key: 'unknown', label: '金额未归属次数', kind: 'count' },
    ],
    rows,
    secondary: {
      title: '产品品牌（来自标的物明细；品牌≠供应商法人，未披露制造商时不推断）',
      columns: [{ key: 'brand', label: '品牌' }, { key: 'lines', label: '标的条数', kind: 'count' },
        { key: 'projects', label: '涉及公告数', kind: 'count' }, { key: 'suppliers', label: '对应中标供应商' }],
      rows: brandRows,
    },
    nodes, edges, evidence_refs: refs,
  }
}

function scenario2(s: Store, req: ScenarioRequest, units: Unit[]): Partial<ScenarioResponse> {
  const buyer = normOrg(req.buyer_id ?? '')
  const mine = units.filter((u) => u.buyer === buyer)
  const only = !!req.participant_only
  const acc = new Map<string, Acc & { wins: number }>()
  const pairs = new Map<string, Acc>()
  for (const u of mine) {
    const list = [...u.bidders].filter((b) => !only || !u.winners.has(b))
    for (const b of list) {
      if (!acc.has(b)) acc.set(b, { ...newAcc(), wins: 0 })
      const a = acc.get(b)!
      if (!a.units.has(u.key) && u.winners.has(b)) a.wins++
      bump(a, u, null)
    }
    for (let i = 0; i < list.length; i++) for (let j = i + 1; j < list.length; j++) {
      const k = pairKey(list[i], list[j])
      if (!pairs.has(k)) pairs.set(k, newAcc())
      bump(pairs.get(k)!, u, null)
    }
  }
  const sorted = [...acc.entries()].sort((a, b) => b[1].count - a[1].count)
  const rows: Row[] = sorted.map(([k, v], i) => ({
    _key: k, rank: i + 1, bidder: s.displayName.get(k) ?? k, count: v.count, wins: v.wins, losses: v.count - v.wins,
  }))
  const pairSorted = [...pairs.entries()].filter(([, v]) => v.count >= 2).sort((a, b) => b[1].count - a[1].count)
  const pairRows = pairSorted.slice(0, 50).map(([k, v]) => {
    const [a, b] = k.split('\u0001')
    return { _key: 'pair:' + k, a: s.displayName.get(a) ?? a, b: s.displayName.get(b) ?? b, count: v.count }
  })
  const top = rows.slice(0, req.top_k)
  const topIds = new Set(top.map((r) => String(r._key)))
  const nodes: GraphNode[] = [{ id: buyer, label: s.displayName.get(buyer) ?? buyer, kind: 'buyer' },
    ...top.map((r) => ({ id: String(r._key), label: String(r.bidder), kind: 'supplier' as const, weight: Number(r.count) }))]
  const edges: GraphEdge[] = top.map((r) => ({ source: buyer, target: String(r._key), label: `${r.count}`, weight: Number(r.count) }))
  for (const [k, v] of pairSorted) {
    const [a, b] = k.split('\u0001')
    if (topIds.has(a) && topIds.has(b)) edges.push({ source: a, target: b, label: `共现 ${v.count}`, weight: v.count })
  }
  const refs: Record<string, string[]> = {}
  sorted.forEach(([k, v]) => (refs[k] = [...v.anns]))
  pairSorted.slice(0, 50).forEach(([k, v]) => (refs['pair:' + k] = [...v.anns]))
  return {
    ...summarize(mine),
    definition: `采购单位「${s.displayName.get(buyer) ?? buyer}」历史${SCOPE_LABEL[req.scope]}单元中的${only ? '未中标参与方' : '全部投标主体'}出现频次。共同参与组合为同一单元内两两共现（≥2 次），共同出现不等于合作或串标。`,
    columns: [
      { key: 'rank', label: '排名', kind: 'count' }, { key: 'bidder', label: '投标主体' },
      { key: 'count', label: '参与次数', kind: 'count' }, { key: 'wins', label: '其中中标', kind: 'count' },
      { key: 'losses', label: '其中未中标', kind: 'count' },
    ],
    rows,
    secondary: { title: '高频共同参与组合（两两共现 ≥ 2 次）', columns: [
      { key: 'a', label: '主体 A' }, { key: 'b', label: '主体 B' }, { key: 'count', label: '共同参与次数', kind: 'count' }], rows: pairRows },
    nodes, edges, evidence_refs: refs,
  }
}

function scenario3(s: Store, req: ScenarioRequest, units: Unit[]): Partial<ScenarioResponse> {
  const focus = normOrg(req.supplier_ids?.[0] ?? '')
  const won = units.filter((u) => u.winners.has(focus))
  const acc = new Map<string, Acc & { wins: number }>()
  for (const u of won) for (const b of u.bidders) {
    if (b === focus) continue
    if (!acc.has(b)) acc.set(b, { ...newAcc(), wins: 0 })
    const a = acc.get(b)!
    if (!a.units.has(u.key) && u.winners.has(b)) a.wins++
    bump(a, u, null)
  }
  const sorted = [...acc.entries()].sort((a, b) => b[1].count - a[1].count)
  const avg2 = (xs: number[]) => (xs.length ? Math.round((xs.reduce((x, y) => x + y, 0) / xs.length) * 100) / 100 : null)
  const rows: Row[] = sorted.map(([k, v], i) => {
    const shared = won.filter((u) => v.units.has(u.key))
    const sm = summarize(shared)
    const mineSc = shared.map((u) => u.bids.get(focus)?.score).filter((x): x is number => x !== null && x !== undefined)
    const theirSc = shared.map((u) => u.bids.get(k)?.score).filter((x): x is number => x !== null && x !== undefined)
    const theirAmt = shared.map((u) => u.bids.get(k)?.amount).filter((x): x is number => !!x)
    return {
      _key: k, rank: i + 1, bidder: s.displayName.get(k) ?? k, count: v.count, wins: v.wins,
      total_amount: sm.known_amount, unknown: sm.unknown_count,
      focus_score: avg2(mineSc), rival_score: avg2(theirSc),
      rival_bid: theirAmt.length ? theirAmt.reduce((x, y) => x + y, 0) : null,
    }
  })
  const top = rows.slice(0, req.top_k)
  const nodes: GraphNode[] = [{ id: focus, label: s.displayName.get(focus) ?? focus, kind: 'focus' },
    ...top.map((r) => ({ id: String(r._key), label: String(r.bidder), kind: 'supplier' as const, weight: Number(r.count) }))]
  const edges: GraphEdge[] = top.map((r) => ({ source: focus, target: String(r._key), label: `共投 ${r.count}`, weight: Number(r.count) }))
  const refs: Record<string, string[]> = {}
  sorted.forEach(([k, v]) => (refs[k] = [...v.anns]))
  const noOthers = won.filter((u) => u.bidders.size <= 1).length
  return {
    ...summarize(won),
    definition: `先确定「${s.displayName.get(focus) ?? focus}」中标的${SCOPE_LABEL[req.scope]}单元（${won.length} 个），再统计这些单元中的其他参与方。表中列出全部参与方；共同项目总金额按项目去重；得分为公告公布的综合得分均值，未公布不计入。`,
    columns: [
      { key: 'rank', label: '排名', kind: 'count' }, { key: 'bidder', label: '共同竞标主体' },
      { key: 'count', label: '共同竞标次数', kind: 'count' },
      { key: 'total_amount', label: '共同项目总金额', kind: 'money' }, { key: 'rival_bid', label: '对方报价合计', kind: 'money' },
      { key: 'focus_score', label: '本方平均得分', kind: 'count' }, { key: 'rival_score', label: '对方平均得分', kind: 'count' },
      { key: 'wins', label: '其中该主体中标', kind: 'count' },
    ],
    rows, nodes, edges, evidence_refs: refs,
    notes: noOthers ? [`${noOthers} 个中标单元未披露其他参与方，不计入共投统计。`] : [],
  }
}

function scenario4(s: Store, req: ScenarioRequest, units: Unit[]): Partial<ScenarioResponse> {
  const sups = [...new Set((req.supplier_ids ?? []).map(normOrg))]
  const perSup = sups.map((sp) => {
    const m = new Map<string, Acc>()
    for (const u of units) if (u.winners.has(sp)) {
      if (!m.has(u.buyer)) m.set(u.buyer, newAcc())
      bump(m.get(u.buyer)!, u, attributable(u))
    }
    return m
  })
  const common = [...(perSup[0]?.keys() ?? [])].filter((b) => perSup.every((m) => m.has(b)))
  const involved: Unit[] = []
  const rows: Row[] = common.map((b) => {
    const us = units.filter((u) => u.buyer === b && sups.some((sp) => u.winners.has(sp)))
    involved.push(...us)
    const sum = summarize(us)
    const row: Row = { _key: b, buyer: s.displayName.get(b) ?? b, projects: sum.project_count, total_amount: sum.known_amount, unknown: sum.unknown_count }
    sups.forEach((_, i) => { row[`c${i}`] = perSup[i].get(b)!.count; row[`a${i}`] = perSup[i].get(b)!.known })
    return row
  }).sort((a, b) => Number(b.projects) - Number(a.projects))
  const nodes: GraphNode[] = [
    ...sups.map((sp) => ({ id: sp, label: s.displayName.get(sp) ?? sp, kind: 'focus' as const })),
    ...rows.slice(0, req.top_k * 2).map((r) => ({ id: String(r._key), label: String(r.buyer), kind: 'buyer' as const, weight: Number(r.projects) })),
  ]
  const shown = new Set(rows.slice(0, req.top_k * 2).map((r) => String(r._key)))
  const edges: GraphEdge[] = []
  sups.forEach((sp, i) => perSup[i].forEach((v, b) => { if (shown.has(b)) edges.push({ source: sp, target: b, label: `${v.count} 次`, weight: v.count }) }))
  const refs: Record<string, string[]> = {}
  for (const r of rows) refs[String(r._key)] = [...new Set(involved.filter((u) => u.buyer === r._key).flatMap((u) => u.announcementIds))]
  const cols = sups.flatMap((sp, i) => {
    const n = s.displayName.get(sp) ?? sp
    const short = n.length > 10 ? n.slice(0, 10) + '…' : n
    return [{ key: `c${i}`, label: `${short} 次数`, kind: 'count' as const }, { key: `a${i}`, label: `${short} 已归属金额`, kind: 'money' as const }]
  })
  return {
    ...summarize(involved),
    definition: `所选 ${sups.length} 家中标供应商都有中标记录的采购单位（集合交集）。逐家次数/金额按${SCOPE_LABEL[req.scope]}统计；去重汇总列按项目去重，同一项目不因多家中标而重复累加。`,
    columns: [{ key: 'buyer', label: '共同采购单位' }, ...cols,
      { key: 'projects', label: '去重项目数', kind: 'count' }, { key: 'total_amount', label: '去重项目总金额', kind: 'money' },
      { key: 'unknown', label: '金额未知项目', kind: 'count' }],
    rows, nodes, edges, evidence_refs: refs,
  }
}

function scenario5(s: Store, req: ScenarioRequest, units: Unit[]): Partial<ScenarioResponse> {
  const sups = [...new Set((req.supplier_ids ?? []).map(normOrg))]
  const joint = units.filter((u) => sups.every((sp) => u.bidders.has(sp)))
  const rows: Row[] = joint.map((u) => {
    const row: Row = { _key: u.key, project: u.title, buyer: s.displayName.get(u.buyer) ?? u.buyer, date: u.date, amount: u.amount,
      packages: u.packageNos.join('、') || null }
    sups.forEach((sp, i) => {
      row[`r${i}`] = u.winners.has(sp) ? '中标' : '未中标'
      const bid = u.bids.get(sp)
      row[`s${i}`] = bid?.score ?? null
      row[`k${i}`] = bid?.rank ?? null
      row[`b${i}`] = bid?.amount ?? null
    })
    row.winner = [...u.winners].sort().map((w) => s.displayName.get(w) ?? w).join('、') || null
    row.n_bidders = u.bidders.size
    return row
  }).sort((a, b) => String(b.date ?? '').localeCompare(String(a.date ?? '')))
  const jointWins = sups.map((sp) => joint.filter((u) => u.winners.has(sp)).length)
  const otherWin = joint.filter((u) => !sups.some((sp) => u.winners.has(sp))).length
  const pct = (n: number) => (joint.length ? `${((n / joint.length) * 100).toFixed(0)}%` : null)
  const summaryRows: Row[] = [
    ...sups.map((sp, i) => ({ _key: 'sum:' + sp, supplier: s.displayName.get(sp) ?? sp, joint: joint.length, wins: jointWins[i], rate: pct(jointWins[i]) })),
    { _key: 'sum:other', supplier: '（均未中标，由其他主体中标）', joint: joint.length, wins: otherWin, rate: pct(otherWin) },
  ]
  const perCols = sups.flatMap((sp, i) => {
    const n = s.displayName.get(sp) ?? sp
    const short = n.length > 8 ? n.slice(0, 8) + '…' : n
    return [{ key: `r${i}`, label: `${short} 结果` }, { key: `s${i}`, label: `${short} 得分`, kind: 'count' as const },
      { key: `k${i}`, label: `${short} 排名`, kind: 'count' as const }, { key: `b${i}`, label: `${short} 报价`, kind: 'money' as const }]
  })
  const sm5 = summarize(joint)
  const nodes: GraphNode[] = [
    ...sups.map((sp) => ({ id: sp, label: s.displayName.get(sp) ?? sp, kind: 'focus' as const })),
    ...joint.slice(0, req.top_k * 3).map((u) => ({ id: 'p:' + u.key, label: u.title.length > 16 ? u.title.slice(0, 16) + '…' : u.title, kind: 'project' as const })),
  ]
  const edges: GraphEdge[] = joint.slice(0, req.top_k * 3).flatMap((u) =>
    sups.map((sp) => ({ source: sp, target: 'p:' + u.key, label: u.winners.has(sp) ? '中标' : '参与' })))
  const refs: Record<string, string[]> = {}
  joint.forEach((u) => (refs[u.key] = u.announcementIds))
  return {
    ...sm5,
    definition: `所选 ${sups.length} 家供应商在同一${SCOPE_LABEL[req.scope]}单元中均作为投标主体出现的项目，逐家列出竞标结果、综合得分、排名和报价（原文未公布为空）。共同参与不要求各自都中标；项目金额按项目去重。`,
    columns: [{ key: 'project', label: '项目' }, { key: 'buyer', label: '采购单位' }, { key: 'date', label: '公告日期' },
      ...perCols, { key: 'winner', label: '中标方' }, { key: 'n_bidders', label: '投标家数', kind: 'count' as const },
      { key: 'packages', label: '关联包' }, { key: 'amount', label: '项目金额', kind: 'money' as const }],
    rows,
    secondary: {
      title: `共同竞标结果汇总（共同项目 ${sm5.project_count} 个）`,
      columns: [{ key: 'supplier', label: '主体' }, { key: 'joint', label: '共同竞标单元', kind: 'count' },
        { key: 'wins', label: '中标次数', kind: 'count' }, { key: 'rate', label: '中标占比' }],
      rows: summaryRows,
    },
    nodes, edges, evidence_refs: refs,
  }
}

const HANDLERS = { 1: scenario1, 2: scenario2, 3: scenario3, 4: scenario4, 5: scenario5 } as const

export async function sampleScenario(id: ScenarioId, req: ScenarioRequest): Promise<ScenarioResponse> {
  const s = await sampleStore()
  const t0 = performance.now()
  const part = HANDLERS[id](s, req, unitsFor(s, req))
  return {
    scenario: id, definition: '', columns: [], rows: [], nodes: [], edges: [], project_count: 0, package_count: 0,
    known_amount: null, unknown_count: 0, evidence_refs: {}, notes: [], secondary: null,
    ...part,
    source_revision: 'sample:full_verified@2026-09-24-draft',
    query_ms: performance.now() - t0,
  }
}

// ---------------- 用户画像（与 backend/app/profiles.py 同口径） ----------------
const avg = (xs: number[]) => (xs.length ? Math.round((xs.reduce((a, b) => a + b, 0) / xs.length) * 10000) / 10000 : null)
const isNum = (x: number | null | undefined): x is number => x !== null && x !== undefined

function trendOf(units: Unit[], amountOf: (u: Unit) => number | null) {
  const m = new Map<string, { month: string; count: number; amount: number | null }>()
  for (const u of units) {
    const k = u.date?.slice(0, 7)
    if (!k) continue
    const r = m.get(k) ?? { month: k, count: 0, amount: null }
    r.count++
    const a = amountOf(u)
    if (a !== null) r.amount = (r.amount ?? 0) + a
    m.set(k, r)
  }
  return [...m.keys()].sort().map((k) => m.get(k)!)
}

function catsOf(s: Store, annIds: Set<string>, supplier?: string, limit = 10) {
  const cats = new Map<string, number>(), brands = new Map<string, number>()
  for (const it of s.items) {
    if (!annIds.has(it.announcement_id)) continue
    if (supplier && normOrg(it.supplier ?? '') !== supplier) continue
    if (it.category) { const c = it.category.split('/').pop()!.split('\\').pop()!; cats.set(c, (cats.get(c) ?? 0) + 1) }
    if (it.brand) brands.set(it.brand, (brands.get(it.brand) ?? 0) + 1)
  }
  const top = (m: Map<string, number>) => [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, limit).map(([name, count]) => ({ name, count }))
  return { categories: top(cats), brands: top(brands) }
}

const metric = (label: string, value: number | string | null, kind: ProfileMetric['kind']): ProfileMetric => ({ label, value, kind })
function buyerProfile(s: Store, req: ProfileRequest, units: Unit[]): Omit<ProfileResponse, 'source_revision' | 'query_ms'> {
  const buyer = normOrg(req.org)
  const mine = units.filter((u) => u.buyer === buyer)
  if (!mine.length) throw new Error(`没有找到采购单位「${req.org}」的公告`)
  const sm = summarize(mine)
  const sup = new Map<string, { count: number; amount: number | null }>()
  const bidderCnt = new Map<string, number>()
  const nBidders: number[] = [], winnerScores: number[] = [], winDiscount: number[] = []
  for (const u of mine) {
    if (u.bidders.size > 0) nBidders.push(u.bidders.size)
    for (const b of u.bidders) bidderCnt.set(b, (bidderCnt.get(b) ?? 0) + 1)
    for (const w of u.winners) {
      const r = sup.get(w) ?? { count: 0, amount: null }
      r.count++
      const a = attributable(u)
      if (a !== null) r.amount = (r.amount ?? 0) + a
      sup.set(w, r)
      const sc = u.bids.get(w)?.score
      if (isNum(sc)) winnerScores.push(sc)
    }
    const others = [...u.bids.entries()].filter(([k, v]) => !u.winners.has(k) && v.amount).map(([, v]) => v.amount!)
    const wins = [...u.winners].map((w) => u.bids.get(w)?.amount).filter((x): x is number => !!x)
    if (others.length && wins.length === 1) winDiscount.push(wins[0] / (others.reduce((a, b) => a + b, 0) / others.length))
  }
  const totalWins = [...sup.values()].reduce((n, r) => n + r.count, 0) || 1
  const topSup = [...sup.entries()].sort((a, b) => b[1].count - a[1].count || (b[1].amount ?? 0) - (a[1].amount ?? 0))
  const shares = topSup.map(([, r]) => r.count / totalWins)
  const annIds = new Set(mine.flatMap((u) => u.announcementIds))
  const { categories, brands } = catsOf(s, annIds)
  const dates = mine.map((u) => u.date?.slice(0, 10)).filter((d): d is string => !!d).sort()
  const competitive = nBidders.filter((n) => n >= 3).length
  const tags: string[] = []
  if (sm.project_count >= 5) tags.push('高频采购')
  if (shares.length >= 2 && shares[0] >= 0.5) tags.push('供应商集中')
  else if (shares.length >= 4) tags.push('供应商分散')
  if (nBidders.length && competitive / nBidders.length >= 0.7) tags.push('竞争充分')
  else if (nBidders.length && nBidders.reduce((a, b) => a + b, 0) / nBidders.length < 2) tags.push('竞争信息少')
  if ((sm.known_amount ?? 0) >= 1e7) tags.push('千万级采购')
  const name = (k: string) => s.displayName.get(k) ?? k
  const refs: Record<string, string[]> = {}
  for (const k of bidderCnt.keys()) refs[k] = [...new Set(mine.filter((u) => u.bidders.has(k)).flatMap((u) => u.announcementIds))].sort()
  return {
    kind: 'buyer', org: name(buyer), org_id: buyer, ...sm,
    definition: `采购单位「${name(buyer)}」在所选时间内作为采购人的${SCOPE_LABEL[req.scope]}单元统计。`,
    metrics: [
      metric('采购项目数', sm.project_count, 'count'), metric('已知成交总额', sm.known_amount, 'money'),
      metric('合作中标供应商', sup.size, 'count'), metric('出现过的投标主体', bidderCnt.size, 'count'),
      metric('平均每单元投标家数', avg(nBidders), 'num'),
      metric('充分竞争单元占比（≥3 家）', nBidders.length ? competitive / nBidders.length : null, 'pct'),
      metric('第一大供应商份额', shares[0] ?? null, 'pct'),
      metric('供应商集中度 HHI', shares.length ? Math.round(shares.reduce((a, x) => a + x * x, 0) * 10000) / 10000 : null, 'num'),
      metric('中标方平均得分', avg(winnerScores), 'num'), metric('中标价 / 其他报价均值', avg(winDiscount), 'pct'),
      metric('首次公告', dates[0] ?? null, 'text'), metric('最近公告', dates[dates.length - 1] ?? null, 'text'),
    ],
    tags,
    top_partners: topSup.slice(0, 10).map(([k, v]) => ({ _key: k, name: name(k), count: v.count, amount: v.amount, share: v.count / totalWins })),
    top_bidders: [...bidderCnt.entries()].sort((a, b) => b[1] - a[1]).slice(0, 10).map(([k, v]) => ({ _key: k, name: name(k), count: v })),
    categories, brands,
    trend: trendOf(mine, (u) => (projectIndex?.get(u.projectKey) ?? u).amount),
    evidence_refs: refs,
  }
}
function supplierProfile(s: Store, req: ProfileRequest, units: Unit[]): Omit<ProfileResponse, 'source_revision' | 'query_ms'> {
  const sp = normOrg(req.org)
  const joined = units.filter((u) => u.bidders.has(sp))
  if (!joined.length) throw new Error(`没有找到投标主体「${req.org}」的投标记录`)
  const won = joined.filter((u) => u.winners.has(sp))
  const scores: number[] = [], ranks: number[] = [], ratio: number[] = []
  let wonAmount: number | null = null, wonUnknown = 0
  for (const u of joined) {
    const bid = u.bids.get(sp)
    if (isNum(bid?.score)) scores.push(bid!.score!)
    if (isNum(bid?.rank)) ranks.push(bid!.rank!)
    const wins = [...u.winners].map((w) => u.bids.get(w)?.amount).filter((x): x is number => !!x)
    if (bid?.amount && wins.length === 1 && !u.winners.has(sp)) ratio.push(bid.amount / wins[0])
  }
  for (const u of won) {
    const a = attributable(u)
    if (a === null) wonUnknown++
    else wonAmount = (wonAmount ?? 0) + a
  }
  const buyers = new Map<string, { bids: number; wins: number; amount: number | null }>()
  for (const u of joined) {
    const r = buyers.get(u.buyer) ?? { bids: 0, wins: 0, amount: null }
    r.bids++
    if (u.winners.has(sp)) {
      r.wins++
      const a = attributable(u)
      if (a !== null) r.amount = (r.amount ?? 0) + a
    }
    buyers.set(u.buyer, r)
  }
  const rivals = new Map<string, { count: number; beat: number; lost: number; anns: Set<string> }>()
  for (const u of joined) for (const b of u.bidders) {
    if (b === sp) continue
    const r = rivals.get(b) ?? { count: 0, beat: 0, lost: 0, anns: new Set<string>() }
    r.count++
    u.announcementIds.forEach((a) => r.anns.add(a))
    if (u.winners.has(sp) && !u.winners.has(b)) r.beat++
    else if (u.winners.has(b) && !u.winners.has(sp)) r.lost++
    rivals.set(b, r)
  }
  const { categories, brands } = catsOf(s, new Set(won.flatMap((u) => u.announcementIds)), sp)
  const winRate = won.length / joined.length
  const topBuyers = [...buyers.entries()].sort((a, b) => b[1].wins - a[1].wins || b[1].bids - a[1].bids)
  const topRivals = [...rivals.entries()].sort((a, b) => b[1].count - a[1].count)
  const dates = joined.map((u) => u.date?.slice(0, 10)).filter((d): d is string => !!d).sort()
  const tags: string[] = []
  if (joined.length >= 5) tags.push('活跃投标')
  if (joined.length >= 3 && winRate >= 0.6) tags.push('高中标率')
  else if (joined.length >= 3 && winRate <= 0.2) tags.push('陪标/低中标率')
  const winsBy = [...buyers.values()].map((v) => v.wins).filter((x) => x > 0)
  const maxW = winsBy.length ? Math.max(...winsBy) : 0
  if (maxW >= 2 && maxW / winsBy.reduce((a, b) => a + b, 0) >= 0.6) tags.push('依赖单一客户')
  else if (winsBy.length >= 3) tags.push('客户多元')
  if ((wonAmount ?? 0) >= 1e7) tags.push('千万级中标')
  const name = (k: string) => s.displayName.get(k) ?? k
  const refs: Record<string, string[]> = {}
  rivals.forEach((v, k) => (refs[k] = [...v.anns].sort()))
  buyers.forEach((_, k) => (refs[k] = [...new Set(joined.filter((u) => u.buyer === k).flatMap((u) => u.announcementIds))].sort()))
  return {
    kind: 'supplier', org: name(sp), org_id: sp, ...summarize(won),
    definition: `「${name(sp)}」作为投标主体出现的${SCOPE_LABEL[req.scope]}单元统计；中标金额仅计单一中标方单元。`,
    metrics: [
      metric('参与投标', joined.length, 'count'), metric('中标', won.length, 'count'), metric('中标率', winRate, 'pct'),
      metric('已归属中标金额', wonAmount, 'money'), metric('金额未归属中标', wonUnknown, 'count'),
      metric('服务采购单位', winsBy.length, 'count'), metric('平均综合得分', avg(scores), 'num'),
      metric('平均排名', avg(ranks), 'num'), metric('未中标时报价 / 中标价', avg(ratio), 'pct'),
      metric('竞争对手数', rivals.size, 'count'),
      metric('首次出现', dates[0] ?? null, 'text'), metric('最近出现', dates[dates.length - 1] ?? null, 'text'),
    ],
    tags,
    top_partners: topBuyers.slice(0, 10).map(([k, v]): ProfileEntity => ({ _key: k, name: name(k), count: v.wins, bids: v.bids, amount: v.amount })),
    top_bidders: topRivals.slice(0, 10).map(([k, v]): ProfileEntity => ({ _key: k, name: name(k), count: v.count, beat: v.beat, lost: v.lost })),
    categories, brands,
    trend: trendOf(won, attributable),
    evidence_refs: refs,
  }
}

export async function sampleProfile(kind: ProfileKind, req: ProfileRequest): Promise<ProfileResponse> {
  const s = await sampleStore()
  const t0 = performance.now()
  const r: ProfileRequest = { ...req, scope: req.scope ?? 'project' }
  const units = unitsFor(s, { scope: r.scope, top_k: 5, date_range: r.date_range ?? null })
  const part = kind === 'buyer' ? buyerProfile(s, r, units) : supplierProfile(s, r, units)
  return { ...part, source_revision: 'sample:full_verified@2026-09-24-draft', query_ms: performance.now() - t0 }
}

// ---------------- 任务二总览（与 backend/app/task2.py 同口径） ----------------
function pairsOf(list: string[]): [string, string][] {
  const xs = [...list].sort()
  const out: [string, string][] = []
  for (let i = 0; i < xs.length; i++) for (let j = i + 1; j < xs.length; j++) out.push([xs[i], xs[j]])
  return out
}
function topKey<T>(m: Map<T, number>): T | null {
  let best: T | null = null, n = -1
  for (const [k, v] of m) if (v > n) { best = k; n = v }
  return best
}
const inc = <T,>(m: Map<T, number>, k: T) => m.set(k, (m.get(k) ?? 0) + 1)

function brief(r: Partial<ScenarioResponse>, labelKey: string): Task2Brief {
  const rows = r.rows ?? []
  const top = rows[0]
  return {
    rows: rows.length, project_count: r.project_count ?? 0, known_amount: r.known_amount ?? null,
    top: top ? (top[labelKey] as string) ?? null : null, top_count: top ? (top.count as number) ?? null : null,
    secondary_rows: r.secondary?.rows.length ?? 0,
  }
}

export async function sampleTask2Overview(): Promise<Task2Overview> {
  const s = await sampleStore()
  const t0 = performance.now()
  const units = [...s.projects.values()]
  const winners = new Set(units.flatMap((u) => [...u.winners]))
  const bidders = new Set(units.flatMap((u) => [...u.bidders]))
  const bw = new Set<string>(), bb = new Set<string>(), co = new Set<string>()
  let bidRecords = 0, scored = 0, priced = 0
  for (const u of units) {
    for (const w of u.winners) bw.add(u.buyer + '\u0001' + w)
    for (const b of u.bidders) if (!u.winners.has(b)) bb.add(u.buyer + '\u0001' + b)
    for (const [a, b] of pairsOf([...u.bidders])) co.add(a + '\u0001' + b)
    for (const v of u.bids.values()) { bidRecords++; if (v.score !== null) scored++; if (v.amount !== null) priced++ }
  }
  const model: Task2Model = {
    announcements: s.anns.length, projects: units.length, packages: s.packages.size,
    buyers: new Set(units.map((u) => u.buyer)).size, winners: winners.size,
    participants: [...bidders].filter((b) => !winners.has(b)).length, bidders: bidders.size,
    edges_buyer_winner: bw.size, edges_buyer_participant: bb.size, edges_co_bid: co.size,
    bid_records: bidRecords, bid_with_score: scored, bid_with_amount: priced,
  }
  const bc = new Map<string, number>(), wc = new Map<string, number>()
  const byBuyer = new Map<string, Set<string>>()
  const pc5 = new Map<string, number>()
  for (const u of units) {
    if (u.bidders.size >= 2) { inc(bc, u.buyer); for (const w of u.winners) inc(wc, w) }
    const set = byBuyer.get(u.buyer) ?? new Set<string>()
    u.winners.forEach((w) => set.add(w))
    byBuyer.set(u.buyer, set)
    for (const [a, b] of pairsOf([...u.bidders].filter((x) => winners.has(x)))) inc(pc5, a + '\u0001' + b)
  }
  const pc4 = new Map<string, number>()
  for (const ws of byBuyer.values()) for (const [a, b] of pairsOf([...ws])) inc(pc4, a + '\u0001' + b)
  const name = (k: string) => s.displayName.get(k) ?? k
  const buyer = topKey(bc), winner = topKey(wc), p4 = topKey(pc4), p5 = topKey(pc5)
  const suggestions = {
    buyer: buyer ? name(buyer) : null, winner: winner ? name(winner) : null,
    pair4: p4 ? p4.split('\u0001').map(name) : [], pair5: p5 ? p5.split('\u0001').map(name) : [],
  }
  const results: Task2Overview['results'] = {}
  const base = { scope: 'project' as const, top_k: 5, date_range: null }
  const all = unitsFor(s, base)
  if (suggestions.buyer) {
    results.s1 = brief(scenario1(s, { ...base, buyer_id: suggestions.buyer }, all), 'supplier')
    results.s2 = brief(scenario2(s, { ...base, buyer_id: suggestions.buyer }, all), 'bidder')
    const p = buyerProfile(s, { org: suggestions.buyer, scope: 'project' }, all)
    results.buyer_profile = { tags: p.tags, metrics: p.metrics.slice(0, 6) }
  }
  if (suggestions.winner) {
    results.s3 = brief(scenario3(s, { ...base, supplier_ids: [suggestions.winner] }, all), 'bidder')
    const p = supplierProfile(s, { org: suggestions.winner, scope: 'project' }, all)
    results.supplier_profile = { tags: p.tags, metrics: p.metrics.slice(0, 6) }
  }
  if (suggestions.pair4.length === 2) results.s4 = brief(scenario4(s, { ...base, supplier_ids: suggestions.pair4 }, all), 'buyer')
  if (suggestions.pair5.length === 2) results.s5 = brief(scenario5(s, { ...base, supplier_ids: suggestions.pair5 }, all), 'project')
  return { model, suggestions, results, source_revision: 'sample:full_verified@2026-09-24-draft', query_ms: performance.now() - t0 }
}
