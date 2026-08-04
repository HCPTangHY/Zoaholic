import { create } from 'zustand';
import { resetSessionExpiredFlag, REDIRECT_AFTER_LOGIN_KEY } from '../lib/session';

interface AuthState {
  isAuthenticated: boolean;
  token: string | null; // JWT
  role: 'admin' | 'user' | null;
  login: (token: string, role: 'admin' | 'user') => void;
  logout: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  isAuthenticated: !!localStorage.getItem('zoaholic_token'),
  token: localStorage.getItem('zoaholic_token'),
  role: localStorage.getItem('zoaholic_role') as 'admin' | 'user' | null,

  login: (token, role) => {
    localStorage.setItem('zoaholic_token', token);
    localStorage.setItem('zoaholic_role', role);
    // 修改原因：登录成功后需重置会话过期防重标志，否则下次 token 再失效时不会提醒。
    // 目的：保证每一次登录失效都能正常提醒并跳转。
    resetSessionExpiredFlag();
    set({ isAuthenticated: true, token, role });
  },

  logout: () => {
    localStorage.removeItem('zoaholic_token');
    localStorage.removeItem('zoaholic_role');
    // 主动退出时清除残留的来源页记录，避免下次登录被误导到旧页面。
    try {
      sessionStorage.removeItem(REDIRECT_AFTER_LOGIN_KEY);
    } catch {
      // ignore
    }
    set({ isAuthenticated: false, token: null, role: null });
  },
}));