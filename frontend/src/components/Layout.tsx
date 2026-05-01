import { useState, useEffect } from 'react';
import { Outlet, Link, useLocation } from 'react-router-dom';
import { LayoutDashboard, Server, Terminal, Key, Settings as SettingsIcon, LogOut, FileText, Puzzle, Sun, Moon, Laptop, Menu, X, FolderOpen, Github, Loader2, ArrowUpCircle, Download, ExternalLink, Copy, GitBranch } from 'lucide-react';
import { useAuthStore } from '../store/authStore';
import { useThemeStore } from '../store/themeStore';
import { apiFetch } from '../lib/api';
import { toastSuccess, toastError } from './Toast';
import * as Dialog from '@radix-ui/react-dialog';

const navItems = [
  { id: '/', label: '仪表盘', icon: LayoutDashboard },
  { id: '/channels', label: '渠道配置', icon: Server },
  { id: '/playground', label: '测试工坊', icon: Terminal },
  { id: '/plugins', label: '插件管理', icon: Puzzle },
  { id: '/logs', label: '系统日志', icon: FileText },
  { id: '/backend-logs', label: '后台日志', icon: Terminal },
  { id: '/workspace', label: '工作区', icon: FolderOpen },
  { id: '/admin', label: '密钥管理', icon: Key },
  { id: '/settings', label: '系统设置', icon: SettingsIcon },
];

function NavContent({
  pathname,
  theme,
  setTheme,
  logout,
  onNavClick,
  versionLabel,
  checkingUpdate,
  onCheckUpdate,
  hasUpdate,
}: {
  pathname: string;
  theme: string;
  setTheme: (t: 'light' | 'dark' | 'system') => void;
  logout: () => void;
  onNavClick: () => void;
  versionLabel: string;
  checkingUpdate: boolean;
  onCheckUpdate: () => void;
  hasUpdate: boolean;
}) {
  return (
    <>
      <nav className="flex-1 p-4 space-y-1">
        {navItems.map(item => {
          const Icon = item.icon;
          return (
            <Link
              key={item.id}
              to={item.id}
              onClick={onNavClick}
              className={`flex items-center gap-3 px-4 py-3 rounded-lg text-sm font-medium transition-all ${
                pathname === item.id
                  ? 'bg-primary text-primary-foreground shadow-md'
                  : 'text-muted-foreground hover:text-foreground hover:bg-muted'
              }`}
            >
              <Icon className="w-5 h-5" />
              {item.label}
            </Link>
          );
        })}
      </nav>

      <div className="p-4 border-t border-border space-y-1">
        {/* Theme Switcher */}
        <div className="flex items-center bg-muted/70 p-1 rounded-lg mb-2">
          <button onClick={() => setTheme('light')} className={`flex-1 flex justify-center py-1.5 rounded-md text-xs font-medium transition-colors ${theme === 'light' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}>
            <Sun className="w-4 h-4" />
          </button>
          <button onClick={() => setTheme('system')} className={`flex-1 flex justify-center py-1.5 rounded-md text-xs font-medium transition-colors ${theme === 'system' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}>
            <Laptop className="w-4 h-4" />
          </button>
          <button onClick={() => setTheme('dark')} className={`flex-1 flex justify-center py-1.5 rounded-md text-xs font-medium transition-colors ${theme === 'dark' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}>
            <Moon className="w-4 h-4" />
          </button>
        </div>

        <a
          href="https://github.com/HCPTangHY/Zoaholic"
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-3 px-4 py-2 text-sm text-muted-foreground hover:text-foreground transition-colors"
        >
          <Github className="w-5 h-5" /> GitHub
        </a>

        {versionLabel && (
          <button
            onClick={onCheckUpdate}
            disabled={checkingUpdate}
            className="flex items-center gap-2 px-4 py-1.5 text-xs font-mono text-muted-foreground hover:text-foreground transition-colors w-full relative"
            title={hasUpdate ? '有新版本可用，点击更新' : '点击检查更新'}
          >
            {checkingUpdate ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <GitBranch className="w-3.5 h-3.5" />}
            {versionLabel}
            {hasUpdate && (
              <span className="absolute right-3 top-1/2 -translate-y-1/2 w-2 h-2 bg-red-500 rounded-full animate-pulse" />
            )}
          </button>
        )}

        <button
          onClick={logout}
          className="flex items-center gap-3 px-4 py-3 rounded-lg text-sm font-medium text-red-600 dark:text-red-500 hover:bg-red-50 dark:hover:bg-red-500/10 w-full transition-colors"
        >
          <LogOut className="w-5 h-5" />
          退出登录
        </button>
      </div>
    </>
  );
}

export default function Layout() {
  const { token, logout } = useAuthStore();
  const { theme, setTheme } = useThemeStore();
  const location = useLocation();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  // Version & Update
  const [currentVersion, setCurrentVersion] = useState('');
  const [deployType, setDeployType] = useState('');
  const [gitInfo, setGitInfo] = useState<{commit?: string}>({});
  const [checkingUpdate, setCheckingUpdate] = useState(false);
  const [updating, setUpdating] = useState(false);
  const [showUpdateDialog, setShowUpdateDialog] = useState(false);
  const [hasUpdate, setHasUpdate] = useState(false);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [updateInfo, setUpdateInfo] = useState<any>(null);

  // 检查是否被用户跳过或延后
  const isUpdateDismissed = (latestVersion: string): boolean => {
    const skipped = localStorage.getItem('zoaholic_skip_version');
    if (skipped === latestVersion) return true;
    const snoozeUntil = localStorage.getItem('zoaholic_snooze_until');
    if (snoozeUntil && Date.now() < parseInt(snoozeUntil, 10)) return true;
    return false;
  };

  // 页面加载：拉版本 + 静默检查更新
  useEffect(() => {
    if (!token) return;
    (async () => {
      try {
        // 拉版本
        const vRes = await apiFetch('/v1/system/version', { headers: { Authorization: `Bearer ${token}` } });
        if (vRes.ok) {
          const data = await vRes.json();
          setCurrentVersion(data.version || '');
          setDeployType(data.deploy_type || '');
          setGitInfo(data.git || {});
        }
        // 静默检查更新
        const uRes = await apiFetch('/v1/system/check-update', { headers: { Authorization: `Bearer ${token}` } });
        if (uRes.ok) {
          const data = await uRes.json();
          setUpdateInfo(data);
          const latestVer = data.latest_release?.version || data.latest_pypi?.version || data.latest_docker?.version || '';
          setHasUpdate(data.has_update && !isUpdateDismissed(latestVer));
        }
      } catch {} // 静默，不弹错
    })();
  }, [token]);

  const handleSkipVersion = () => {
    const ver = updateInfo?.latest_release?.version || updateInfo?.latest_pypi?.version || '';
    if (ver) localStorage.setItem('zoaholic_skip_version', ver);
    setHasUpdate(false);
    setShowUpdateDialog(false);
    toastSuccess('已跳过此版本');
  };

  const handleSnooze = () => {
    localStorage.setItem('zoaholic_snooze_until', String(Date.now() + 24 * 60 * 60 * 1000));
    setHasUpdate(false);
    setShowUpdateDialog(false);
    toastSuccess('24 小时内不再提醒');
  };

  const handleCheckUpdate = async () => {
    if (!token) return;
    if (updateInfo?.has_update) {
      // 手动点击时无视 dismiss，直接弹
      setShowUpdateDialog(true);
      return;
    }
    setCheckingUpdate(true);
    try {
      const res = await apiFetch('/v1/system/check-update', { headers: { Authorization: `Bearer ${token}` } });
      if (res.ok) {
        const data = await res.json();
        setUpdateInfo(data);
        setHasUpdate(data.has_update || false);
        if (data.has_update) {
          setShowUpdateDialog(true);
        } else {
          toastSuccess('已是最新版本');
        }
      } else {
        toastError(await res.text(), '检查更新失败');
      }
    } catch (err) {
      toastError(err, '检查更新失败');
    } finally {
      setCheckingUpdate(false);
    }
  };

  const handlePerformUpdate = async () => {
    if (!token) return;
    setUpdating(true);
    try {
      const res = await apiFetch('/v1/system/update', {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      });
      if (res.ok) {
        const data = await res.json();
        toastSuccess(`更新到 v${data.new_version}，服务即将重启...`);
        setShowUpdateDialog(false);
        setTimeout(() => window.location.reload(), 5000);
      } else {
        toastError(await res.text(), '更新失败');
      }
    } catch (err) {
      toastError(err, '更新失败');
    } finally {
      setUpdating(false);
    }
  };

  const versionLabel = currentVersion
    ? `v${currentVersion}${deployType === 'git' && gitInfo.commit ? ` (${gitInfo.commit})` : deployType === 'docker' ? ' (docker)' : deployType === 'pip' ? ' (pip)' : ''}`
    : '';

  const handleNavClick = () => {
    setMobileMenuOpen(false);
  };

  const navProps = {
    pathname: location.pathname,
    theme,
    setTheme,
    logout,
    onNavClick: handleNavClick,
    versionLabel,
    checkingUpdate,
    onCheckUpdate: handleCheckUpdate,
    hasUpdate,
  };

  const currentLabel = navItems.find(item => item.id === location.pathname)?.label || 'Zoaholic';

  return (
    <div className="flex h-screen bg-background text-foreground font-sans transition-colors duration-300">
      {/* Desktop Sidebar */}
      <aside className="w-64 bg-card border-r border-border flex-col hidden md:flex">
        <div className="h-16 flex items-center px-6 border-b border-border">
          <div className="flex items-center gap-2">
            <img src="/zoaholic.png" alt="Zoaholic" className="w-8 h-8 rounded-lg shadow-lg" />
            <span className="font-bold text-lg tracking-tight">Zoaholic</span>
          </div>
        </div>
        <NavContent {...navProps} />
      </aside>

      {/* Mobile Menu Overlay */}
      {mobileMenuOpen && (
        <div
          className="fixed inset-0 bg-black/50 z-40 md:hidden"
          onClick={() => setMobileMenuOpen(false)}
        />
      )}

      {/* Mobile Sidebar */}
      <aside className={`fixed inset-y-0 left-0 w-64 bg-card border-r border-border flex flex-col z-50 transform transition-transform duration-300 ease-in-out md:hidden ${
        mobileMenuOpen ? 'translate-x-0' : '-translate-x-full'
      }`}>
        <div className="h-16 flex items-center justify-between px-6 border-b border-border">
          <div className="flex items-center gap-2">
            <img src="/zoaholic.png" alt="Zoaholic" className="w-8 h-8 rounded-lg shadow-lg" />
            <span className="font-bold text-lg tracking-tight">Zoaholic</span>
          </div>
          <button
            onClick={() => setMobileMenuOpen(false)}
            className="p-2 rounded-lg hover:bg-muted transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>
        <NavContent {...navProps} />
      </aside>

      {/* Main Content */}
      <div className="flex-1 flex flex-col min-w-0">
        <header className="h-16 border-b border-border flex items-center px-4 md:px-8 bg-background/80 flex-shrink-0 backdrop-blur-sm">
          {/* Mobile Menu Button */}
          <button
            onClick={() => setMobileMenuOpen(true)}
            className="p-2 rounded-lg hover:bg-muted transition-colors md:hidden mr-2"
          >
            <Menu className="w-5 h-5" />
          </button>

          <h2 className="text-lg font-semibold text-foreground flex items-center gap-2">
            {currentLabel}
          </h2>
        </header>
        <main className="flex-1 overflow-auto p-4 md:p-8 bg-muted/20">
          <div className="max-w-6xl mx-auto h-full">
            <Outlet />
          </div>
        </main>
      </div>

      {/* Update Dialog */}
      <Dialog.Root open={showUpdateDialog} onOpenChange={setShowUpdateDialog}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-black/50 z-[60]" />
          <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-[480px] max-w-[95vw] bg-background border border-border rounded-xl shadow-2xl z-[61] p-6">
            <Dialog.Title className="text-lg font-bold text-foreground flex items-center gap-2 mb-4">
              <ArrowUpCircle className="w-5 h-5 text-blue-500" /> 发现新版本
            </Dialog.Title>

            {updateInfo && (
              <div className="space-y-3 mb-6">
                <div className="flex items-center justify-between bg-muted p-3 rounded-lg border border-border">
                  <div>
                    <span className="text-sm text-muted-foreground">当前版本</span>
                    <div className="font-mono text-foreground">v{updateInfo.current_version || currentVersion}</div>
                    <span className="text-xs text-muted-foreground">{updateInfo.deploy_type}</span>
                  </div>
                  <div className="text-muted-foreground">→</div>
                  <div className="text-right">
                    <span className="text-sm text-muted-foreground">最新版本</span>
                    <div className="font-mono text-blue-500 font-bold">
                      {updateInfo.latest_release?.tag || updateInfo.latest_pypi?.version || updateInfo.latest_docker?.tag || '—'}
                    </div>
                  </div>
                </div>

                {updateInfo.latest_release?.body && (
                  <div className="text-sm text-muted-foreground bg-muted/50 p-3 rounded-lg border border-border max-h-40 overflow-y-auto whitespace-pre-wrap">
                    {updateInfo.latest_release.body}
                  </div>
                )}

                {updateInfo.pending_count > 0 && (
                  <div className="text-sm">
                    <span className="text-muted-foreground">待拉取 </span>
                    <span className="text-foreground font-medium">{updateInfo.pending_count} 个 commit</span>
                    <div className="mt-1.5 bg-muted p-2 rounded-lg border border-border max-h-28 overflow-y-auto font-mono text-xs text-muted-foreground">
                      {updateInfo.pending_commits?.map((c: string, i: number) => <div key={i}>{c}</div>)}
                    </div>
                  </div>
                )}

                {updateInfo.update_instructions?.type === 'docker' && !updateInfo.update_instructions.can_auto_update && (
                  <div className="text-sm">
                    <span className="text-amber-500 font-medium">⚠ 容器未挂载 docker.sock，需手动更新：</span>
                    <div className="mt-1.5 bg-muted p-2 rounded-lg border border-border font-mono text-xs text-foreground">
                      {updateInfo.update_instructions.commands?.map((c: string, i: number) => <div key={i}>$ {c}</div>)}
                    </div>
                  </div>
                )}

                {(updateInfo.latest_release?.html_url || updateInfo.latest_pypi?.html_url) && (
                  <a
                    href={updateInfo.latest_release?.html_url || updateInfo.latest_pypi?.html_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                  >
                    <ExternalLink className="w-3 h-3" />
                    {updateInfo.deploy_type === 'pip' ? '在 PyPI 查看' : '在 GitHub 查看'}
                  </a>
                )}
              </div>
            )}

            <div className="flex items-center gap-2">
              <button onClick={handleSkipVersion} className="px-3 py-2 text-xs text-muted-foreground hover:text-foreground transition-colors">跳过此版本</button>
              <button onClick={handleSnooze} className="px-3 py-2 text-xs text-muted-foreground hover:text-foreground transition-colors">24h 后提醒</button>
              <div className="flex-1" />
              <Dialog.Close className="px-4 py-2 text-sm font-medium text-foreground bg-muted hover:bg-muted/80 rounded-lg">取消</Dialog.Close>
              {updateInfo?.update_instructions?.type === 'docker' && !updateInfo.update_instructions.can_auto_update ? (
                <button
                  onClick={() => {
                    const cmds = updateInfo.update_instructions?.commands?.join('\n') || '';
                    navigator.clipboard.writeText(cmds).then(() => toastSuccess('命令已复制到剪贴板'));
                  }}
                  className="px-4 py-2 text-sm font-medium text-white bg-amber-600 hover:bg-amber-500 rounded-lg flex items-center gap-2"
                >
                  <Copy className="w-4 h-4" /> 复制更新命令
                </button>
              ) : (
                <button
                  onClick={handlePerformUpdate}
                  disabled={updating}
                  className="px-4 py-2 text-sm font-medium text-white bg-blue-600 hover:bg-blue-500 rounded-lg flex items-center gap-2 disabled:opacity-50"
                >
                  {updating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
                  {updating ? '更新中...' : '立即更新'}
                </button>
              )}
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
