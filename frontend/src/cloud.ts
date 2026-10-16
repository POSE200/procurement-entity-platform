const CONFIG_KEY = 'ict.visitorModelConfig'
type Config = Record<string, string | number | null>

function readConfig(): Config {
  try { return JSON.parse(sessionStorage.getItem(CONFIG_KEY) || '{}') } catch { return {} }
}

function candidateConfig(current: Config, body: Record<string, unknown>): Config {
  const next = { ...current }
  const provider = String(body.provider).toUpperCase()
  if (body.make_default !== false) next.LLM_PROVIDER = String(body.provider)
  for (const [field, suffix] of [['base_url', 'BASE_URL'], ['model', 'MODEL'], ['api_key', 'API_KEY']]) {
    if (body[field] != null && body[field] !== '') next[`${provider}_${suffix}`] = String(body[field])
  }
  if (body.max_concurrency != null) next.LLM_MAX_CONCURRENCY = Number(body.max_concurrency)
  if (body.timeout_seconds != null) next.LLM_TIMEOUT_SECONDS = Number(body.timeout_seconds)
  return next
}

const originalFetch = window.fetch.bind(window)
window.fetch = async (input, init) => {
  const url = new URL(input instanceof Request ? input.url : String(input), location.href)
  if (url.origin !== location.origin || (!url.pathname.startsWith('/api/') && url.pathname !== '/readyz')) {
    return originalFetch(input, init)
  }
  const method = (init?.method || (input instanceof Request ? input.method : 'GET')).toUpperCase()
  let config = readConfig()
  const save = url.pathname === '/api/v1/llm/config' && method === 'PUT'
  if (save && typeof init?.body === 'string') config = candidateConfig(config, JSON.parse(init.body))
  const headers = new Headers(input instanceof Request ? input.headers : undefined)
  new Headers(init?.headers).forEach((value, name) => headers.set(name, value))
  if (Object.keys(config).length) {
    const bytes = new TextEncoder().encode(JSON.stringify(config))
    headers.set('X-ICT-Config', btoa(Array.from(bytes, (x) => String.fromCharCode(x)).join('')))
  }
  const response = await originalFetch(input, { ...init, headers })
  if (save && response.ok) sessionStorage.setItem(CONFIG_KEY, JSON.stringify(config))
  return response
}
