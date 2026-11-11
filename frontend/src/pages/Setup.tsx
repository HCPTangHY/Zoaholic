import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';

interface SetupStatus {
  needs_setup: boolean;
  has_config: boolean;
  has_admin_user: boolean;
  database_enabled?: boolean;
}

export default function Setup() {
  const [loading, setLoading] = useState(false);
  const [lifecycle] = useState(() => ({ active: true, submitting: false }));
  const [checking, setChecking] = useState(true);
  const [error, setError] = useState('');
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [existingAdminKey, setExistingAdminKey] = useState('');

  const navigate = useNavigate();
  const requiresAdminKey = Boolean(status?.has_config);
  const requiresExistingPassword = Boolean(status?.has_admin_user && !status.has_config);
  const databaseDisabled = status?.database_enabled === false;

  useEffect(() => {
    let active = true;
    lifecycle.active = true;
    const check = async () => {
      try {
        const res = await fetch('/setup/status', { cache: 'no-store' });
        if (!res.ok) throw new Error(`无法检查初始化状态：HTTP ${res.status}`);
        const data: SetupStatus = await res.json();
        if (!active) return;
        if (!data.needs_setup) {
          navigate('/login');
          return;
        }
        setStatus(data);
      } catch (err) {
        if (active) setError(err instanceof Error ? err.message : '无法检查初始化状态，请刷新重试');
      } finally {
        if (active) setChecking(false);
      }
    };

    void check();
    return () => { active = false; lifecycle.active = false; };
  }, [navigate, lifecycle]);

  const handleSubmit = async (e: import('react').FormEvent) => {
    e.preventDefault();
    if (!lifecycle.active || lifecycle.submitting || loading || checking || !status?.needs_setup || databaseDisabled) return;
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

    if (requiresAdminKey && !existingAdminKey.trim()) {
      setError('请输入现有配置中的管理员 API Key');
      return;
    }

    lifecycle.submitting = true;
    setLoading(true);
    try {
      const res = await fetch('/setup/init', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(requiresAdminKey ? { Authorization: `Bearer ${existingAdminKey.trim()}` } : {}),
        },
        body: JSON.stringify({
          username: username.trim(),
          password,
          confirm_password: confirmPassword,
        }),
      });

      const data = await res.json().catch(() => null);
      if (!lifecycle.active) return;

      if (!res.ok) {
        const detail = data?.detail;
        setError(typeof detail === 'string' ? detail : detail?.message || `初始化失败：HTTP ${res.status}`);
        return;
      }

      const adminApiKey = data?.admin_api_key;
      if (!adminApiKey) {
        setError('初始化成功但未返回管理员 Key');
        return;
      }

      // Keep the PR's header-only recovery credential; never persist it in browser storage.
      setExistingAdminKey('');
      setPassword('');
      setConfirmPassword('');
      navigate('/login');
    } catch {
      if (lifecycle.active) setError('网络错误，请检查后端服务是否正常启动');
    } finally {
      lifecycle.submitting = false;
      if (lifecycle.active) setLoading(false);
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
          <h1 className="text-2xl font-bold leading-tight text-foreground tracking-tight">初始化 Zoaholic</h1>
          <p className="text-sm leading-relaxed text-muted-foreground mt-2">完成管理员初始化后，API Key 才能调用网关。</p>
        </div>

        <form onSubmit={handleSubmit} className="bg-card border border-border p-6 sm:p-8 rounded-2xl shadow-sm">
          <fieldset disabled={loading || !status || databaseDisabled} className="space-y-4 disabled:opacity-50">
            {requiresAdminKey && (
              <div>
                <label htmlFor="setup-admin-key" className="text-sm font-medium text-foreground mb-2 block">现有管理员 API Key</label>
                <p className="text-xs leading-relaxed text-muted-foreground mb-2">检测到已有配置。补建管理员必须验证现有 Admin Key，普通 User Key 不能使用，不会因此开放其他接口。</p>
                <input
                  id="setup-admin-key"
                  type="password"
                  value={existingAdminKey}
                  onChange={(e) => setExistingAdminKey(e.target.value)}
                  className="w-full bg-background border border-border focus:border-primary focus:ring-1 focus:ring-primary rounded-lg px-4 py-2.5 outline-none"
                  autoComplete="off"
                  required
                />
              </div>
            )}
            {requiresExistingPassword && <p className="rounded-xl bg-muted/40 p-4 text-sm leading-relaxed text-muted-foreground">管理员账号已存在，但配置缺失。请输入原管理员用户名和密码验证身份；此操作不会重设管理员密码。</p>}
            {databaseDisabled && <p role="alert" className="rounded-xl bg-muted/40 p-4 text-sm leading-relaxed text-muted-foreground">数据库已关闭，无法持久化管理员账号。请启用数据库后完成初始化；仅设置 api.yaml 或 JWT_SECRET 不会开放 API。</p>}
            <div>
              <label htmlFor="setup-username" className="text-sm font-medium text-foreground mb-2 block">管理员用户名</label>
              <input
                id="setup-username"
                autoComplete="username"
                maxLength={64}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className="w-full bg-background border border-border focus:border-primary focus:ring-1 focus:ring-primary rounded-lg px-4 py-2.5 text-foreground placeholder:text-muted-foreground outline-none transition-all"
                placeholder="admin"
                required
              />
            </div>

            <div>
              <label htmlFor="setup-password" className="text-sm font-medium text-foreground mb-2 block">密码</label>
              <input
                id="setup-password"
                type="password"
                autoComplete="new-password"
                minLength={6}
                maxLength={256}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full bg-background border border-border focus:border-primary focus:ring-1 focus:ring-primary rounded-lg px-4 py-2.5 text-foreground placeholder:text-muted-foreground outline-none transition-all"
                placeholder="至少 6 位"
                required
              />
            </div>

            <div>
              <label htmlFor="setup-confirm-password" className="text-sm font-medium text-foreground mb-2 block">确认密码</label>
              <input
                id="setup-confirm-password"
                type="password"
                autoComplete="new-password"
                minLength={6}
                maxLength={256}
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                className="w-full bg-background border border-border focus:border-primary focus:ring-1 focus:ring-primary rounded-lg px-4 py-2.5 text-foreground placeholder:text-muted-foreground outline-none transition-all"
                required
              />
            </div>

            {error && (
              <div role="alert" className="text-destructive text-sm leading-relaxed font-medium bg-destructive/10 border border-destructive/20 px-3 py-2 rounded-lg">
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={loading || !status || databaseDisabled}
              className="w-full bg-primary hover:bg-primary/90 text-primary-foreground text-sm font-semibold py-3 rounded-lg transition-all duration-150 active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary mt-6 disabled:opacity-50 disabled:pointer-events-none"
            >
              {loading ? '正在初始化...' : '初始化并进入控制台'}
            </button>
          </fieldset>
        </form>

        <div className="text-xs text-muted-foreground mt-4 leading-relaxed">
          环境变量或 api.yaml 可以提供初始配置，但不能代替管理员账号初始化。恢复 Key 仅用于此次请求，不保存到浏览器存储。
        </div>
      </div>
    </div>
  );
}
