// 修改原因：登录失效处理需要在 lib/api.ts 与 store/authStore.ts 之间共享「来源页存储键」和
//           「会话过期防抖标志」。若把它们放在 api.ts，会与 authStore 形成互相 import 的循环依赖。
// 修改方式：把这些无副作用的共享常量与标志抽到独立模块，双方都只从这里引入，避免循环依赖。
// 目的：让登录失效提示、跳转、来源页记忆逻辑在不引入模块循环的前提下复用同一份状态。

/**
 * 登录成功后应跳回的来源页存储键（sessionStorage）。
 */
export const REDIRECT_AFTER_LOGIN_KEY = 'zoaholic_redirect_after_login';

/**
 * 会话过期处理防抖标志。
 *
 * 页面加载时经常并发多个请求，token 失效会让它们同时命中 401/403。
 * 用模块级布尔标志保证：登录失效只提示一次、跳转只发生一次。
 */
let sessionExpiredHandled = false;

export function isSessionExpiredHandled(): boolean {
  return sessionExpiredHandled;
}

export function markSessionExpiredHandled(): void {
  sessionExpiredHandled = true;
}

/**
 * 登录成功后调用，重置会话过期防抖标志。
 * 这样下一次 token 再次失效时仍能正常提示并跳转。
 */
export function resetSessionExpiredFlag(): void {
  sessionExpiredHandled = false;
}
