const BASE = '/api/v1'

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

type Query = Record<string, string | number | boolean | null | undefined>

function search(query?: Query): string {
  if (!query) return ''
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined || value === '') continue
    params.set(key, String(value))
  }
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

async function request<T>(
  path: string,
  init?: RequestInit & { query?: Query }
): Promise<T> {
  const { query, ...rest } = init ?? {}
  const response = await fetch(`${BASE}${path}${search(query)}`, {
    headers: { 'Content-Type': 'application/json' },
    ...rest
  })
  if (!response.ok) {
    // FastAPI 的错误体统一是 {detail: string}，取不到时退回状态码。
    const body = await response.json().catch(() => null)
    const detail =
      body && typeof body.detail === 'string'
        ? body.detail
        : `HTTP ${response.status}`
    throw new ApiError(response.status, detail)
  }
  return response.json() as Promise<T>
}

export const api = {
  get: <T>(path: string, query?: Query) => request<T>(path, { query }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PUT', body: JSON.stringify(body) })
}
