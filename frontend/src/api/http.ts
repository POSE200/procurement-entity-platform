/** 业务 API 基础请求。前端只调用本机后端 /api，不直连任何模型服务，也不持有任何密钥。 */

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public notImplemented = false,
  ) {
    super(message)
  }
}

async function request<T>(method: string, url: string, body?: unknown, timeoutMs = 30000): Promise<T> {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const isForm = body instanceof FormData
    const res = await fetch(url, {
      method,
      signal: ctrl.signal,
      headers: body && !isForm ? { 'Content-Type': 'application/json' } : undefined,
      body: body === undefined ? undefined : isForm ? body : JSON.stringify(body),
    })
    const ct = res.headers.get('content-type') ?? ''
    const data = ct.includes('application/json') ? await res.json() : await res.text()
    if (!res.ok) {
      // 404/405 代表后端尚未实现该业务接口
      const notImpl = res.status === 404 || res.status === 405
      const detail = typeof data === 'object' && data && 'detail' in data ? String((data as { detail: unknown }).detail) : ''
      throw new ApiError(res.status, notImpl ? '后端尚未实现该接口' : detail || `请求失败（HTTP ${res.status}）`, notImpl)
    }
    return data as T
  } catch (e) {
    if (e instanceof ApiError) throw e
    if ((e as Error).name === 'AbortError') throw new ApiError(0, '请求超时')
    throw new ApiError(0, '无法连接后端服务（127.0.0.1:8015）')
  } finally {
    clearTimeout(timer)
  }
}

export const http = {
  get: <T>(url: string, timeoutMs?: number) => request<T>('GET', url, undefined, timeoutMs),
  post: <T>(url: string, body?: unknown, timeoutMs?: number) => request<T>('POST', url, body, timeoutMs),
  patch: <T>(url: string, body?: unknown) => request<T>('PATCH', url, body),
  put: <T>(url: string, body?: unknown) => request<T>('PUT', url, body),
  delete: <T>(url: string) => request<T>('DELETE', url),
}

export function qs(params: Record<string, unknown>): string {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue
    sp.set(k, String(v))
  }
  const s = sp.toString()
  return s ? '?' + s : ''
}
