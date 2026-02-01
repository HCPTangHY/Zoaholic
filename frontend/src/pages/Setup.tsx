import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuthStore } from '../store/authStore';

export default function Setup() {
  const [loading, setLoading] = useState(false);
  const [checking, setChecking] = useState(true);
  const [error, setError] = useState('');
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');

  const login = useAuthStore((s) => s.login);
  const navigate = useNavigate();

  useEffect(() => {
    // 若已初始化，直接跳回登录页
    const check = async () => {
      try {
        const res = await fetch('/setup/status');
        if (res.ok) {
          const data = await res.json();
          if (data?.needs_setup === false) {
            navigate('/login');
            return;
          }
        }
      } catch {
        // ignore
      } finally {
        setChecking(false);
      }
    };

    check();
  }, [navigate]);

  const handleSubmit = async (e: import('react').FormEvent) => {
    e.preventDefault();
    setError('');

    if (!username.trim()) {
      setError('请输入管理员用户名');
      return;
    }
    if (password.length < 6) {
      setError('密码至少 6 位');
      return;
    }
    if (password !== confirmPassword) {
      setError('两次输入的密码不一致');
      return;
    }

    setLoading(true);
    try {
      const res = await fetch('/setup/init', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username,
          password,
          confirm_password: confirmPassword,
        }),
      });

      const data = await res.json().catch(() => null);

      if (!res.ok) {
        setError(data?.detail || `初始化失败: HTTP ${res.status}`);
        return;
      }

      const adminApiKey = data?.admin_api_key;
      if (!adminApiKey) {
        setError('初始化成功但未返回管理员 Key');
        return;
      }

      // 初始化后进入登录页（建议使用账号密码 + JWT 登录）
      navigate('/login');
    } catch {
      setError('网络错误，请检查后端服务是否正常启动');
    } finally {
      setLoading(false);
    }
  };

  if (checking) {
    return (
      <div className="min-h-screen bg-background flex items-center justify-center p-4 font-sans">
        <div className="text-muted-foreground">正在检查初始化状态...</div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background flex items-center justify-center p-4 font-sans transition-colors duration-300">
      <div className="w-full max-w-md">
        <div className="flex flex-col items-center mb-8">
          <div className="w-16 h-16 bg-card border border-border rounded-2xl flex items-center justify-center mb-4 shadow-sm">
            <span className="text-primary font-bold text-xl">Z</span>
          </div>
          <h1 className="text-2xl font-bold text-foreground tracking-tight">初始化 Zoaholic</h1>
          <p className="text-muted-foreground mt-2">首次启动需要设置管理员账号与密码</p>
        </div>

        <form onSubmit={handleSubmit} className="bg-card border border-border p-8 rounded-2xl shadow-lg">
          <div className="space-y-4">
            <div>
              <label className="text-sm font-medium text-foreground mb-1.5 block">管理员用户名</label>
              <input
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className="w-full bg-background border border-border focus:border-primary focus:ring-1 focus:ring-primary rounded-lg px-4 py-2.5 text-foreground placeholder:text-muted-foreground outline-none transition-all"
                placeholder="admin"
                required
              />
            </div>

            <div>
              <label className="text-sm font-medium text-foreground mb-1.5 block">密码</label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full bg-background border border-border focus:border-primary focus:ring-1 focus:ring-primary rounded-lg px-4 py-2.5 text-foreground placeholder:text-muted-foreground outline-none transition-all"
                placeholder="至少 6 位"
                required
              />
            </div>

            <div>
              <label className="text-sm font-medium text-foreground mb-1.5 block">确认密码</label>
              <input
                type="password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                className="w-full bg-background border border-border focus:border-primary focus:ring-1 focus:ring-primary rounded-lg px-4 py-2.5 text-foreground placeholder:text-muted-foreground outline-none transition-all"
                required
              />
            </div>

            {error && (
              <div className="text-destructive text-sm font-medium bg-destructive/10 border border-destructive/20 px-3 py-2 rounded-lg">
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={loading}
              className="w-full bg-primary hover:bg-primary/90 text-primary-foreground font-semibold py-2.5 rounded-lg transition-colors mt-6 disabled:opacity-50 disabled:pointer-events-none"
            >
              {loading ? '正在初始化...' : '初始化并进入控制台'}
            </button>
          </div>
        </form>

        <div className="text-xs text-muted-foreground mt-4 leading-relaxed">
          你也可以通过环境变量直接初始化：<br />
          <code className="font-mono">ADMIN_API_KEY</code> 或 <code className="font-mono">CONFIG_YAML_BASE64</code>
        </div>
      </div>
    </div>
  );
}
