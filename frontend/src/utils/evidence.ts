/**
 * 在公告纯文本中定位证据片段。只做字符串查找与高亮，不使用 v-html 渲染原始 HTML，避免脚本注入。
 */
export interface Snippet {
  term: string
  before: string
  hit: string
  after: string
  line: number
}

export function findSnippets(text: string, terms: (string | null | undefined)[], radius = 60, perTerm = 2): Snippet[] {
  const out: Snippet[] = []
  const seen = new Set<number>()
  for (const raw of terms) {
    const term = raw?.trim()
    if (!term || term.length < 2) continue
    let from = 0
    for (let n = 0; n < perTerm; n++) {
      const i = text.indexOf(term, from)
      if (i < 0) break
      from = i + term.length
      if (seen.has(i)) continue
      seen.add(i)
      out.push({
        term,
        before: text.slice(Math.max(0, i - radius), i).replace(/\n/g, ' ⏎ '),
        hit: term,
        after: text.slice(i + term.length, i + term.length + radius).replace(/\n/g, ' ⏎ '),
        line: text.slice(0, i).split('\n').length,
      })
    }
  }
  return out
}

/** 将文本切成 高亮/普通 片段，供模板逐段渲染 */
export function splitHighlight(text: string, terms: string[]): { t: string; hit: boolean }[] {
  const ts = [...new Set(terms.filter((t) => t && t.length >= 2))].sort((a, b) => b.length - a.length)
  if (!ts.length) return [{ t: text, hit: false }]
  const re = new RegExp(ts.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'g')
  const parts: { t: string; hit: boolean }[] = []
  let last = 0
  for (const m of text.matchAll(re)) {
    if (m.index! > last) parts.push({ t: text.slice(last, m.index), hit: false })
    parts.push({ t: m[0], hit: true })
    last = m.index! + m[0].length
  }
  if (last < text.length) parts.push({ t: text.slice(last), hit: false })
  return parts
}

/** 数字在原文中可能写作 319300.00 / 319,300.00 / 31.93万元，生成几种候选写法 */
export function numberForms(v: number | null | undefined): string[] {
  if (v === null || v === undefined) return []
  const fixed = v.toFixed(2)
  const [i, d] = fixed.split('.')
  const comma = i.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  const forms = [fixed, `${comma}.${d}`, String(v)]
  if (Number.isInteger(v)) forms.push(comma)
  return [...new Set(forms)].filter((s) => s.length >= 2)
}
