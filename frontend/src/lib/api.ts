import { useAuthStore } from '../store/authStore';

/**
 * 带鉴权与自动登出的 fetch。
 *
 * 约定：当后端返回 401/403 时，认为当前 token 已失效（常见于后端重启/更新后 JWT_SECRET 变更），
 * 前端将清除本地 token 并跳转到 /login。
 */
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

  // 统一处理：token 失效 / 权限不足
  if (res.status === 401 || res.status === 403) {
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
