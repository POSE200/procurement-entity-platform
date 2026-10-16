/** 展示层格式化。null/undefined 统一显示为“未披露”，绝不显示为 0。 */

export const MISSING = '未披露'

const moneyFmt = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const numFmt = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 4 })

export function isBlank(v: unknown): boolean {
  return v === null || v === undefined || (typeof v === 'string' && v.trim() === '')
}

export function money(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return MISSING
  return '¥' + moneyFmt.format(v)
}

/** 大额以“万元”展示，便于表头统计卡片阅读 */
export function moneyWan(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return MISSING
  if (Math.abs(v) < 10000) return money(v)
  return moneyFmt.format(v / 10000) + ' 万元'
}

export function num(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return MISSING
  return numFmt.format(v)
}

export function text(v: unknown): string {
  return isBlank(v) ? MISSING : String(v)
}

export function date(v: string | null | undefined): string {
  if (!v) return MISSING
  return v.slice(0, 10)
}

export function dateTime(v: string | null | undefined): string {
  if (!v) return MISSING
  return v.replace('T', ' ').slice(0, 16)
}

export function ms(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return v < 1000 ? `${Math.round(v)} ms` : `${(v / 1000).toFixed(2)} s`
}

/** 统一机构名称用于归并：全角括号转半角、去空白 */
export function normOrg(name: string): string {
  return name.replace(/（/g, '(').replace(/）/g, ')').replace(/\s+/g, '').trim()
}

/** CSV 单元格转义，并防止以 = + - @ 开头的公式注入 */
export function csvCell(v: unknown): string {
  let s = isBlank(v) ? '' : String(v)
  if (/^[=+\-@\t\r]/.test(s)) s = "'" + s
  return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s
}
