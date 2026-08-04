import { useEffect, useState } from 'react';
import { useAuthStore } from '../store/authStore';
import { useNavigate } from 'react-router-dom';
import { Activity, Key, LogIn, Github } from 'lucide-react';
import { REDIRECT_AFTER_LOGIN_KEY } from '../lib/session';

export default function Login() {
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  // 修改原因：登录失效被动跳转时，登录页需提示用户“会话已过期”，而不是空白登录页。
  // 修改方式：如果 sessionStorage 里存在来源页，说明是被动跳转，展示一行提示。
  const [expiredHint, setExpiredHint] = useState(false);
  const login = useAuthStore((state) => state.login);
  const navigate = useNavigate();

  useEffect(() => {
    try {
      if (sessionStorage.getItem(REDIRECT_AFTER_LOGIN_KEY)) {
        setExpiredHint(true);
      }
    } catch {
      // ignore
    }
  }, []);

  // 若后端提示需要初始化，则跳转到 /setup
  useEffect(() => {
    const check = async () => {
      try {
        const res = await fetch('/setup/status');
        if (res.ok) {
          const data = await res.json();
          if (data?.needs_setup) {
            navigate('/setup');
          }
        }
      } catch {
        // ignore
      }
    };
    check();
  }, [navigate]);

  const handleLogin = async (e: import('react').FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');

    try {
      const res = await fetch('/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });

      const data = await res.json().catch(() => null);

      if (res.status === 404) {
        // 未初始化
        navigate('/setup');
        return;
      }

      if (!res.ok) {
        setError(data?.detail || `登录失败: HTTP ${res.status}`);
        return;
      }

      const token = data?.access_token;
      if (!token) {
        setError('登录成功但未返回 token');
        return;
      }

      login(token, 'admin');
      // 修改原因：登录成功后应尽量回到登录失效前所在页面，而不是一律回首页。
      // 修改方式：读取并清除 sessionStorage 里的来源页，存在且不是 /login 时跳转回去。
      let redirectTo = '/';
      try {
        const saved = sessionStorage.getItem(REDIRECT_AFTER_LOGIN_KEY);
        sessionStorage.removeItem(REDIRECT_AFTER_LOGIN_KEY);
        if (saved && saved !== '/login' && saved.startsWith('/')) {
          redirectTo = saved;
        }
      } catch {
        // ignore
      }
      navigate(redirectTo, { replace: true });
    } catch {
      setError('网络错误，请检查后端服务是否正常启动');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-background flex items-center justify-center p-4 font-sans transition-colors duration-300">
      <div className="w-full max-w-md">
        <div className="flex flex-col items-center mb-8">
          <div className="w-16 h-16 bg-card border border-border rounded-2xl flex items-center justify-center mb-4 shadow-sm">
            <Activity className="w-8 h-8 text-primary" />
          </div>
          <h1 className="text-2xl font-bold text-foreground tracking-tight">Zoaholic Gateway</h1>
          <p className="text-muted-foreground mt-2">请输入 API Key 登录管理控制台</p>
        </div>

        {expiredHint && (
          <div className="mb-4 text-amber-700 dark:text-amber-300 text-sm font-medium bg-amber-500/10 border border-amber-500/20 px-3 py-2 rounded-lg text-center">
            登录已过期，请重新登录
          </div>
        )}

        <form onSubmit={handleLogin} className="bg-card border border-border p-8 rounded-2xl shadow-lg">
          <div className="space-y-4">
            <div>
              <label className="text-sm font-medium text-foreground mb-1.5 flex items-center gap-2">
                <Key className="w-4 h-4" />
                管理员用户名
              </label>
              <input
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="admin"
                className="w-full bg-background border border-border focus:border-primary focus:ring-1 focus:ring-primary rounded-lg px-4 py-2.5 text-foreground placeholder:text-muted-foreground outline-none transition-all"
                required
              />

              <label className="text-sm font-medium text-foreground mb-1.5 flex items-center gap-2 mt-4">
                <Key className="w-4 h-4" />
                密码
              </label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="请输入密码"
                className="w-full bg-background border border-border focus:border-primary focus:ring-1 focus:ring-primary rounded-lg px-4 py-2.5 text-foreground placeholder:text-muted-foreground outline-none transition-all"
                required
              />
            </div>

            {error && <div className="text-destructive text-sm font-medium bg-destructive/10 border border-destructive/20 px-3 py-2 rounded-lg">{error}</div>}

            <button
              type="submit"
              disabled={loading || !username.trim() || !password.trim()}
              className="w-full bg-primary hover:bg-primary/90 text-primary-foreground font-semibold py-2.5 rounded-lg transition-colors flex items-center justify-center gap-2 mt-6 disabled:opacity-50 disabled:pointer-events-none"
            >
              {loading ? <Activity className="w-4 h-4 animate-spin" /> : <LogIn className="w-4 h-4" />}
              {loading ? '正在验证...' : '进入控制台'}
            </button>
          </div>
        </form>

        <div className="flex justify-center mt-6">
          <a
            href="https://github.com/HCPTangHY/Zoaholic"
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
          >
            <Github className="w-4 h-4" /> GitHub
          </a>
        </div>
      </div>
    </div>
  );
}