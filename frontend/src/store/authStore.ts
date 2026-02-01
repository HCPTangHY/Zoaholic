import { create } from 'zustand';

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
    set({ isAuthenticated: true, token, role });
  },

  logout: () => {
    localStorage.removeItem('zoaholic_token');
    localStorage.removeItem('zoaholic_role');
    set({ isAuthenticated: false, token: null, role: null });
  },
}));