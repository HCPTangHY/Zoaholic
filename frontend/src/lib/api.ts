import { useAuthStore } from '../store/authStore';

/**
 * 带鉴权与自动登出的 fetch。
 *
 * 说明：
 * - 管理控制台使用 JWT（Authorization: Bearer <jwt>）。
 * - 但部分接口会把“上游渠道/模型”的 401/403 透传回来（例如填错 OpenAI Key）。
 *   这些 401/403 并不代表管理端 JWT 失效，不能因此把用户踢回登录页。
 *
 * 因此：
 * - 仅当后端错误明确指向“本地鉴权失败”（token 缺失/失效/过期）时，才自动登出并跳转 /login。
 */

function looksLikeLocalAuthFailure(detail: string): boolean {
  const d = (detail || '').toLowerCase();
  // core/auth.py、routes/auth.py 里常见的鉴权失败文案
  return (
    d.includes('invalid or missing credentials') ||
    d.includes('invalid or expired token') ||
    d.includes('not authenticated') ||
    d.includes('could not validate credentials')
  );
}

async function shouldAutoLogoutOnAuthError(res: Response): Promise<boolean> {
  if (!(res.status === 401 || res.status === 403)) return false;

  // 尽量从 body 中判断是否为“本地鉴权失败”；避免把上游 401/403 误判为 JWT 失效。
  try {
    const text = await res.clone().text();
    if (!text) return false;

    try {
      const data = JSON.parse(text);

      // FastAPI HTTPException: { detail: "..." }
      if (data && typeof data === 'object') {
        const detail = (data as any).detail;
        if (typeof detail === 'string' && looksLikeLocalAuthFailure(detail)) return true;

        // OpenAI 风格错误：{ error: { message: "..." } }
        const err = (data as any).error;
        if (err && typeof err === 'object') {
          const msg = (err as any).message;
          if (typeof msg === 'string' && looksLikeLocalAuthFailure(msg)) return true;
        }
      }
    } catch {
      // body 不是 JSON，就当作普通文本
      if (looksLikeLocalAuthFailure(text)) return true;
    }
  } catch {
    // ignore
  }

  return false;
}
export async function apiFetch(input: RequestInfo | URL, init: RequestInit = {}) {
  const { token, logout } = useAuthStore.getState();

  const headers = new Headers(init.headers || undefined);
  if (token) {
    headers.set('Authorization', `Bearer ${token}`);
  }

  const res = await fetch(input, {
    ...init,
    headers,
  });

  // 统一处理：仅当“本地鉴权失败”时才自动登出。
  if (await shouldAutoLogoutOnAuthError(res)) {
    try {
      logout();
    } catch {
      // ignore
    }

    // 避免在登录页反复跳转
    if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
      window.location.href = '/login';
    }
  }

  return res;
}
