import { useAuthStore } from '../store/authStore';
import { toastWarning } from '../components/Toast';
import {
  REDIRECT_AFTER_LOGIN_KEY,
  isSessionExpiredHandled,
  markSessionExpiredHandled,
} from './session';

// 修改原因：部分模块（如 authStore、Login 页）历史上从 lib/api 引用登录失效相关符号。
// 修改方式：把这些无副作用的共享常量/函数抽到 lib/session，再从这里 re-export，
//           既保持旧的 import 路径可用，又避免 api.ts 与 authStore 形成循环依赖。
export { REDIRECT_AFTER_LOGIN_KEY, resetSessionExpiredFlag } from './session';

/**
 * 带鉴权与自动登出的 fetch。
 *
 * 说明：
 * - 管理控制台使用 JWT（Authorization: Bearer <jwt>）。
 * - 部分接口会把上游渠道的 401/403 透传回来（例如填错 OpenAI Key 导致上游返回 401）。
 *   这些 401/403 并不代表管理端 JWT 失效，不能因此把用户踢回登录页。
 *
 * 因此仅当后端返回的错误明确指向「本地鉴权失败」时，才自动登出。
 */

/**
 * core/auth.py 中本地鉴权失败的 detail 文案。
 * 如果后端修改了这些文案，此处需要同步更新。
 */
const LOCAL_AUTH_FAILURE_DETAILS = new Set([
  'Invalid or missing API Key',
  'Invalid or missing credentials',
  'Permission denied',
  // 修改原因：Key 被管理员禁用后，本地鉴权层同样会返回该文案，此时管理端 JWT 也应视为失效。
  'API Key has been disabled',
]);

/**
 * 判断 401/403 响应是否来自本地鉴权层。
 *
 * 存在两种本地鉴权失败来源：
 * 1) core/auth.py 的 Depends 鉴权：返回 FastAPI 标准 {"detail": "..."}。
 * 2) core/middleware.py 的 StatsMiddleware：对 /v1 端点鉴权失败时返回
 *    OpenAI 风格 {"error": {"message": "...", "type": "..."}}。
 *
 * 管理控制台大部分请求打到 /v1 管理端点，JWT 失效时命中的是第 2 种格式；
 * 早期实现只识别第 1 种，导致 JWT 过期后不弹提示、不跳转登录页。
 *
 * 上游透传的错误 message 不会与本地固定文案完全一致，因此按精确文案匹配可避免误登出。
 */
async function isLocalAuthFailure(res: Response): Promise<boolean> {
  if (res.status !== 401 && res.status !== 403) return false;

  try {
    const data = await res.clone().json();
    if (!data || typeof data !== 'object') return false;

    // 1) FastAPI 标准 detail 格式（core/auth.py）
    if (typeof data.detail === 'string' && LOCAL_AUTH_FAILURE_DETAILS.has(data.detail)) {
      return true;
    }

    // 2) OpenAI 风格 error.message 格式（core/middleware.py 的 StatsMiddleware）
    const errorObj = (data as { error?: unknown }).error;
    if (errorObj && typeof errorObj === 'object') {
      const message = (errorObj as { message?: unknown }).message;
      if (typeof message === 'string' && LOCAL_AUTH_FAILURE_DETAILS.has(message)) {
        return true;
      }
    }
  } catch {
    // 响应体不是 JSON，不是本地鉴权错误
  }
  return false;
}

/**
 * 处理管理端会话失效：提示用户、记录来源页、登出并跳转登录页。
 * 通过防抖标志保证并发失效只处理一次。
 */
function handleSessionExpired(logout: () => void): void {
  if (isSessionExpiredHandled()) return;
  markSessionExpiredHandled();

  if (typeof window !== 'undefined') {
    const { pathname, search } = window.location;
    // 只有当前不在登录页时才记录来源页，避免登录后又跳回 /login。
    if (pathname !== '/login') {
      try {
        window.sessionStorage.setItem(REDIRECT_AFTER_LOGIN_KEY, `${pathname}${search || ''}`);
      } catch {
        // sessionStorage 不可用时忽略，仅丢失来源页记忆
      }
    }
  }

  // 提示用户登录已过期（复用全局 toast，脱离 React 组件树也可调用）。
  try {
    toastWarning('登录已过期，请重新登录');
  } catch {
    // ignore
  }

  try {
    logout();
  } catch {
    // ignore
  }

  // 跳转登录页（避免在登录页反复跳转）。
  if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
    // 稍作延迟，让 toast 有机会渲染出来再跳转。
    window.setTimeout(() => {
      if (window.location.pathname !== '/login') {
        window.location.href = '/login';
      }
    }, 300);
  }
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

  // 仅当「本地鉴权失败」时才自动登出并提示
  if (await isLocalAuthFailure(res)) {
    handleSessionExpired(logout);
  }

  return res;
}
