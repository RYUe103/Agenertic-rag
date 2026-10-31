export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: {
      ...(init.body instanceof FormData ? {} : {'Content-Type': 'application/json'}),
      ...init.headers,
    },
  });
  const contentType = response.headers.get('content-type') || '';
  let payload: unknown = null;
  if (response.status !== 204) {
    if (contentType.includes('application/json')) {
      payload = await response.json();
    } else {
      const text = await response.text();
      payload = text ? {detail: text.slice(0, 300)} : null;
    }
  }
  if (!response.ok) {
    const detail = payload && typeof payload === 'object' && 'detail' in payload
      ? String((payload as {detail: unknown}).detail)
      : `请求失败（${response.status}）`;
    throw new ApiError(detail, response.status);
  }
  return payload as T;
}

export function queryString(values: Record<string, string | number | undefined | null>): string {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value));
  });
  const value = params.toString();
  return value ? `?${value}` : '';
}

export async function agentStream(path: string, body: unknown, receive: (event: Record<string, any>) => void): Promise<void> {
  const response = await fetch(path, {method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
  if (!response.ok || !response.body) throw new ApiError(`Agent 请求失败（${response.status}）`, response.status);
  const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
  try {
    for (;;) {
      const {value, done} = await reader.read();
      buffer += decoder.decode(value, {stream: !done});
      let end: number;
      while ((end = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, end); buffer = buffer.slice(end + 2);
        if (frame.startsWith('data: ')) receive(JSON.parse(frame.slice(6)));
      }
      if (done) break;
    }
  } finally { reader.releaseLock(); }
}
