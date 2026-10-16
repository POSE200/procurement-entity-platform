import { ElMessage } from 'element-plus'

/** 下载后端生成的文件（标准样例表 xlsx 等），文件名取自响应头。 */
export async function downloadFile(url: string, fallbackName: string): Promise<void> {
  const res = await fetch(url)
  if (!res.ok) {
    let detail = `下载失败（HTTP ${res.status}）`
    try {
      const j = await res.json()
      if (j?.detail) detail = String(j.detail)
    } catch { /* 非 JSON 响应 */ }
    throw new Error(detail)
  }
  const cd = res.headers.get('content-disposition') ?? ''
  const m = /filename\*=UTF-8''([^;]+)/i.exec(cd)
  const name = m ? decodeURIComponent(m[1]) : fallbackName
  const counts = (res.headers.get('x-row-counts') ?? '').split(',').map(Number)
  const blob = await res.blob()
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = name
  a.click()
  URL.revokeObjectURL(a.href)
  if (counts.length === 3 && counts.every((n) => !Number.isNaN(n))) {
    ElMessage.success(`已下载：总表 ${counts[0]} 行 · 标的表 ${counts[1]} 行 · 投标人表 ${counts[2]} 行`)
  }
}
