import type { RecordData } from './types';

let csrf = '';
let sessionPromise: Promise<void> | undefined;
export class ApiError extends Error {
  constructor(
    message: string,
    public status = 0,
    public code = '',
    public details?: unknown,
  ) {
    super(message);
  }
}
export async function initSession(): Promise<void> {
  if (!sessionPromise)
    sessionPromise = (async () => {
      const response = await fetch('/api/session', {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      });
      if (!response.ok)
        throw new ApiError('本机会话连接失败，请从阅藏启动器重新打开工作台。', response.status);
      const data = await response.json();
      csrf = data.csrf_token || data.csrf || data.token || '';
      if (!csrf) throw new ApiError('服务未返回有效的本机会话，请重新启动阅藏。');
    })().catch((error) => {
      sessionPromise = undefined;
      throw error;
    });
  return sessionPromise;
}
export async function api<T = RecordData>(path: string, init: RequestInit = {}): Promise<T> {
  await initSession();
  const headers = new Headers(init.headers);
  headers.set('Accept', 'application/json');
  if (init.method && init.method !== 'GET') headers.set('X-Yuecang-CSRF', csrf);
  if (init.body && !(init.body instanceof FormData))
    headers.set('Content-Type', 'application/json');
  const response = await fetch(path, { ...init, headers, credentials: 'same-origin' });
  if (response.redirected) throw new ApiError('会话已经改变，请从启动器重新打开工作台。', 401);
  const isJson = response.headers.get('content-type')?.includes('application/json');
  const data = isJson ? await response.json() : undefined;
  if (!response.ok) {
    const error = data?.error || data?.detail;
    const message = typeof error === 'string' ? error : error?.message;
    throw new ApiError(
      message ||
        `未收到操作结果（${response.status}）。请先刷新并核对任务或修改记录，再决定是否重试。`,
      response.status,
      error?.code,
      error?.details,
    );
  }
  if (!isJson && response.status !== 204)
    throw new ApiError('服务返回的结果格式不正确，操作状态未确认。');
  return data as T;
}
export const post = <T = RecordData>(path: string, data: unknown = {}) =>
  api<T>(path, { method: 'POST', body: JSON.stringify(data) });
