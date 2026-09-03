/**
 * fetch 响应桩。
 *
 * 必须同时提供 text() 与 json()：api client 走 text() 以便区分「空 body」
 * 和「JSON 解析失败」，只实现 json() 的桩会掩盖真实行为。
 */
export function jsonResponse(body: unknown, init: { ok?: boolean; status?: number } = {}): Response {
  const text = JSON.stringify(body)
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(text),
  } as Response
}

/** 空 body 响应，用于 DELETE 这类 204 或 200 无内容的接口。 */
export function emptyResponse(status = 204): Response {
  return {
    ok: status < 400,
    status,
    json: () => Promise.reject(new SyntaxError('Unexpected end of JSON input')),
    text: () => Promise.resolve(''),
  } as Response
}

/** body 不是合法 JSON 的响应，用于验证客户端不会把 SyntaxError 泄漏给页面。 */
export function textResponse(text: string, init: { ok?: boolean; status?: number } = {}): Response {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    json: () => Promise.reject(new SyntaxError('Unexpected token')),
    text: () => Promise.resolve(text),
  } as Response
}
